"""The Explorer proposal validator (CLAUDE.md §4.5; docs/specs/
P6_P8_explorer_llm_design.md §4.7). Every rule id below matches the spec's
own table exactly, and that id is what a test/finding's `reasons` and the
repair round both see -- the only reference to "what went wrong" is this
file's rule ids, never a re-worded ad hoc string.

Two entry points:

- `validate_wire_proposal(wire, ...)` -- the one a caller with a freshly
  parsed planner response should use. Runs V-S1 (jsonschema against the
  wire schema) first; a structurally invalid wire proposal never reaches
  `to_canonical` (which assumes well-formed input) at all.
- `validate_proposal(canonical, ...)` -- the pure, canonical-input
  validator (V-S2 onward). Used directly by tests that hand-build a
  canonical proposal, and internally by `validate_wire_proposal` once V-S1
  has passed.

A test is valid only when it AND at least one of its own findings are
valid (§4.7 header); `_finalise` computes that closure once every other
rule has run."""

from __future__ import annotations

import re
import string
from typing import Any

import jsonschema
import pandas as pd

from orchestrator.expr import ExpressionError, compile_expr
from orchestrator.explorer.canonical import to_canonical
from orchestrator.explorer.currency import resolve_currency_unit
from orchestrator.explorer.param_walk import iter_column_params
from orchestrator.explorer.wire_schema import PLAN_PROPOSAL_SCHEMA
from orchestrator.primitives import PRIMITIVES, run_primitive
from orchestrator.primitives.common import PrimitiveContext
from orchestrator.populations import PopulationContext, build_populations

EXPLORER_VALIDATOR_VERSION = "1"

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_METRIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
_TITLE_NAME_LIMIT = 120
_PROSE_LIMIT = 800
_TITLE_NAME_FIELDS = frozenset({"skill_name", "risks.title", "controls.title", "findings.title", "tests.name"})

_PLACEHOLDER_SPAN_RE = re.compile(r"\{[a-z_][a-z0-9_]*\}")
_BACKTICK_SPAN_RE = re.compile(r"`([^`]*)`")
_DIGIT_RE = re.compile(r"[0-9]")
_FORBIDDEN_SUBSTRINGS = ("http", "SELECT ", "import ", "lambda", "__", "exec(", "eval(", "```")
_BARE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_FORMATTER = string.Formatter()

_ADDITIVE_AMOUNT_KINDS = {"sum", "value", "excess", "sum_where"}

_CAPS = {
    "tests": 15, "findings": 25, "thresholds": 25, "populations": 12,
    "risks": 15, "controls": 15,
}
_MANAGEMENT_QUESTIONS_CAP = 4


# ── small shared helpers ────────────────────────────────────────────────────


def _dtype_for(col_type: str) -> str:
    return {
        "integer": "int64", "number": "float64", "boolean": "bool",
        "date": "datetime64[ns]", "datetime": "datetime64[ns]",
    }.get(col_type, "object")


def _columns_by_name(profile: dict, source: str) -> dict[str, dict]:
    src = profile.get(source) or {}
    return {c["name"]: c for c in src.get("columns", [])}


def _profiled_value_set(colinfo: dict) -> set | None:
    if colinfo.get("pii"):
        return None
    values = colinfo.get("values")
    if values is None:
        return None
    return {v["value"] for v in values}


