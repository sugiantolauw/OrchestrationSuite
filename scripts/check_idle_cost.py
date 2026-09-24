"""Post-deploy idle-cost check (CLAUDE.md §11 "Cost incident and fixes" /
independent review 2026-09-24 item 6): after a deploy, with the App sitting
idle (no run in flight, nobody clicking around), the warehouse should end the
window STOPPED and should not have been woken (started) more than a handful
of times. This is the regression check for the 2026-09-23 incident (~3,000
queries/hour while idle, warehouse never stopped, ~$218).

The corporate-workspace assessment (CLAUDE.md §11 "Corporate workspace
assessment") found `system.query.history` and `system.serving.endpoint_usage`
are not available there, so this script no longer depends on
`system.query.history`. Instead it counts `STARTING` rows in
`system.compute.warehouse_events` for the target warehouse over the trailing
window (every unwanted wake of a 1-minute-auto-stop warehouse is its own
"STARTING" event, regardless of what triggered it) and reads the warehouse's
current state via the SDK. When `system.compute.warehouse_events` itself is
unavailable (permission denied, or the table does not exist in this
workspace), the script does NOT fail the run on that alone -- it reports
"not available" with the reason and falls back to the warehouse-state check
only, per CLAUDE.md's "no fake successful integrations" rule (NN13): a
missing system table is reported, not silently treated as zero activity, but
it also must not turn an environment limitation into a spurious idle-cost
failure. Exit is non-zero only for a *real* idle-cost failure (warehouse
still running, or -- when the events table is readable -- too many start
events); a merely-unavailable system table exits 0 with a warning line.

This is deliberately NOT run against the live workspace by this session
(CLAUDE.md operating instructions) -- its logic is exercised by
tests/test_check_idle_cost.py against a fake client, never live here.

Usage:
    python scripts/check_idle_cost.py --minutes 30 [--max-start-events 2]
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

# Substrings seen in Databricks SQL errors for a system table that exists but
# is not granted, or does not exist in this workspace/region at all. Matched
# case-insensitively against the exception's str(); anything else is treated
# as a real failure and re-raised (CLAUDE.md NN13 -- never silently swallow
# an error that is not actually "this table is unavailable").
_UNAVAILABLE_ERROR_MARKERS = (
    "table_or_view_not_found",
    "permission_denied",
    "access_denied",
    "does not exist",
    "not found",
    "unauthorized_access",
)


class SystemTableUnavailable(Exception):
    """Raised internally, then caught, when a system-table read fails for a
    permission/existence reason rather than a real error. Never propagates
    out of count_warehouse_start_events -- callers see a (None, reason) pair
    instead so a missing system table cannot silently masquerade as zero
    activity (CLAUDE.md NN13)."""


def _looks_unavailable(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _UNAVAILABLE_ERROR_MARKERS)


@dataclass
class IdleCostReport:
    warehouse_state: str
    window_minutes: int
    max_allowed_start_events: int
    start_event_count: int | None
    start_events_unavailable_reason: str | None = None

    @property
    def ok(self) -> bool:
        if self.warehouse_state != "STOPPED":
            return False
        if self.start_event_count is None:
            # The events table is not available -- that is an environment
            # fact to report, not by itself an idle-cost failure (the
            # warehouse-state check above is still a real, independent
            # signal that the app is not being kept awake).
            return True
        return self.start_event_count <= self.max_allowed_start_events

    def describe(self) -> str:
        lines = [f"Window: last {self.window_minutes} minute(s)"]
        if self.start_event_count is None:
            lines.append(
                "Warehouse start events: NOT AVAILABLE "
                f"({self.start_events_unavailable_reason})"
            )
        else:
            lines.append(
                f"Warehouse start events: {self.start_event_count} "
                f"(threshold: <= {self.max_allowed_start_events})"
            )
        lines.append(f"Warehouse state: {self.warehouse_state} (expected: STOPPED)")
        lines.append("RESULT: " + ("OK" if self.ok else "FAIL — idle App is not actually idle"))
        return "\n".join(lines)


def count_warehouse_start_events(
    sql_client, *, warehouse_id: str, since: datetime
) -> tuple[int | None, str | None]:
    """Counts `STARTING` rows in system.compute.warehouse_events for
    `warehouse_id` since `since`. `sql_client` is anything exposing
    `.execute(statement: str) -> list[dict]` -- the real caller wraps
    databricks-sql-connector or the SDK's statement execution API; tests pass
    a fake that returns a canned row set, never a live connection (see this
    module's own docstring).

    Returns (count, None) on a normal read, or (None, reason) when the
    system table itself is unavailable (permission/table-not-found) rather
    than raising -- see SystemTableUnavailable above. Any other failure
    (a malformed result, an unrelated SQL error) is re-raised: it is a real
    problem, not an environment limitation."""
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        rows = sql_client.execute(
            "SELECT count(*) AS n FROM system.compute.warehouse_events "
            "WHERE event_time >= TIMESTAMP '{}' AND warehouse_id = '{}' "
            "AND event_type = 'STARTING'".format(since_iso, warehouse_id)
        )
    except Exception as exc:  # noqa: BLE001 -- classified below, not swallowed blind
        if _looks_unavailable(exc):
            return None, str(exc)
        raise
    if not rows:
        raise RuntimeError(
            "system.compute.warehouse_events returned no rows at all for a COUNT(*) query "
            "-- cannot assert idle-ness from nothing"
        )
    return int(rows[0]["n"]), None


def get_warehouse_state(workspace_client, warehouse_id: str) -> str:
    return str(workspace_client.warehouses.get(warehouse_id).state)


def check_idle_cost(
    sql_client, workspace_client, *, client_id: str, warehouse_id: str,
    minutes: int, max_start_events: int = 2, now: datetime | None = None,
    **_legacy_kwargs,
) -> IdleCostReport:
    """`client_id` is accepted and unused (kept for the deploy-script call
    signature; system.compute.warehouse_events is not attributed per caller
    the way system.query.history was, so it no longer filters by it).
    `max_queries` is accepted via `_legacy_kwargs` for callers not yet
    updated to `max_start_events`."""
    max_start_events = _legacy_kwargs.pop("max_queries", max_start_events)
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=minutes)
    start_event_count, unavailable_reason = count_warehouse_start_events(
        sql_client, warehouse_id=warehouse_id, since=since
    )
    warehouse_state = get_warehouse_state(workspace_client, warehouse_id)
    return IdleCostReport(
        warehouse_state=warehouse_state,
        window_minutes=minutes,
        max_allowed_start_events=max_start_events,
        start_event_count=start_event_count,
        start_events_unavailable_reason=unavailable_reason,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=30, help="lookback window, minutes")
    parser.add_argument(
        "--max-start-events", type=int, default=2,
        help="max warehouse STARTING events tolerated while idle (system.compute.warehouse_events)",
    )
    parser.add_argument(
        "--max-queries", type=int, default=None,
        help="deprecated alias for --max-start-events, kept for old callers",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Importable entry point (independent review item 7 -- runnable from a
    workspace notebook, not only `python scripts/check_idle_cost.py`).
    `argv` defaults to `sys.argv[1:]` when omitted; returns the process exit
    code instead of calling `sys.exit` itself so a notebook caller can act on
    it without a `SystemExit` in its own cell."""
    args = build_parser().parse_args(argv)
    max_start_events = args.max_queries if args.max_queries is not None else args.max_start_events

    warehouse_id = os.environ.get("DBX_WAREHOUSE_ID")
    if not warehouse_id:
        print("ERROR: DBX_WAREHOUSE_ID must be set (the App must already be deployed -- see scripts/deploy_app.py).")
        return 2
    client_id = os.environ.get("DBX_APP_SERVICE_PRINCIPAL_CLIENT_ID", "")

    from databricks.sdk import WorkspaceClient

    workspace_client = WorkspaceClient()
    sql_client = _RealSqlClient(workspace_client, warehouse_id)

    report = check_idle_cost(
        sql_client, workspace_client, client_id=client_id, warehouse_id=warehouse_id,
        minutes=args.minutes, max_start_events=max_start_events,
    )
    print(report.describe())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
