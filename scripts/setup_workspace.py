"""Bootstrap the personal Databricks Free Edition workspace.

Idempotent. Safe to run repeatedly.

Creates:
  - Catalog:     ${DBX_CATALOG}
  - Schema:      ${DBX_CATALOG}.${DBX_SCHEMA}
  - Volume:      ${DBX_VOLUME}
  - Delta tables: runs, findings, management_actions, trace_events,
                  uploaded_files, llm_calls, evaluation_runs, golden_examples

Usage:
    python scripts/setup_workspace.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the package root (parent of scripts/) -- a no-op (never
# raises) when the file does not exist, e.g. a workspace notebook/Git
# folder checkout with no local .env (independent review 2026-09-24 item 7).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PACKAGE_ROOT / ".env")


def build_ddl_statements(catalog: str, schema: str, volume: str) -> list[str]:
    """Every DDL statement setup_workspace bootstraps -- a function of
    (catalog, schema, volume) rather than a module-level constant, so
    importing this module (e.g. from a notebook, independent review
    2026-09-24 item 7) never requires these to already be set as
    environment variables.
    """
    CATALOG, SCHEMA, VOLUME = catalog, schema, volume
    return [
        f"CREATE CATALOG IF NOT EXISTS {CATALOG}",
        f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}",
        # Volume path components: /Volumes/<catalog>/<schema>/<name>
        # Extract the volume name (last path segment) for the CREATE VOLUME statement.
        f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{SCHEMA}.{VOLUME.rstrip('/').split('/')[-1]}",

        # ----- Runs ---------------------------------------------------------------
        f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.runs (
            run_id STRING NOT NULL,
            skill_id STRING,
            skill_name STRING,
            mode STRING,
            audit_period_start DATE,
            audit_period_end DATE,
            run_owner STRING,
            data_mode STRING,
            status STRING,
            findings_count INT,
            high_risk_count INT,
            potential_exposure DOUBLE,
            open_actions INT,
            run_config STRING,
            started_at TIMESTAMP,
            updated_at TIMESTAMP
        ) USING DELTA
        PARTITIONED BY (skill_id)
        """,

        # ----- Findings -----------------------------------------------------------
        f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.findings (
            run_id STRING NOT NULL,
            finding_id STRING NOT NULL,
            skill_id STRING,
            title STRING,
            category STRING,
            severity STRING,
            risk_score DOUBLE,
            exposure_aud DOUBLE,
            evidence_refs STRING,
            metrics_json STRING,
            narrative STRING,
            priority_rationale STRING,
            created_at TIMESTAMP
        ) USING DELTA
        PARTITIONED BY (skill_id)
        """,

        # ----- Management actions -------------------------------------------------
        f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.management_actions (
            action_id STRING NOT NULL,
            run_id STRING,
            finding_id STRING,
            skill_id STRING,
            title STRING,
            owner STRING,
            due_date DATE,
            status STRING,
            remediation_draft STRING,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        ) USING DELTA
        PARTITIONED BY (skill_id)
        """,

        # ----- Trace events -------------------------------------------------------
        f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.trace_events (
            run_id STRING NOT NULL,
            event_id STRING NOT NULL,
            node STRING,
            status STRING,
            message STRING,
            duration_ms BIGINT,
            emitted_at TIMESTAMP
        ) USING DELTA
        """,

        # ----- Uploaded files -----------------------------------------------------
        f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.uploaded_files (
            run_id STRING NOT NULL,
            filename STRING NOT NULL,
            volume_path STRING,
            mime_type STRING,
            size_bytes BIGINT,
            validation_status STRING,
            validation_notes STRING,
            uploaded_at TIMESTAMP
        ) USING DELTA
        """,

        # ----- LLM call audit trail -----------------------------------------------
        f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.llm_calls (
            call_id STRING NOT NULL,
            run_id STRING,
            node STRING,
            model STRING,
            temperature DOUBLE,
            prompt STRING,
            response STRING,
            input_tokens INT,
            output_tokens INT,
            latency_ms BIGINT,
            error STRING,
            cached BOOLEAN,
            called_at TIMESTAMP
        ) USING DELTA
        """,

        # ----- Evaluation results -------------------------------------------------
        f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.evaluation_runs (
            eval_id STRING NOT NULL,
            skill_id STRING,
            node STRING,
            model STRING,
            prompt_version STRING,
            surface STRING,
            metric STRING,
            value DOUBLE,
            mlflow_run_id STRING,
            evaluated_at TIMESTAMP
        ) USING DELTA
        """,

        # ----- Golden examples for Surface 4 --------------------------------------
        f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.golden_examples (
            example_id STRING NOT NULL,
            skill_id STRING,
            node STRING,
            inputs_json STRING,
            reference_output STRING,
            rubric_json STRING,
            version INT,
            created_at TIMESTAMP
        ) USING DELTA
        """,
    ]


# Cost incident, CLAUDE.md §11 "Cost and idle fixes" / independent review
# 2026-09-24 item 6: the deployed App's idle polling kept this warehouse
# awake for ~20 hours, ~230 DBU (~$218). The idle-polling fix (orchestrator/
# executor.py) is necessary but not sufficient on its own -- the warehouse's
# own auto_stop is the second, independent control, and it must never be
# left at a platform default that could be longer than a minute.
_WAREHOUSE_AUTO_STOP_MINS = 1


def _ensure_warehouse_auto_stop(w: "WorkspaceClient", warehouse_id: str) -> None:
    """Idempotently sets this warehouse's auto_stop_mins to
    _WAREHOUSE_AUTO_STOP_MINS -- a no-op API call if it is already set (never
    calls .edit() when the current value already matches, so a repeat run of
    this bootstrap never resets an unrelated in-flight query's warehouse)."""
    current = w.warehouses.get(warehouse_id)
    if current.auto_stop_mins == _WAREHOUSE_AUTO_STOP_MINS:
        print(f"  auto_stop_mins already {_WAREHOUSE_AUTO_STOP_MINS} on {current.name} ({warehouse_id})")
        return
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

    _ensure_warehouse_auto_stop(w, warehouse_id)

    print(f"Bootstrapping {catalog}.{schema} ...")
    for stmt in build_ddl_statements(catalog, schema, volume):
        clean = " ".join(stmt.split())
        print(f"  -> {clean[:80]}...")
        w.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=stmt,
            wait_timeout="30s",
        )

    print("\n[OK] Workspace bootstrap complete.")
    print(f"     Catalog:  {catalog}")
    print(f"     Schema:   {catalog}.{schema}")
    print(f"     Volume:   {volume}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