def _is_profiled_or_zero(value: Any, colinfo: dict) -> bool:
    """BUG-EXPLORER-PLAN-1 (independent review round 5, RUN-B68ACB9ED712):
    the wire schema's generic FILTER_VALUE type (wire_schema.py) allows an
    ARRAY `value` on every filter/condition op, not only "in"/"not_in" --
    `_validate_filter` only guards the list case for those two ops, so a
    "gt"/"gte"/"lt"/"lte"/"eq"/"ne" filter (or a metric `where`, or a
    list_membership allowed_values entry) whose `value` is itself a list
    reached `value in profiled` unguarded and raised
    TypeError("unhashable type: 'list'"), failing the whole `plan` node
    with no reason ever surfaced to the auditor (NN14). An unhashable
    `value` is never a profiled value -- reported as an ordinary V-F2/V-T4/
    V-T5 violation on that one filter/test, never a crash."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        return True
    profiled = _profiled_value_set(colinfo)
    if profiled is None:
        return False
    try:
        return value in profiled
    except TypeError:
        return False


# ── V-P1/V-P2/V-P3: prose checks ────────────────────────────────────────────


def _check_no_digits(text: str, profile_columns: set[str]) -> str | None:
    stripped = _PLACEHOLDER_SPAN_RE.sub("", text)
    stripped = _BACKTICK_SPAN_RE.sub(
        lambda m: "" if m.group(1) in profile_columns else m.group(0), stripped
    )
    if _DIGIT_RE.search(stripped):
        return f"prose contains a digit (V-P1): {text!r}"
    return None


def _check_no_code_substrings(text: str) -> str | None:
    for pat in _FORBIDDEN_SUBSTRINGS:
        if pat in text:
            return f"prose contains a forbidden substring {pat!r} (V-P3)"
    return None


def _check_prose(text: Any, *, profile_columns: set[str]) -> list[str]:
    if not isinstance(text, str):
        return [f"expected a string, got {text!r}"]
    errors = []
    d = _check_no_digits(text, profile_columns)
    if d:
        errors.append(d)
    c = _check_no_code_substrings(text)
    if c:
        errors.append(c)
    return errors


def _check_template(text: str, *, allowed_names: set[str]) -> list[str]:
    try:
        parts = list(_FORMATTER.parse(text))
    except ValueError as exc:
        return [f"invalid template syntax (V-P2): {exc}"]
    errors: list[str] = []
    for _literal, field_name, format_spec, conversion in parts:
        if field_name is None:
            continue
        if conversion is not None or format_spec:
            errors.append(f"placeholder {{{field_name}}} may not use a conversion or format spec (V-P2)")
            continue
        if not _BARE_IDENTIFIER_RE.match(field_name):
            errors.append(f"placeholder {{{field_name}}} is not a bare identifier (V-P2)")
            continue
        if field_name not in allowed_names:
            errors.append(f"placeholder {{{field_name}}} is not in metrics_cited or thresholds_cited (V-P2)")
    return errors


# ── V-F1/V-F2/V-F3: population filters ──────────────────────────────────────


def _validate_filter(flt: dict, cols_by_name: dict[str, dict], depth: int, out: list[str]) -> None:
    if depth > 3:
        out.append("filter nesting exceeds depth 3 (V-F3)")
        return
    if "any_of" in flt:
        for sub in flt["any_of"]:
            _validate_filter(sub, cols_by_name, depth + 1, out)
        return
    if "all_of" in flt:
        for sub in flt["all_of"]:
            _validate_filter(sub, cols_by_name, depth + 1, out)
        return

    col = flt.get("column")
    op = flt.get("op")
    colinfo = cols_by_name.get(col)
    if colinfo is None:
        out.append(f"filter column {col!r} is not a profile column of this population's source (V-F1)")
        return

    if op == "between":
        value = flt.get("value")
        if not (isinstance(value, dict) and value.get("ref") == "audit_period"):
            out.append("'between' is only allowed against the audit period (V-F3)")
        if colinfo["type"] not in ("date", "datetime"):
            out.append(f"'between' only allowed on a date/datetime column, {col!r} is {colinfo['type']!r} (V-F3)")
        return
    if op in ("gt", "gte", "lt", "lte"):
        if colinfo["type"] not in ("integer", "number", "date", "datetime"):
            out.append(f"{op!r} only allowed on a numeric or date column, {col!r} is {colinfo['type']!r} (V-F3)")
        value = flt.get("value")
        if not _is_profiled_or_zero(value, colinfo):
            out.append(f"filter value {value!r} on {col!r} is not a profiled value or 0 (V-F2)")
        return
    if op in ("is_null", "not_null"):
        return
    if op in ("eq", "ne", "in", "not_in"):
        value = flt.get("value")
        if op in ("in", "not_in"):
            if not isinstance(value, list):
                out.append(f"{op!r} requires a list value (V-F2)")
                return
            for v in value:
                if not _is_profiled_or_zero(v, colinfo):
                    out.append(f"filter value {v!r} on {col!r} is not a profiled value or 0 (V-F2)")
        else:
            if not _is_profiled_or_zero(value, colinfo):
                out.append(f"filter value {value!r} on {col!r} is not a profiled value or 0 (V-F2)")
        return
    out.append(f"unknown filter op {op!r}")


# ── V-T7: schema-only dry run ────────────────────────────────────────────────


def _empty_frame(cols: list[dict]) -> pd.DataFrame:
    data = {"__source": pd.Series([], dtype="object"), "__row_key": pd.Series([], dtype="object")}
    for c in cols:
        data[c["name"]] = pd.Series([], dtype=_dtype_for(c["type"]))
    return pd.DataFrame(data)


def _dry_run_populations(canonical: dict, profile: dict, source_by_pop_key: dict[str, str]) -> dict:
    amount_date_by_source = {s["source"]: s for s in canonical.get("sources", [])}
    sources_ctx = {}
    for source_name in profile:
        cols = (profile.get(source_name) or {}).get("columns", [])
        sources_ctx[source_name] = {"df": _empty_frame(cols), "version": "dry-run"}

    populations_config: dict[str, dict] = {}
    for p in canonical.get("populations", []):
        cfg: dict = {"source": p["source"], "filters": p.get("filters", [])}
        sd = amount_date_by_source.get(p["source"], {})
        if sd.get("amount_column"):
            cfg["amount_column"] = sd["amount_column"]
        if sd.get("date_column"):
            cfg["date_column"] = sd["date_column"]
        populations_config[p["key"]] = cfg

    thresholds_ctx = {t["id"]: {"value": t["value"]} for t in canonical.get("thresholds", [])}
    ctx = PopulationContext(
        sources=sources_ctx, references={}, audit_period=("2020-01-01", "2020-01-01"),
        thresholds=thresholds_ctx, custom_derivations={},
    )
    return build_populations(populations_config, ctx)


def _dry_run_test(test: dict, canonical: dict, profile: dict) -> str | None:
    try:
        populations = _dry_run_populations(canonical, profile, {})
        thresholds_ctx = {t["id"]: {"value": t["value"]} for t in canonical.get("thresholds", [])}
        prim_ctx = PrimitiveContext(populations=populations, thresholds=thresholds_ctx, references={})
        run_primitive(test["primitive"], prim_ctx, test["params"])
    except Exception as exc:  # noqa: BLE001 -- §4.7 V-T7: "Any exception invalidates the test"
        return f"schema-only dry run failed (V-T7): {exc}"
    return None


# ── V-T3: column-valued params ──────────────────────────────────────────────

_DATE_PARAM_NAMES = {"date_column", "start_column", "end_column"}
_NUMERIC_METRIC_KINDS = {"sum", "max", "excess", "distinct"}


def _validate_test_columns(test: dict, populations_by_key: dict, profile: dict, out: list[str]) -> None:
    params = test.get("params", {})
    primitive = test["primitive"]

    def _cols_for(pop_key: str | None) -> dict[str, dict] | None:
        pop = populations_by_key.get(pop_key)
        if pop is None:
            return None
        return _columns_by_name(profile, pop["source"])

    if primitive == "anti_join_gap":
        left_cols = _cols_for(params.get("left_population"))
        right_cols = _cols_for(params.get("right_population"))
        left_keys = params.get("left_keys") or []
        right_keys = params.get("right_keys") or []
        if len(left_keys) != len(right_keys):
            out.append("left_keys and right_keys must have the same length (V-T3)")
        for k in left_keys:
            if left_cols is not None and k not in left_cols:
                out.append(f"left_keys column {k!r} is not a profile column of the left population's source (V-T3)")
        for k in right_keys:
            if right_cols is not None and k not in right_cols:
                out.append(f"right_keys column {k!r} is not a profile column of the right population's source (V-T3)")
        for i, (lk, rk) in enumerate(zip(left_keys, right_keys)):
            lt = (left_cols or {}).get(lk, {}).get("type")
            rt = (right_cols or {}).get(rk, {}).get("type")
            if lt and rt and lt != rt and not ({lt, rt} <= {"integer", "number"}):
                out.append(f"left_keys[{i}]/right_keys[{i}] have incompatible types ({lt!r} vs {rt!r}) (V-T3)")
        carry = params.get("carry_right_columns") or []
        for c in carry:
            if right_cols is not None and c not in right_cols:
                out.append(f"carry_right_columns column {c!r} is not a profile column of the right population's source (V-T3)")
        return

    cols = _cols_for(params.get("population"))
    if cols is None:
        out.append("V-T3: cannot check column params -- population not resolved")
        return

    for location, col in iter_column_params(params):
        if location.startswith(("left_keys", "right_keys", "carry_right_columns")):
            continue
        if col not in cols:
            out.append(f"{location} references {col!r} which is not a profile column of this test's population source (V-T3)")
            continue
        col_type = cols[col]["type"]
        if location in _DATE_PARAM_NAMES and col_type not in ("date", "datetime"):
            out.append(f"{location} {col!r} must be a date/datetime column, is {col_type!r} (V-T3)")
        if location == "amount_column" and col_type not in ("integer", "number"):
            out.append(f"amount_column {col!r} must be numeric, is {col_type!r} (V-T3)")

    metrics = params.get("metrics") or {}
    for name, spec in metrics.items():
        if spec.get("kind") in _NUMERIC_METRIC_KINDS and spec.get("column"):
            col = spec["column"]
            colinfo = cols.get(col)
            if colinfo is not None and colinfo["type"] not in ("integer", "number"):
                out.append(
                    f"metrics.{name}.column {col!r} (kind {spec['kind']!r}) must be numeric, "
                    f"is {colinfo['type']!r} (V-T3)"
                )


# ── the top-level canonical-input validator ─────────────────────────────────


def validate_proposal(
    canonical: dict,
    *,
    profile: dict,
    run_sources: list[str],
    data_source: Any,
    pinned_versions: dict[str, Any],
) -> dict:
    """`profile` is the Explorer profile's `sources` mapping (§4.3:
    `{source_name: {"row_count", "null_counts", "columns": [...]}}`).
    `data_source` is a `DataSourceAdapter`-shaped object (only
    `.distinct_count(source, version=, columns=)` is called, for V-C3).
    `pinned_versions` is `{source_name: version}`, the same versions the
    profile itself was taken at."""

    proposal_errors: list[dict] = []
    warnings: list[str] = []
    test_reasons: dict[str, list[dict]] = {}
    finding_reasons: dict[str, list[dict]] = {}

    def add_proposal(rule: str, message: str) -> None:
        proposal_errors.append({"rule": rule, "message": message})

    def add_test(key: str, rule: str, message: str) -> None:
        test_reasons.setdefault(key, []).append({"rule": rule, "message": message})

    def add_finding(key: str, rule: str, message: str) -> None:
        finding_reasons.setdefault(key, []).append({"rule": rule, "message": message})

    for e in canonical.get("_canonicalization_errors", []):
        add_proposal("canonical", e)

    populations = canonical.get("populations", [])
    risks = canonical.get("risks", [])
    controls = canonical.get("controls", [])
    thresholds = canonical.get("thresholds", [])
    tests = canonical.get("tests", [])
    findings = canonical.get("findings", [])
    sources = canonical.get("sources", [])
    data_gaps = canonical.get("data_gaps", [])
    assumptions = canonical.get("assumptions", [])

    # ── V-S3: caps ───────────────────────────────────────────────────────
    for block_name, items in (
        ("tests", tests), ("findings", findings), ("thresholds", thresholds),
        ("populations", populations), ("risks", risks), ("controls", controls),
    ):
        if len(items) > _CAPS[block_name]:
            add_proposal("V-S3", f"{block_name} has {len(items)} entries, more than the cap of {_CAPS[block_name]}")
    for f in findings:
        if len(f.get("management_questions", [])) > _MANAGEMENT_QUESTIONS_CAP:
            add_proposal(
                "V-S3",
                f"findings.{f.get('key')}.management_questions has more than "
                f"{_MANAGEMENT_QUESTIONS_CAP} entries",
            )

    # ── V-S2: key uniqueness + format ───────────────────────────────────
    def _check_keys(items: list[dict], block: str, field: str) -> None:
        seen: set[str] = set()
        for i, item in enumerate(items):
            val = item.get(field)
            if not isinstance(val, str) or not _KEY_RE.match(val):
                add_proposal("V-S2", f"{block}[{i}].{field} = {val!r} is not a valid key (^[a-z][a-z0-9_]{{1,31}}$)")
                continue
            if val in seen:
                add_proposal("V-S2", f"duplicate {field} {val!r} in {block}")
            seen.add(val)

    _check_keys(populations, "populations", "key")
    _check_keys(risks, "risks", "key")
    _check_keys(controls, "controls", "key")
    _check_keys(thresholds, "thresholds", "id")
    _check_keys(tests, "tests", "key")
    _check_keys(findings, "findings", "key")

    # ── V-S4: sources <-> run sources <-> populations ───────────────────
    source_names = [s["source"] for s in sources]
    if len(source_names) != len(set(source_names)):
        add_proposal("V-S4", "sources[] lists the same source more than once")
    for s in source_names:
        if s not in run_sources:
            add_proposal("V-S4", f"sources references {s!r} which is not a bound run source")
    declared_sources = set(source_names)
    for p in populations:
        if p.get("source") not in declared_sources:
            add_proposal(
                "V-S4", f"populations.{p.get('key')} uses source {p.get('source')!r} not declared in sources[]"
            )

    profile_columns = {c["name"] for src in profile.values() for c in src.get("columns", [])}

    # ── back-mapping: population -> the test keys that read it ─────────
    populations_by_key = {p["key"]: p for p in populations}
    risks_by_key = {r["key"]: r for r in risks}
    controls_by_key = {c["key"]: c for c in controls}
    thresholds_by_id = {t["id"]: t for t in thresholds}

    tests_using_population: dict[str, list[str]] = {}
    tests_using_risk: dict[str, list[str]] = {}
    tests_using_control: dict[str, list[str]] = {}
    tests_using_source: dict[str, list[str]] = {}
    tests_using_threshold: dict[str, list[str]] = {}

    for t in tests:
        key = t["key"]
        for pop_param in ("population", "left_population", "right_population"):
            pop_key = t.get("params", {}).get(pop_param)
            if pop_key:
                tests_using_population.setdefault(pop_key, []).append(key)
                pop = populations_by_key.get(pop_key)
                if pop:
                    tests_using_source.setdefault(pop["source"], []).append(key)
        if t.get("risk_key"):
            tests_using_risk.setdefault(t["risk_key"], []).append(key)
        if t.get("control_key"):
            tests_using_control.setdefault(t["control_key"], []).append(key)
        for _loc, tid in _iter_threshold_refs(t.get("params", {})):
            tests_using_threshold.setdefault(tid, []).append(key)

    def _invalidate_tests_using(mapping: dict[str, list[str]], key: str, rule: str, message: str) -> None:
        for test_key in mapping.get(key, []):
            add_test(test_key, rule, message)

    # ── V-F1/V-F2/V-F3: population filters -- a violation invalidates every
    # test that reads this population ────────────────────────────────────
    for p in populations:
        cols_by_name = _columns_by_name(profile, p.get("source"))
        filter_errors: list[str] = []
        for flt in p.get("filters", []):
            _validate_filter(flt, cols_by_name, 1, filter_errors)
        for e in filter_errors:
            rule = e.rsplit("(", 1)[-1].rstrip(")") if e.endswith(")") else "V-F1"
            _invalidate_tests_using(tests_using_population, p["key"], rule, f"population {p['key']}: {e}")

    # ── V-P1/V-P3: top-level prose (skill_name/domain/summary/assumptions/
    # data_gaps) -- proposal-level, no single owning test ────────────────
    for field, value in (("skill_name", canonical.get("skill_name")), ("domain", canonical.get("domain")),
                          ("summary", canonical.get("summary"))):
        for msg in _check_prose(value, profile_columns=profile_columns):
            add_proposal("V-P1/V-P3", f"{field}: {msg}")
        if isinstance(value, str) and len(value) > (
            _TITLE_NAME_LIMIT if field in _TITLE_NAME_FIELDS else _PROSE_LIMIT
        ):
            add_proposal("V-S3", f"{field} exceeds its length cap")

    for a in assumptions:
        for msg in _check_prose(a, profile_columns=profile_columns):
            add_proposal("V-P1/V-P3", f"assumptions: {msg}")
        if isinstance(a, str) and len(a) > _PROSE_LIMIT:
            add_proposal("V-S3", "an assumptions entry exceeds the 800 character cap")

    for g in data_gaps:
        for msg in _check_prose(g.get("description"), profile_columns=profile_columns):
            add_proposal("V-P1/V-P3", f"data_gaps: {msg}")

    # ── V-P1/V-P3 on populations/risks/controls/thresholds -- invalidates
    # every test that reads that population/risk/control/threshold ─────
    for p in populations:
        for msg in _check_prose(p.get("description"), profile_columns=profile_columns):
            _invalidate_tests_using(tests_using_population, p["key"], "V-P1/V-P3", f"population {p['key']}: {msg}")

    for r in risks:
        for field in ("title", "description"):
            for msg in _check_prose(r.get(field), profile_columns=profile_columns):
                _invalidate_tests_using(tests_using_risk, r["key"], "V-P1/V-P3", f"risk {r['key']}.{field}: {msg}")
            if field == "title" and isinstance(r.get(field), str) and len(r[field]) > _TITLE_NAME_LIMIT:
                add_proposal("V-S3", f"risks.{r['key']}.title exceeds its length cap")

    for c in controls:
        for field in ("title", "description"):
            for msg in _check_prose(c.get(field), profile_columns=profile_columns):
                _invalidate_tests_using(tests_using_control, c["key"], "V-P1/V-P3", f"control {c['key']}.{field}: {msg}")
            if field == "title" and isinstance(c.get(field), str) and len(c[field]) > _TITLE_NAME_LIMIT:
                add_proposal("V-S3", f"controls.{c['key']}.title exceeds its length cap")

    # ── V-H1: threshold value bounds ─────────────────────────────────────
    threshold_valid: dict[str, bool] = {}
    for th in thresholds:
        tid = th["id"]
        ok = True
        value = th.get("value")
        unit = th.get("unit")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            ok = False
            reason = f"threshold {tid}.value {value!r} is not a finite number (V-H1)"
        elif unit == "%" and not (0 <= value <= 100):
            ok = False
            reason = f"threshold {tid}.value {value!r} out of range for unit %% (V-H1)"
        elif unit in ("days", "count") and value < 0:
            ok = False
            reason = f"threshold {tid}.value {value!r} must be >= 0 for unit {unit!r} (V-H1)"
        else:
            reason = ""
        threshold_valid[tid] = ok
        for msg in _check_prose(th.get("description"), profile_columns=profile_columns):
            _invalidate_tests_using(tests_using_threshold, tid, "V-P1/V-P3", f"threshold {tid}.description: {msg}")
        if not ok:
            _invalidate_tests_using(tests_using_threshold, tid, "V-H1", reason)
            for f in findings:
                if tid in f.get("thresholds_cited", []):
                    add_finding(f["key"], "V-H1", reason)
        if not tests_using_threshold.get(tid) and not any(
            tid in f.get("thresholds_cited", []) for f in findings
        ):
            warnings.append(f"threshold {tid} is unused -- dropped at materialisation")

    # ── V-C2: sources[].amount_column / date_column ─────────────────────
    entry_key_valid: dict[str, bool] = {}
    for s in sources:
        source = s["source"]
        cols = _columns_by_name(profile, source)
        if s.get("amount_column"):
            col = cols.get(s["amount_column"])
            if col is None or col["type"] not in ("integer", "number") or col.get("pii") \
                    or col.get("null_count", 0) != 0:
                _invalidate_tests_using(
                    tests_using_source, source, "V-C2",
                    f"sources.{source}.amount_column {s['amount_column']!r} is not a non-PII, "
                    f"fully-populated numeric column (V-C2)",
                )
        if s.get("date_column"):
            col = cols.get(s["date_column"])
            if col is None or col["type"] not in ("date", "datetime"):
                _invalidate_tests_using(
                    tests_using_source, source, "V-C2",
                    f"sources.{source}.date_column {s['date_column']!r} is not a date/datetime column (V-C2)",
                )
        # ── V-C3: entry_key ──────────────────────────────────────────────
        # CLAUDE.md independent-review decision 2026-09-24 item 1 / NN14:
        # line identity for a row-grain source is its own row identity --
        # an entry_key is required only where the grain genuinely repeats
        # (skills/tne_exco/contract.yaml's attendee_validity is the
        # reference case). A source that declares no entry_key is therefore
        # VALID for V-C3/monetary_basis purposes (row identity always
        # exists and is always unique); this branch previously marked it
        # invalid, which made every monetary_basis:spend|excess finding on
        # a plain row-grain source unconditionally fail V-N3 -- the live
        # 2026-09-25 measurement's "0/3 valid, all V-C3/N-3 unresolvable
        # entry_key" was this false positive, not a real data problem. A
        # DECLARED entry_key must still be unique at the pinned version, or
        # this still fails loudly (V-C3, below) -- that part is unchanged.
        entry_key = s.get("entry_key")
        if entry_key:
            missing = [c for c in entry_key if c not in cols]
            if missing:
                entry_key_valid[source] = False
                _invalidate_tests_using(
                    tests_using_source, source, "V-C3",
                    f"sources.{source}.entry_key references unknown column(s) {missing} (V-C3)",
                )
            elif len(entry_key) == 1:
                entry_key_valid[source] = bool(cols[entry_key[0]].get("unique"))
                if not entry_key_valid[source]:
                    _invalidate_tests_using(
                        tests_using_source, source, "V-C3",
                        f"sources.{source}.entry_key {entry_key} is not unique at the pinned version (V-C3)",
                    )
            else:
                version = pinned_versions.get(source)
                row_count = (profile.get(source) or {}).get("row_count")
                try:
                    distinct = data_source.distinct_count(source, version=version, columns=entry_key)
                except Exception as exc:  # noqa: BLE001 -- surfaced as a validation reason, not a crash
                    entry_key_valid[source] = False
                    _invalidate_tests_using(
                        tests_using_source, source, "V-C3",
                        f"sources.{source}.entry_key uniqueness check failed: {exc} (V-C3)",
                    )
                else:
                    entry_key_valid[source] = distinct == row_count
                    if not entry_key_valid[source]:
                        _invalidate_tests_using(
                            tests_using_source, source, "V-C3",
                            f"sources.{source}.entry_key {entry_key} is not unique at the pinned "
                            f"version ({distinct} distinct of {row_count} rows) (V-C3)",
                        )
        else:
            entry_key_valid[source] = True

    # ── per-test checks (V-T1, V-T2, V-T3, V-T4, V-T5, V-T6, V-T7, V-C1) ─
    test_metrics: dict[str, set[str]] = {}
    for t in tests:
        key = t["key"]
        params = t.get("params", {})
        primitive = t.get("primitive")

        for field in ("control_objective", "risk_hypothesis", "rationale", "name"):
            for msg in _check_prose(t.get(field), profile_columns=profile_columns):
                add_test(key, "V-P1/V-P3", f"{field}: {msg}")
        if isinstance(t.get("name"), str) and len(t["name"]) > _TITLE_NAME_LIMIT:
            add_test(key, "V-S3", "tests.name exceeds its length cap")
        for field in ("control_objective", "risk_hypothesis", "rationale"):
            if isinstance(t.get(field), str) and len(t[field]) > _PROSE_LIMIT:
                add_test(key, "V-S3", f"tests.{field} exceeds the 800 character cap")

        # V-T1
        if primitive not in PRIMITIVES:
            add_test(key, "V-T1", f"unknown primitive {primitive!r}")
            test_metrics[key] = set()
            continue

        # V-T2
        try:
            jsonschema.validate(params, PRIMITIVES[primitive].PARAMS_SCHEMA)
        except jsonschema.ValidationError as exc:
            add_test(key, "V-T2", f"params invalid for primitive {primitive!r}: {exc.message}")

        # V-T3
        col_errors: list[str] = []
        _validate_test_columns(t, populations_by_key, profile, col_errors)
        for e in col_errors:
            add_test(key, "V-T3", e)

        # V-T4: threshold refs + list_membership.allowed_values
        for _loc, tid in _iter_threshold_refs(params):
            if tid not in thresholds_by_id:
                add_test(key, "V-T4", f"threshold reference {tid!r} is not a proposal threshold")
        if primitive == "list_membership":
            pop = populations_by_key.get(params.get("population"))
            col = params.get("column")
            if pop is not None and col:
                colinfo = _columns_by_name(profile, pop["source"]).get(col)
                if colinfo is not None:
                    for v in params.get("allowed_values") or []:
                        if not _is_profiled_or_zero(v, colinfo):
                            add_test(key, "V-T4", f"allowed_values {v!r} is not a profiled value of {col!r}")

        # V-T5: metrics
        metrics = params.get("metrics") or {}
        if not metrics:
            add_test(key, "V-T5", "test has no metrics")
        for mname, mspec in metrics.items():
            if not _METRIC_NAME_RE.match(mname):
                add_test(key, "V-T5", f"metric name {mname!r} does not match ^[a-z][a-z0-9_]{{2,47}}$")
            if mname in thresholds_by_id:
                add_test(key, "V-T5", f"metric name {mname!r} collides with a threshold id")
            if not mspec.get("unit"):
                add_test(key, "V-T5", f"metric {mname!r} has no unit")
            where = mspec.get("where")
            if where:
                pop = populations_by_key.get(params.get("population"))
                if pop is not None:
                    colinfo = _columns_by_name(profile, pop["source"]).get(where.get("column"))
                    if colinfo is None:
                        add_test(key, "V-T5", f"metrics.{mname}.where.column {where.get('column')!r} not a profile column")
                    elif where.get("op") not in ("in", "not_in") and not _is_profiled_or_zero(where.get("value"), colinfo):
                        add_test(key, "V-T5", f"metrics.{mname}.where.value {where.get('value')!r} is not a profiled value or 0")
        all_metric_names = set(metrics)
        dupe_check: set[str] = set()
        for other in tests:
            if other["key"] == key:
                continue
            dupe_check |= set(other.get("params", {}).get("metrics") or {})
        collisions = all_metric_names & dupe_check
        if collisions:
            add_test(key, "V-T5", f"metric name(s) {sorted(collisions)} are not globally unique across the proposal")
        test_metrics[key] = all_metric_names

        # V-T6
        control = controls_by_key.get(t.get("control_key"))
        if control is None:
            add_test(key, "V-T6", f"control_key {t.get('control_key')!r} does not exist")
        elif control.get("risk_key") != t.get("risk_key"):
            add_test(key, "V-T6", "control's risk_key does not equal the test's risk_key")
        if t.get("risk_key") not in risks_by_key:
            add_test(key, "V-T6", f"risk_key {t.get('risk_key')!r} does not exist")
        if t.get("assertion") != "operating":
            add_test(key, "V-T6", "assertion must be 'operating' -- the platform does not do design assessment")

        # V-C1: currency
        pop = populations_by_key.get(params.get("population")) or populations_by_key.get(params.get("left_population"))
        if pop is not None:
            currency_needed = any(m.get("unit") == "currency" for m in metrics.values()) or any(
                thresholds_by_id.get(tid, {}).get("unit") == "currency" for _loc, tid in _iter_threshold_refs(params)
            )
            if currency_needed and resolve_currency_unit(pop["source"], profile) is None:
                add_test(key, "V-C1", "currency not evidenced -- amounts cannot be summed (CLAUDE.md §0.5)")

        # V-T7: schema-only dry run (only when structurally sane enough to attempt)
        if not test_reasons.get(key) and primitive in PRIMITIVES:
            dry_error = _dry_run_test(t, canonical, profile)
            if dry_error:
                add_test(key, "V-T7", dry_error)

    # ── per-finding checks (V-N1, V-N2, V-N3, V-P1/P2/P3) ────────────────
    for f in findings:
        key = f["key"]
        test = next((t for t in tests if t["key"] == f.get("test_key")), None)
        if test is None:
            add_finding(key, "V-N1", f"test_key {f.get('test_key')!r} does not exist")
            continue

        this_test_metrics = test_metrics.get(test["key"], set())
        cited = set(f.get("metrics_cited", []))
        unknown_cited = cited - this_test_metrics
        if unknown_cited:
            add_finding(key, "V-N1", f"metrics_cited {sorted(unknown_cited)} not produced by this finding's own test")

        cited_thresholds = set(f.get("thresholds_cited", []))
        unknown_thresholds = cited_thresholds - set(thresholds_by_id)
        if unknown_thresholds:
            add_finding(key, "V-N1", f"thresholds_cited {sorted(unknown_thresholds)} not a proposal threshold")

        try:
            compile_expr(f["trigger"], known_metrics=this_test_metrics, known_thresholds=set(thresholds_by_id))
        except ExpressionError as exc:
            add_finding(key, "V-N2", f"trigger invalid: {exc}")

        for i, rule in enumerate(f.get("severity", [])):
            if "when" in rule:
                try:
                    compile_expr(rule["when"], known_metrics=this_test_metrics, known_thresholds=set(thresholds_by_id))
                except ExpressionError as exc:
                    add_finding(key, "V-N2", f"severity[{i}].when invalid: {exc}")

        metrics_spec = test.get("params", {}).get("metrics") or {}
        has_amount_metric = any(
            name in cited and metrics_spec.get(name, {}).get("kind") in _ADDITIVE_AMOUNT_KINDS
            and metrics_spec.get(name, {}).get("unit") == "currency"
            for name in cited
        )
        monetary_basis = f.get("monetary_basis")
        if monetary_basis == "none" and has_amount_metric:
            add_finding(key, "V-N3", "monetary_basis is 'none' but metrics_cited includes a currency amount metric")
        if monetary_basis in ("spend", "excess") and not has_amount_metric:
            add_finding(key, "V-N3", f"monetary_basis is {monetary_basis!r} but metrics_cited has no currency amount metric")
        if monetary_basis in ("spend", "excess"):
            pop = populations_by_key.get(test.get("params", {}).get("population"))
            source = pop["source"] if pop else None
            if source is None or not entry_key_valid.get(source):
                add_finding(
                    key, "V-N3",
                    f"monetary_basis {monetary_basis!r} requires a valid entry_key on the population's "
                    f"source (V-C3)",
                )

        for field in ("title", "observation", "recommendation"):
            for msg in _check_prose(f.get(field), profile_columns=profile_columns):
                add_finding(key, "V-P1/V-P3", f"{field}: {msg}")
        if isinstance(f.get("title"), str) and len(f["title"]) > _TITLE_NAME_LIMIT:
            add_finding(key, "V-S3", "findings.title exceeds its length cap")
        for field in ("observation", "recommendation"):
            if isinstance(f.get(field), str) and len(f[field]) > _PROSE_LIMIT:
                add_finding(key, "V-S3", f"findings.{field} exceeds the 800 character cap")
        for q in f.get("management_questions", []):
            for msg in _check_prose(q, profile_columns=profile_columns):
                add_finding(key, "V-P1/V-P3", f"management_questions: {msg}")

        allowed_names = cited | cited_thresholds
        for field in ("observation", "recommendation"):
            text = f.get(field)
            if isinstance(text, str):
                for msg in _check_template(text, allowed_names=allowed_names):
                    add_finding(key, "V-P2", f"{field}: {msg}")

    # ── V-T8 + finalisation ──────────────────────────────────────────────
    findings_report: dict[str, dict] = {}
    for f in findings:
        key = f["key"]
        reasons = finding_reasons.get(key, [])
        findings_report[key] = {"valid": not reasons, "reasons": reasons}

    tests_report: dict[str, dict] = {}
    for t in tests:
        key = t["key"]
        own_reasons = list(test_reasons.get(key, []))
        has_valid_finding = any(
            f.get("test_key") == key and findings_report.get(f["key"], {}).get("valid")
            for f in findings
        )
        if not has_valid_finding:
            own_reasons.append({"rule": "V-T8", "message": "no finding rule -- exceptions would not be written up (§4.5)"})
        tests_report[key] = {"valid": not own_reasons, "reasons": own_reasons}

    return {
        "proposal_errors": proposal_errors,
        "tests": tests_report,
        "findings": findings_report,
        "warnings": warnings,
    }


def _iter_threshold_refs(params: dict):
    """Yields `(location, threshold_id)` for every `{"threshold": id}`
    reference in a test's params (§4.7 V-T4)."""
    for key in ("limit", "threshold", "window_days", "aggregate_threshold", "max_line"):
        value = params.get(key)
        if isinstance(value, dict) and isinstance(value.get("threshold"), str):
            yield key, value["threshold"]


