"""Load the T&E source files (synthetic_data/) into Unity Catalog as governed Delta
tables, so the T&E Skill can be run with its data sources pointed at UC instead of
local files (CLAUDE.md §2.1, §4.3 contract.yaml raw sources).

Idempotent and re-runnable: schema/volume/tables are created with IF NOT EXISTS /
CREATE OR REPLACE, so running this twice against the same files produces the same
result. Every workspace-specific value (catalog, schema, volume, warehouse) comes
from the environment or CLI flags -- nothing hardcoded (CLAUDE.md NN16).

For each of the 8 sources declared in skills/tne_exco/contract.yaml's `sources` map:
  1. Read the file with pandas exactly as orchestrator.contract.LocalFileDataSource
     does (same sheet, same format, no header trimming -- the RAW header is what
     gets preserved in Delta).
  2. Add a 1-based `_source_row` column (the row's position in the original file),
     which is how orchestrator.adapters.datasource_uc.UCTableDataSource builds a
     `__row_key` that traces a UC row back to an exact row in the source file.
  3. Write a Parquet copy, upload it to the Volume via the Files API, and
     CREATE OR REPLACE a Delta table over it with column mapping enabled (mode =
     'name') so original headers -- spaces, parentheses, '|', trailing whitespace --
     survive unchanged. Column mapping is required for this: without it Delta
     rejects several of these characters in a column name outright.
  4. Verify row count, column names and (for expense_report) the amount-column sum
     against the pandas frame that was uploaded.

Usage:
    set -a; source .env; set +a
    python scripts/load_tne_sources_to_uc.py
    python scripts/load_tne_sources_to_uc.py --catalog orchestrationsuite --schema tne_source --volume raw
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PACKAGE_ROOT / ".env")

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.catalog import VolumeType  # noqa: E402

SKILL_DIR = PACKAGE_ROOT / "skills" / "tne_exco"
SYNTHETIC_DATA_DIR = PACKAGE_ROOT / "synthetic_data"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(kind: str, value: str) -> None:
    if not _IDENTIFIER_RE.match(value):
        raise SystemExit(f"refusing to use {kind}={value!r} as an identifier: not a plain name")


def _quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _read_source_df(cfg: dict, path: Path) -> pd.DataFrame:
    fmt = cfg.get("format", "csv")
    if fmt == "xlsx":
        return pd.read_excel(path, sheet_name=cfg.get("sheet", 0))
    if fmt == "csv":
        return pd.read_csv(path)
    raise SystemExit(f"unsupported format {fmt!r} for {path}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SqlRunner:
    """Thin wrapper over the Statement Execution API: submits, polls until the
    statement leaves PENDING/RUNNING, and raises with the server's error message
    on failure. Mirrors the pattern in scripts/setup_workspace.py."""

    def __init__(self, w: WorkspaceClient, warehouse_id: str):
        self.w = w
        self.warehouse_id = warehouse_id

    def run(self, statement: str, *, timeout_s: float = 300.0):
        resp = self.w.statement_execution.execute_statement(
            statement=statement, warehouse_id=self.warehouse_id, wait_timeout="30s"
        )
        deadline = time.monotonic() + timeout_s
        while resp.status.state.value in ("PENDING", "RUNNING"):
            if time.monotonic() > deadline:
                raise SystemExit(f"statement timed out after {timeout_s}s: {statement[:200]}")
            time.sleep(1.0)
            resp = self.w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state.value != "SUCCEEDED":
            err = resp.status.error
            msg = f"{err.error_code}: {err.message}" if err else resp.status.state.value
            raise SystemExit(f"SQL failed: {msg}\n--- statement ---\n{statement}")
        return resp


def _rows_from_result(resp) -> list[list]:
    if resp.result is None or resp.result.data_array is None:
        return []
    return resp.result.data_array


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", default=os.environ.get("DBX_CATALOG"))
    ap.add_argument("--schema", default=os.environ.get("DBX_TNE_SCHEMA", "tne_source"))
    ap.add_argument("--volume", default=os.environ.get("DBX_TNE_VOLUME", "raw"))
    ap.add_argument(
        "--warehouse-http-path", default=os.environ.get("DBX_WAREHOUSE_HTTP_PATH")
    )
    args = ap.parse_args()

    if not args.catalog:
        raise SystemExit("--catalog or DBX_CATALOG is required")
    if not args.warehouse_http_path:
        raise SystemExit("--warehouse-http-path or DBX_WAREHOUSE_HTTP_PATH is required")

    _validate_identifier("catalog", args.catalog)
    _validate_identifier("schema", args.schema)
    _validate_identifier("volume", args.volume)

    warehouse_id = args.warehouse_http_path.rstrip("/").split("/")[-1]

    with open(SKILL_DIR / "contract.yaml") as f:
        contract = yaml.safe_load(f)
    sources: dict[str, dict] = contract["sources"]

    w = WorkspaceClient()
    sql = SqlRunner(w, warehouse_id)

    catalog, schema, volume = args.catalog, args.schema, args.volume
    schema_fqn = f"{catalog}.{schema}"
    volume_fqn = f"{schema_fqn}.{volume}"
    volume_path = f"/Volumes/{catalog}/{schema}/{volume}"

    print(f"Catalog:  {catalog}")
    print(f"Schema:   {schema_fqn}")
    print(f"Volume:   {volume_path}")
    print(f"Warehouse: {warehouse_id}\n")

    print(f"-> CREATE SCHEMA IF NOT EXISTS {schema_fqn}")
    try:
        w.schemas.create(name=schema, catalog_name=catalog, comment="T&E source tables for SKILL-001 (P5)")
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise

    print(f"-> CREATE VOLUME IF NOT EXISTS {volume_fqn}")
    try:
        w.volumes.create(
            catalog_name=catalog,
            schema_name=schema,
            name=volume,
            volume_type=VolumeType.MANAGED,
            comment="Raw Parquet staging for T&E source loads",
        )
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise

    report: list[dict] = []

    for source_name, cfg in sources.items():
        file_name = cfg["file"]
        path = SYNTHETIC_DATA_DIR / file_name
        if not path.is_file():
            raise SystemExit(f"{source_name}: file not found: {path}")

        print(f"\n=== {source_name} ({file_name}) ===")
        sha256 = _sha256_file(path)
        df = _read_source_df(cfg, path)
        n_rows = len(df)
        df.insert(0, "_source_row", range(1, n_rows + 1))

        buf = io.BytesIO()
        df.to_parquet(buf, engine="pyarrow", index=False)
        buf.seek(0)
        parquet_bytes = buf.getvalue()

        remote_parquet_path = f"{volume_path}/{source_name}.parquet"
        print(f"  uploading {len(parquet_bytes):,} bytes -> {remote_parquet_path}")
        w.files.upload(remote_parquet_path, io.BytesIO(parquet_bytes), overwrite=True)

        fqn = f"{schema_fqn}.{source_name}"
        loaded_at = pd.Timestamp.now(tz="UTC").isoformat()
        comment = (
            f"Loaded from synthetic_data/{file_name} (sha256={sha256}) at {loaded_at} "
            f"by scripts/load_tne_sources_to_uc.py"
        )
        ctas = f"""
        CREATE OR REPLACE TABLE {fqn}
        TBLPROPERTIES (
          'delta.columnMapping.mode' = 'name',
          'delta.minReaderVersion' = '2',
          'delta.minWriterVersion' = '5',
          'tne.source_file' = '{file_name}',
          'tne.source_sha256' = '{sha256}'
        )
        COMMENT '{comment.replace("'", "''")}'
        AS SELECT * EXCEPT (_rescued_data) FROM read_files('{remote_parquet_path}', format => 'parquet')
        """
        print(f"  CREATE OR REPLACE TABLE {fqn}")
        try:
            sql.run(ctas)
        except SystemExit as exc:
            print(f"  FAILED creating {fqn}: {exc}")
            raise

        for col, colcfg in cfg.get("columns", {}).items():
            if col not in df.columns:
                continue
            bits = [colcfg.get("type", "string")]
            if colcfg.get("pii"):
                bits.append("PII")
            col_comment = ", ".join(bits).replace("'", "''")
            try:
                sql.run(
                    f"ALTER TABLE {fqn} ALTER COLUMN {_quote_ident(col)} "
                    f"COMMENT '{col_comment}'"
                )
            except SystemExit as exc:
                print(f"  (column comment skipped for {col!r}: {exc})")

        count_resp = sql.run(f"SELECT count(*) AS n FROM {fqn}")
        uc_rows = int(_rows_from_result(count_resp)[0][0])

        desc_resp = sql.run(f"DESCRIBE TABLE {fqn}")
        uc_columns = [
            r[0] for r in _rows_from_result(desc_resp) if r[0] and not r[0].startswith("#")
        ]

        expected_columns = list(df.columns)
        row_check = uc_rows == n_rows
        col_check = uc_columns == expected_columns

        amount_check = None
        if source_name == "expense_report":
            amount_col = "Expense Amount (reimbursement currency)"
            pandas_sum = float(df[amount_col].sum())
            resp = sql.run(f"SELECT sum({_quote_ident(amount_col)}) AS s FROM {fqn}")
            uc_sum = float(_rows_from_result(resp)[0][0])
            amount_check = abs(pandas_sum - uc_sum) < 0.01
            print(f"  amount sum: pandas={pandas_sum:.2f} uc={uc_sum:.2f} match={amount_check}")

        print(f"  rows: file={n_rows} uc={uc_rows} match={row_check}")
        print(f"  columns match: {col_check}")
        if not col_check:
            print(f"    expected: {expected_columns}")
            print(f"    actual:   {uc_columns}")

        report.append(
            {
                "source": source_name,
                "fqn": fqn,
                "rows": uc_rows,
                "rows_match": row_check,
                "columns_match": col_check,
                "amount_match": amount_check,
                "sha256": sha256,
            }
        )

    print("\n=== Summary ===")
    ok = True
    for r in report:
        status = "OK" if r["rows_match"] and r["columns_match"] and r["amount_match"] is not False else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"  [{status}] {r['fqn']:45s} rows={r['rows']:>7}  sha256={r['sha256'][:12]}...")

    if not ok:
        raise SystemExit("\nOne or more sources failed verification -- see above.")
    print("\nAll sources loaded and verified.")


if __name__ == "__main__":
    main()
