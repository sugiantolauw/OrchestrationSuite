"""Explorer Mode profiling (docs/specs/P6_P8_explorer_llm_design.md §4.3,
§4.3.1). Two layers, deliberately kept separate:

1. Each `DataSourceAdapter.profile_columns()` (orchestrator/adapters/
   protocols.py) computes RAW per-column statistics -- it has no idea what
   PII is, only how to count, min/max and bucket values. `pandas_profile_
   columns` below is the one shared implementation LocalFileDataSource,
   VolumeUploadAwareDataSource (flat-file sources) and UCTableDataSource all
   call, over the same pandas population a Playbook run would already read
   (CLAUDE.md §2.3 rule 4's cell ceiling already guards that read) --
   documented simplification vs. this spec's "SQL pushdown for UC" phrasing:
   correct aggregates, same bounded-memory guarantee, one implementation
   instead of two that could drift, exactly the trade-off the existing
   Playbook profile() node already documents for the same reason.

2. `profile_source()` here is the ORCHESTRATION: call a source's
   profile_columns(), classify each column's PII status (§4.3.1), and mask
   every PII column's value-revealing fields (min/max/values/
   negative_count/zero_count/in_period_count) BEFORE the result is handed
   back -- so a caller that stores this in RunState.profile_result or a
   prompt payload can never leak a PII value, by construction (G15), not by
   remembering to redact it later."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from orchestrator.explorer.iso4217 import ISO4217_CODES

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SKILLS_DIR = _REPO_ROOT / "skills"

# CLAUDE.md §4.1 / orchestrator.adapters.datasource_uc, datasource_volume_upload,
# orchestrator.contract.parse_source_bytes: every population frame carries these
# two bookkeeping columns. Never profiled as data.
_BOOKKEEPING_COLUMNS = {"__source", "__row_key"}

_AMOUNT_TOKENS = ("amount", "amt", "value", "cost", "price", "total", "spend", "gst", "tax", "fee")
_PERSON_TOKENS = ("name", "dob", "birth")
_CONTACT_TOKENS = ("email", "phone", "mobile", "address")
_FLAG_VALUE_SETS = ({"y", "n"}, {"yes", "no"}, {"true", "false"}, {"0", "1"})

# §4.3.1 rule 3.
_PII_HEURISTIC_TOKENS = (
    "name", "email", "phone", "mobile", "address", "employee", "emp", "staff",
    "person", "traveller", "attendee", "approver", "manager", "dob", "birth",
    "tfn", "abn", "account", "bsb", "card", "passport", "licence", "license",
)


# ── type + semantic-type inference (§4.3) ───────────────────────────────────


def infer_column_type(series: pd.Series) -> str:
    """`"string" | "integer" | "number" | "date" | "datetime" | "boolean"`,
    from the pandas dtype (plus one refinement: a datetime64 column whose
    every non-null value has a midnight time component is "date", not
    "datetime" -- pandas has no separate date64 dtype). A column that is
    entirely null, or genuinely mixed-type text, is "string": conservative,
    never guessed (this only affects what the planner is told). Known
    consequence: a CSV-sourced date column arrives as plain text
    (pandas.read_csv never infers dates on its own) and profiles as
    "string", with no min/max/in_period_count -- an Excel date cell
    round-trips as a real datetime64 dtype and profiles correctly. A UC
    TIMESTAMP/DATE column is unaffected either way, since its dtype is
    already native."""
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(series):
        non_null = series.dropna()
        if len(non_null) and bool((non_null.dt.normalize() == non_null).all()):
            return "date"
        return "datetime"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "number"
    return "string"


def _is_currency_code_column(non_null: pd.Series, max_check: int) -> bool:
    uniques = non_null.unique()
    if len(uniques) == 0 or len(uniques) > max_check:
        return False
    for v in uniques:
        if not isinstance(v, str) or v.strip().upper() not in ISO4217_CODES:
            return False
    return True


def _is_flag_like(two_value_set: set[str] | None) -> bool:
    if two_value_set is None:
        return False
    return any(two_value_set == s for s in _FLAG_VALUE_SETS)


def classify_semantic_type(
    *, name: str, col_type: str, distinct_count: int, row_count: int, unique: bool,
    is_currency_code: bool, max_distinct: int, two_value_set: set[str] | None = None,
) -> str:
    """§4.3 "Semantic type rules, first match wins"."""
    lname = name.lower()
    if col_type in ("date", "datetime"):
        return "date"
    if col_type == "string" and is_currency_code:
        return "currency_code"
    if col_type == "boolean" or (distinct_count == 2 and _is_flag_like(two_value_set)):
        return "flag"
    if unique:
        return "identifier"
    if col_type in ("integer", "number") and any(t in lname for t in _AMOUNT_TOKENS):
        return "amount"
    if distinct_count <= max_distinct:
        return "category"
    if any(t in lname for t in _PERSON_TOKENS):
        return "person"
    if any(t in lname for t in _CONTACT_TOKENS):
        return "contact"
    if col_type == "string" and row_count > 0 and (distinct_count / row_count) > 0.5:
        return "free_text"
    return "other"


# ── scalar/value serialisation ──────────────────────────────────────────────


def _scalar_to_jsonable(value: Any, *, date_only: bool = False) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat() if date_only else value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def _value_counts(non_null: pd.Series, *, min_count: int, date_only: bool) -> tuple[list[dict], int]:
    counts = non_null.value_counts()
    items = [(v, int(c)) for v, c in counts.items()]
    # §4.3: "values sorted by (-count, value)".
    items.sort(key=lambda vc: (-vc[1], str(vc[0])))
    kept = [
        {"value": _scalar_to_jsonable(v, date_only=date_only), "count": c}
        for v, c in items if c >= min_count
    ]
    suppressed = sum(1 for _, c in items if c < min_count)
    return kept, suppressed


def _in_period_count(non_null: pd.Series, audit_period: tuple[str, str]) -> int:
    start, end = audit_period
    dates = pd.to_datetime(non_null).dt.normalize()
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    return int(((dates >= start_ts) & (dates <= end_ts)).sum())


# ── DataSourceAdapter.profile_columns' one shared implementation ───────────


def pandas_profile_columns(
    df: pd.DataFrame,
    *,
    max_distinct: int,
    min_count: int,
    audit_period: tuple[str, str] | None = None,
    audit_timezone: str | None = None,
) -> dict:
    """RAW per-column statistics (no PII awareness -- see this module's own
    docstring). Shape: `{"row_count", "null_counts", "columns": [...]}` --
    the first two keys are kept Playbook-compatible (§4.3: "profile_result
    for Explorer ... keeps the Playbook-compatible row_count/null_counts
    keys")."""
    columns = [c for c in df.columns if c not in _BOOKKEEPING_COLUMNS]
    row_count = len(df)
    result_columns: list[dict] = []

    for name in columns:
        series = df[name]
        null_count = int(series.isna().sum())
        non_null = series.dropna()
        distinct_count = int(non_null.nunique())
        col_type = infer_column_type(series)
        unique = distinct_count == row_count and null_count == 0

        is_currency = col_type == "string" and _is_currency_code_column(
            non_null, max(max_distinct, 50)
        )
        two_value_set: set[str] | None = None
        if distinct_count == 2 and col_type != "boolean":
            two_value_set = {str(v).strip().lower() for v in non_null.unique()}

        semantic_type = classify_semantic_type(
            name=name, col_type=col_type, distinct_count=distinct_count, row_count=row_count,
            unique=unique, is_currency_code=is_currency, max_distinct=max_distinct,
            two_value_set=two_value_set,
        )

        col: dict = {
            "name": name, "type": col_type, "null_count": null_count,
            "distinct_count": distinct_count, "unique": unique, "semantic_type": semantic_type,
        }

        if col_type in ("integer", "number") and len(non_null):
            col["negative_count"] = int((non_null < 0).sum())
            col["zero_count"] = int((non_null == 0).sum())
            col["min"] = _scalar_to_jsonable(non_null.min())
            col["max"] = _scalar_to_jsonable(non_null.max())
        if col_type in ("date", "datetime") and len(non_null):
            date_only = col_type == "date"
            col["min"] = _scalar_to_jsonable(non_null.min(), date_only=date_only)
            col["max"] = _scalar_to_jsonable(non_null.max(), date_only=date_only)
            if audit_period and audit_timezone:
                col["in_period_count"] = _in_period_count(non_null, audit_period)

        if distinct_count <= max_distinct:
            values, suppressed = _value_counts(
                non_null, min_count=min_count, date_only=(col_type == "date")
            )
            col["values"] = values
            col["suppressed_values"] = suppressed

        result_columns.append(col)

    null_counts = {c["name"]: c["null_count"] for c in result_columns}
    return {"row_count": row_count, "null_counts": null_counts, "columns": result_columns}


def pandas_distinct_count(df: pd.DataFrame, columns: list[str]) -> int:
    """`COUNT(DISTINCT col1, col2, ...)` over a pandas population -- V-C3's
    composite-`entry_key` uniqueness check is `this == row_count`."""
    return int(df[columns].dropna().drop_duplicates().shape[0]) if columns else 0


# ── §4.3.1 PII classification ───────────────────────────────────────────────

_VALUE_REVEALING_FIELDS = (
    "min", "max", "negative_count", "zero_count", "in_period_count", "values", "suppressed_values",
)


def load_repo_pii_flags(skills_dir: str | Path | None = None) -> set[tuple[str, str]]:
    """Every `(source_name, column_name)` any repo Skill's contract.yaml
    declares `pii: true` for (§4.3.1 rule 1) -- Explorer reuses these flags
    when a run happens to bind the same source name a repo Skill also uses.
    A Skill whose contract.yaml fails to parse is skipped (best-effort
    auxiliary signal, not a contract check in its own right); it never
    raises, because that would make an unrelated Skill's YAML error block
    profiling on a run that never touches it."""
    base = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
    flags: set[tuple[str, str]] = set()
    if not base.is_dir():
        return flags
    for contract_path in sorted(base.glob("*/contract.yaml")):
        try:
            data = yaml.safe_load(contract_path.read_text()) or {}
        except Exception:  # noqa: BLE001 -- best-effort auxiliary signal, see docstring
            continue
        for source_name, source_cfg in (data.get("sources") or {}).items():
            for col_name, col_cfg in ((source_cfg or {}).get("columns") or {}).items():
                if isinstance(col_cfg, dict) and col_cfg.get("pii"):
                    flags.add((source_name, col_name))
    return flags


def classify_pii(
    *, source_name: str, column_name: str, semantic_type: str,
    repo_pii_flags: set[tuple[str, str]], uc_tags: dict[str, list[str]] | None,
    pii_tag_names: tuple[str, ...] | list[str] | None,
) -> tuple[bool, str | None, str | None]:
    """`(pii, pii_basis, pii_basis_note)`. The first rule that marks a column
    PII wins (§4.3.1); masking more is always safe, so this never tries to
    be clever about un-marking one."""
    if (source_name, column_name) in repo_pii_flags:
        return True, "contract", None

    pii_basis_note: str | None = None
    if uc_tags is None:
        pii_basis_note = "tags unreadable"
    else:
        tags = {t.lower() for t in uc_tags.get(column_name, [])}
        wanted = {t.lower() for t in (pii_tag_names or ())}
        if tags & wanted:
            return True, "uc_tag", None

    lname = column_name.lower()
    if any(t in lname for t in _PII_HEURISTIC_TOKENS) or semantic_type in ("person", "contact", "free_text"):
        return True, "heuristic", pii_basis_note
    return False, None, pii_basis_note


def mask_pii_column(col: dict, *, pii_basis: str | None, pii_basis_note: str | None) -> dict:
    """Strips every value-revealing field (§4.3: "PII columns expose ONLY
    name, type, null_count, distinct_count, unique, pii: true and
    pii_basis")."""
    masked = {
        "name": col["name"], "type": col["type"], "null_count": col["null_count"],
        "distinct_count": col["distinct_count"], "unique": col["unique"],
        "semantic_type": col["semantic_type"], "pii": True, "pii_basis": pii_basis,
    }
    if pii_basis_note:
        masked["pii_basis_note"] = pii_basis_note
    # Belt-and-braces: even if a future field is added to the raw profile
    # without this module being updated, an explicit denylist copy (above)
    # rather than "copy everything except X" means a new value-revealing
    # field is masked BY DEFAULT, never leaked by omission.
    for field in _VALUE_REVEALING_FIELDS:
        assert field not in masked, f"masked PII column must never carry {field!r}"
    return masked


def profile_source(
    data_source: Any,
    source: str,
    *,
    version: int | str,
    max_distinct: int,
    min_count: int,
    audit_period: tuple[str, str] | None = None,
    audit_timezone: str | None = None,
    repo_pii_flags: set[tuple[str, str]] | None = None,
    pii_tag_names: tuple[str, ...] | list[str] | None = None,
) -> dict:
    """The orchestration `discover`/`profile`'s Explorer branch calls (§4.3):
    raw stats from `data_source.profile_columns()`, PII classification and
    masking applied on top -- what this returns is safe to store in
    RunState.profile_result and to serialise into a planner prompt (G15)."""
    raw = data_source.profile_columns(
        source, version=version, max_distinct=max_distinct, min_count=min_count,
        audit_period=audit_period, audit_timezone=audit_timezone,
    )
    column_tags_fn = getattr(data_source, "column_tags", None)
    uc_tags = column_tags_fn(source, version=version) if column_tags_fn is not None else None
    flags = repo_pii_flags if repo_pii_flags is not None else load_repo_pii_flags()

    columns: list[dict] = []
    for col in raw["columns"]:
        pii, basis, note = classify_pii(
            source_name=source, column_name=col["name"], semantic_type=col["semantic_type"],
            repo_pii_flags=flags, uc_tags=uc_tags, pii_tag_names=pii_tag_names,
        )
        if pii:
            columns.append(mask_pii_column(col, pii_basis=basis, pii_basis_note=note))
        else:
            entry = {**col, "pii": False, "pii_basis": None}
            if note:
                entry["pii_basis_note"] = note
            columns.append(entry)

    return {"row_count": raw["row_count"], "null_counts": raw["null_counts"], "columns": columns}
