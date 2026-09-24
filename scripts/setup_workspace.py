"""Bootstrap the Databricks workspace: catalog, schema, Volume, warehouse
auto-stop, and every Delta table via the real migration path --
`DeltaPersistence(settings).migrate()` against `orchestrator/ddl/delta/*.sql`.

Idempotent. Safe to run repeatedly.

Independent review 2026-09-24 item 1: this script used to carry its own,
separately-maintained DDL that had drifted out of sync with the P1A-P6
migrations under `orchestrator/ddl/delta/` -- e.g. its old `runs` table had
none of `run_kind`/`phase`/`fingerprint_id`/`state_version`, and its old
`findings`/`node_attempts`/`llm_calls` shapes did not match the real ones
either. There is now exactly one place table DDL is defined: the migration
files the deployed App itself applies at start (`orchestrator.service.
build_app_context` -> `DeltaPersistence.migrate()`). This script creates the
catalog/schema/Volume those migrations need to run against, sets the
warehouse's auto-stop (CLAUDE.md §11 cost incident), then calls the same
`migrate()` the App calls.

Usage:
    python scripts/setup_workspace.py
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the package root (parent of scripts/) -- a no-op (never
# raises) when the file does not exist, e.g. a workspace notebook/Git
# folder checkout with no local .env (independent review 2026-09-24 item 7).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PACKAGE_ROOT / ".env")
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


def build_bootstrap_statements(catalog: str, schema: str, volume: str) -> list[str]:
    """The only statements this script issues directly: catalog, schema and
    Volume creation, a function of (catalog, schema, volume) rather than a
    module-level constant, so importing this module (e.g. from a notebook)
    never requires these to already be set as environment variables. Every
    table is created by `DeltaPersistence.migrate()` (see module docstring),
    not here.
    """
    return [
        f"CREATE CATALOG IF NOT EXISTS {catalog}",
        f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}",
        # Volume path components: /Volumes/<catalog>/<schema>/<name>
        # Extract the volume name (last path segment) for the CREATE VOLUME statement.
        f"CREATE VOLUME IF NOT EXISTS {catalog}.{schema}.{volume.rstrip('/').split('/')[-1]}",
    ]


# Cost incident, CLAUDE.md §11 "Cost and idle fixes" / independent review
# 2026-09-24 item 6: the deployed App's idle polling kept this warehouse
# awake for ~20 hours, ~230 DBU (~$218). The idle-polling fix (orchestrator/
# executor.py) is necessary but not sufficient on its own -- the warehouse's
# own auto_stop is the second, independent control, and it must never be
# left at a platform default that could be longer than a minute.
_WAREHOUSE_AUTO_STOP_MINS = 1


def _ensure_warehouse_ready(w: "WorkspaceClient", warehouse_id: str) -> str:
    """Idempotently sets this warehouse's auto_stop_mins to
    _WAREHOUSE_AUTO_STOP_MINS -- a no-op API call if it is already set (never
    calls .edit() when the current value already matches, so a repeat run of
    this bootstrap never resets an unrelated in-flight query's warehouse).
    Returns the warehouse's own http_path, read back from the platform
    rather than guessed (same pattern as scripts/deploy_app.py's
    _resolve_warehouse), which DeltaPersistence needs to connect."""
    current = w.warehouses.get(warehouse_id)
    if current.auto_stop_mins == _WAREHOUSE_AUTO_STOP_MINS:
        print(f"  auto_stop_mins already {_WAREHOUSE_AUTO_STOP_MINS} on {current.name} ({warehouse_id})")
    else:
        print(
            f"  setting auto_stop_mins {current.auto_stop_mins} -> {_WAREHOUSE_AUTO_STOP_MINS} "
            f"on {current.name} ({warehouse_id})"
        )
        w.warehouses.edit(
            id=warehouse_id,
            name=current.name,
            cluster_size=current.cluster_size,
            auto_stop_mins=_WAREHOUSE_AUTO_STOP_MINS,
            min_num_clusters=current.min_num_clusters,
            max_num_clusters=current.max_num_clusters,
            enable_serverless_compute=current.enable_serverless_compute,
        )
    if current.odbc_params is None or not current.odbc_params.path:
        raise SystemExit(f"ERROR: warehouse {warehouse_id!r} has no resolvable http_path.")
    return current.odbc_params.path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=None, help="Overrides DBX_CATALOG.")
    parser.add_argument("--schema", default=None, help="Overrides DBX_SCHEMA.")
    parser.add_argument("--volume", default=None, help="Overrides DBX_VOLUME.")
    parser.add_argument("--warehouse-id", default=None, help="Overrides DBX_WAREHOUSE_ID.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Importable entry point (independent review item 7 -- runnable from a
    workspace notebook on a cluster with notebook authentication, where
    `WorkspaceClient()` needs no PAT). `argv` defaults to `sys.argv[1:]`;
    returns 0 on success, 1 on a resolvable-here configuration problem (no
    catalog/schema/volume, no warehouse) rather than calling `sys.exit`
    itself, so a notebook caller can act on the return value without a
    `SystemExit` propagating into its own cell."""
    args = build_parser().parse_args(argv)

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        print("ERROR: databricks-sdk not installed. Run: pip install -r requirements.txt")
        return 1

    catalog = args.catalog or os.environ.get("DBX_CATALOG")
    schema = args.schema or os.environ.get("DBX_SCHEMA")
    volume = args.volume or os.environ.get("DBX_VOLUME")
    missing = [n for n, v in (("DBX_CATALOG", catalog), ("DBX_SCHEMA", schema), ("DBX_VOLUME", volume)) if not v]
    if missing:
        print(f"ERROR: missing required configuration: {', '.join(missing)} (env or --catalog/--schema/--volume).")
        return 1

    w = WorkspaceClient()

    warehouse_id = args.warehouse_id or os.environ.get("DBX_WAREHOUSE_ID")
    if not warehouse_id:
        # Free Edition users typically have a single Serverless warehouse; discover it.
        warehouses = list(w.warehouses.list())
        if not warehouses:
            print("ERROR: no SQL warehouse found. Create one and set DBX_WAREHOUSE_ID in .env.")
            return 1
        warehouse_id = warehouses[0].id
        print(f"Using warehouse: {warehouses[0].name} ({warehouse_id})")

    warehouse_http_path = _ensure_warehouse_ready(w, warehouse_id)

    print(f"Bootstrapping {catalog}.{schema} ...")
    for stmt in build_bootstrap_statements(catalog, schema, volume):
        clean = " ".join(stmt.split())
        print(f"  -> {clean[:80]}...")
        w.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=stmt,
            wait_timeout="30s",
        )

    from orchestrator.adapters.persistence_delta import DeltaPersistence
    from orchestrator.config import load_settings

    settings = load_settings()
    settings = dataclasses.replace(
        settings,
        catalog=catalog,
        schema=schema,
        volume=volume,
        warehouse_http_path=warehouse_http_path,
        host=settings.host or os.environ.get("DATABRICKS_HOST"),
    )

    print("Applying Delta migrations (orchestrator/ddl/delta) ...")
    persistence = DeltaPersistence(settings)
    applied = persistence.migrate()
    if applied:
        print(f"  applied: {', '.join(applied)}")
    else:
        print("  already up to date -- no pending migrations")

    print("\n[OK] Workspace bootstrap complete.")
    print(f"     Catalog:  {catalog}")
    print(f"     Schema:   {catalog}.{schema}")
    print(f"     Volume:   {volume}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
