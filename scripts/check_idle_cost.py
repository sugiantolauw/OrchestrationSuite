"""Post-deploy idle-cost check (CLAUDE.md §11 "Cost incident and fixes" /
independent review 2026-09-24 item 6): after a deploy, with the App sitting
idle (no run in flight, nobody clicking around), the App's service principal
should issue ~0 SQL statements and the warehouse should end the window
STOPPED. This is the regression check for the 2026-09-23 incident (~3,000
queries/hour while idle, warehouse never stopped, ~$218).

Queries `system.query.history` for statements attributed to the App's
service principal (identified by `by_client_id`) over the trailing
`--minutes` window, and `system.compute.warehouses`/the warehouses API for
the warehouse's current state. Fails loudly (non-zero exit, printed detail)
rather than a silent pass on a query that returned nothing meaningful.

This is deliberately NOT run against the live workspace by this session
(CLAUDE.md operating instructions) -- its logic is exercised by
tests/test_check_idle_cost.py against a fake client, never live here.

Usage:
    python scripts/check_idle_cost.py --minutes 30 [--max-queries 5]
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PACKAGE_ROOT / ".env")


@dataclass
class IdleCostReport:
    query_count: int
    warehouse_state: str
    window_minutes: int
    max_allowed_queries: int

    @property
    def ok(self) -> bool:
        return self.query_count <= self.max_allowed_queries and self.warehouse_state == "STOPPED"

    def describe(self) -> str:
        lines = [
            f"Window: last {self.window_minutes} minute(s)",
            f"Queries from the App's service principal: {self.query_count} "
            f"(threshold: <= {self.max_allowed_queries})",
            f"Warehouse state: {self.warehouse_state} (expected: STOPPED)",
        ]
        lines.append("RESULT: " + ("OK" if self.ok else "FAIL — idle App is not actually idle"))
        return "\n".join(lines)


def count_recent_queries(sql_client, *, client_id: str, since: datetime) -> int:
    """Counts rows in system.query.history attributed to `client_id` (the
    App's service principal application id) since `since`. `sql_client` is
    anything exposing `.execute(statement: str) -> list[dict]` -- the real
    caller wraps databricks-sql-connector or the SDK's statement execution
    API; tests pass a fake that returns a canned row set, never a live
    connection (see this module's own docstring)."""
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S")
    rows = sql_client.execute(
        "SELECT count(*) AS n FROM system.query.history "
        "WHERE start_time >= TIMESTAMP '{}' AND by_client_id = '{}'".format(since_iso, client_id)
    )
    if not rows:
        raise RuntimeError("system.query.history returned no rows at all -- cannot assert idle-ness from nothing")
    return int(rows[0]["n"])


def get_warehouse_state(workspace_client, warehouse_id: str) -> str:
    return str(workspace_client.warehouses.get(warehouse_id).state)


def check_idle_cost(
    sql_client, workspace_client, *, client_id: str, warehouse_id: str,
    minutes: int, max_queries: int, now: datetime | None = None,
) -> IdleCostReport:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=minutes)
    query_count = count_recent_queries(sql_client, client_id=client_id, since=since)
    warehouse_state = get_warehouse_state(workspace_client, warehouse_id)
    return IdleCostReport(
        query_count=query_count, warehouse_state=warehouse_state,
        window_minutes=minutes, max_allowed_queries=max_queries,
    )


class _RealSqlClient:
    """The real `.execute(statement) -> list[dict]` wrapper around a live
    SQL warehouse connection, same pattern as
    orchestrator.adapters.persistence_delta.DeltaPersistence's own
    connection factory (never reused directly -- that class owns Delta's
    retry/idempotency contract, this script only ever runs one read-only
    query). Never exercised by tests -- see this module's own docstring."""

    def __init__(self, workspace_client, warehouse_id: str):
        from databricks import sql
        from databricks.sdk.core import Config

        settings_host = workspace_client.config.host
        cfg = Config(host=settings_host)
        hostname = settings_host.replace("https://", "").replace("http://", "").rstrip("/")
        http_path = workspace_client.warehouses.get(warehouse_id).odbc_params.path
        self._conn = sql.connect(
            server_hostname=hostname, http_path=http_path, credentials_provider=lambda: cfg.authenticate,
        )

    def execute(self, statement: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(statement)
            columns = [c[0] for c in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=30, help="lookback window, minutes")
    parser.add_argument("--max-queries", type=int, default=5, help="max queries tolerated while idle")
    args = parser.parse_args()

    client_id = os.environ.get("DBX_APP_SERVICE_PRINCIPAL_CLIENT_ID")
    warehouse_id = os.environ.get("DBX_WAREHOUSE_ID")
    if not client_id or not warehouse_id:
        print(
            "ERROR: DBX_APP_SERVICE_PRINCIPAL_CLIENT_ID and DBX_WAREHOUSE_ID must be set "
            "(the App must already be deployed -- see scripts/deploy_app.py)."
        )
        sys.exit(2)

    from databricks.sdk import WorkspaceClient

    workspace_client = WorkspaceClient()
    sql_client = _RealSqlClient(workspace_client, warehouse_id)

    report = check_idle_cost(
        sql_client, workspace_client, client_id=client_id, warehouse_id=warehouse_id,
        minutes=args.minutes, max_queries=args.max_queries,
    )
    print(report.describe())
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