def check_wire_schema(wire: dict) -> list[str]:
    """V-S1: `jsonschema` against the wire schema. Returns error messages,
    or an empty list when `wire` conforms."""
    validator = jsonschema.Draft202012Validator(PLAN_PROPOSAL_SCHEMA)
    return [f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in validator.iter_errors(wire)]


def _check_kind_matches_primitive(wire: dict) -> list[tuple[str, str]]:
    """A defensive, WIRE-level cross-check (V-T1's `params.kind ==
    primitive`) -- `to_canonical` drops `params.kind` (it is not a real
    primitive parameter, orchestrator.primitives' own PARAMS_SCHEMAs would
    reject it), so this can only run before canonicalisation. Returns
    `(test_key, message)` pairs."""
    out: list[tuple[str, str]] = []
    for t in wire.get("tests") or []:
        kind = (t.get("params") or {}).get("kind")
        primitive = t.get("primitive")
        if kind is not None and kind != primitive:
            out.append((t.get("key", "?"), f"params.kind {kind!r} does not match primitive {primitive!r} (V-T1)"))
    return out


def validate_wire_proposal(
    wire: dict,
    *,
    profile: dict,
    run_sources: list[str],
    data_source: Any,
    pinned_versions: dict[str, Any],
) -> dict:
    """The recommended entry point (§4.8's `report = validate(to_canonical(
    proposal))`): V-S1 first, against the raw wire form; only a
    structurally sound proposal is canonicalised and passed to
    `validate_proposal`."""
    s1_errors = check_wire_schema(wire)
    if s1_errors:
        return {
            "proposal_errors": [{"rule": "V-S1", "message": m} for m in s1_errors],
            "tests": {}, "findings": {}, "warnings": [],
        }

    canonical = to_canonical(wire)
    report = validate_proposal(
        canonical, profile=profile, run_sources=run_sources, data_source=data_source,
        pinned_versions=pinned_versions,
    )
    for test_key, message in _check_kind_matches_primitive(wire):
        entry = report["tests"].setdefault(test_key, {"valid": True, "reasons": []})
        entry["reasons"].append({"rule": "V-T1", "message": message})
        entry["valid"] = False
    return report
