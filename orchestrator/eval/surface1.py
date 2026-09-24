"""Surface 1 (CLAUDE.md §5 Tier B): the correctness gate. Compares a
COMPLETED run's own persisted flagged exceptions and metrics -- read back
through PersistenceAdapter exactly as `/trace`/`/runs` do, never re-derived
from raw data (fieldwork.execute's durable outputs, CLAUDE.md §4.2) -- to a
hand-verified, auditor-confirmed exception list, at the SAME scoring unit
docs/specs/SKILL-001_test_specification.md §3 declares per test.

This is the CORRECTNESS gate: it proves the engine produces the right
answers. It is not the `computation.py` regression comparison (CLAUDE.md §5
Tier B), and it is not Surface 2 (orchestrator/eval/surface2.py), which
scores against a PLANTED synthetic fixture the generator itself declares --
Surface 1's oracle is real audit judgement, independent of any generator
(CLAUDE.md §9: "never use a generator as its own test oracle").

The oracle never enters this development environment as real data (CLAUDE.md
§9, §11 "Further decisions... Deferred to the corporate migration: the
Surface 1 auditor-confirmed exception list"). This module, `docs/templates/
surface1_oracle_template.csv` and `scripts/run_surface1.py` exist so that
list can be dropped in at the corporate workspace with NO code change here --
only `--run-id ID --oracle PATH` on the command line. Nothing in this module
is SKILL-001-specific in its MECHANICS (the oracle format, the comparison
algorithm, the report shape); `CATALOGUE_TESTS` below is the one
SKILL-001-specific piece -- the same shape orchestrator/eval/surface2.py's
own per-test tables already are.

## Oracle file format

A long/tidy CSV or XLSX, one row per (exception identity field) or (expected
metric value) -- documented in full, with two illustrative synthetic rows,
in docs/templates/surface1_oracle_template.csv. Columns (by name, any order,
extra columns ignored):

    record_type      "exception" | "metric"
    test_id          one of the 13 scorable catalogue test ids (T3.1a,
                      T3.1b, T3.2a, T3.3a, T3.3b, T4.1, T4.2, T4.4, T5.1,
                      T5.2, T6.1a, T6.1c, T6.1d) -- or "RUN" for a
                      record_type=metric row about a run-level metric not
                      tied to one test (e.g. run_exposure_headline).
                      T4.3 is a KNOWN name that is never scorable (CLAUDE.md
                      §11 "T4.3 stays not_testable") -- the oracle must not
                      reference it; doing so fails the run loudly rather
                      than silently skipping those rows.
    exception_id      record_type=exception only: the auditor's own label
                      for one confirmed exception or confirmed-clean
                      sample, unique within (test_id). A group- or
                      member-grain test's identity is declared across
                      SEVERAL rows sharing one exception_id (one row per
                      identity_field).
    expected          record_type=exception only: "true" (a confirmed
                      exception) or "false" (a confirmed-clean sample --
                      REQUIRED for a meaningful false-positive rate; see
                      CLAUDE.md §9 "~100 rows, metrics worked independently
                      by a human"). Every row sharing one exception_id must
                      agree.
    identity_field    record_type=exception only: which declared field this
                      row supplies, e.g. "Booking ID" (T3.1b, T3.3a),
                      "Travel Request ID" (T3.1a), "Approval Step" (T6.1a),
                      "Row Number" (T3.2a, T4.1, T4.2, T4.4, T6.1c, and
                      T5.1's "member" rows), or one of a group test's own
                      declared key columns (T3.3b, T5.2, T6.1d). See
                      CATALOGUE_TESTS / the template for the exact set each
                      test_id requires. "Row Number" is the 1-based data-row
                      number within the run's own PINNED source file
                      (CLAUDE.md docs/specs/SKILL-001_test_specification.md
                      §0 "Row identity": (source_sha256, row_number) -- the
                      sha256 is recorded in this report's source_versions so
                      the auditor can confirm they are looking at the same
                      file cut this run read) -- used for the two sources
                      (expense_report, attendee_validity) with no single
                      natural per-line business key.
    identity_value    record_type=exception only: the value for
                      identity_field.
    metric_name       record_type=metric only: a run_metrics name.
    expected_value    record_type=metric only: the auditor's independently
                      worked value.

Validated strictly at load: unknown record_type, unknown test_id, a missing
or extra identity_field for a test's declared grain, an exception_id
declaring inconsistent `expected` values, a duplicate (test_id, exception_id,
identity_field), two exception_ids resolving to the identical scoring unit,
and a duplicate (test_id, metric_name) all fail loudly (Surface1OracleError)
-- CLAUDE.md non-negotiable 14, no fuzzy matching, no silent defaults.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from openpyxl import Workbook

from orchestrator.contract import validate_contract
from orchestrator.primitives.common import keyed_unit_id, member_group_id
from orchestrator.skills import plan_test_flags

# ── oracle errors ────────────────────────────────────────────────────────────


class Surface1OracleError(Exception):
    """Raised for any structural problem with the oracle file, or an oracle
    entry that cannot be scored (an unknown/unscorable test_id, an
    out-of-range Row Number, ...). Always fails loudly -- never silently
    dropped (CLAUDE.md non-negotiable 14)."""

    def __init__(self, violations: list[str]):
        self.violations = list(violations)
        super().__init__("; ".join(self.violations))


class Surface1RunError(Exception):
    """The run itself is not in a state Surface 1 can score (not yet
    executed, or otherwise unreadable)."""


# ── per-test scoring-unit configuration (SKILL-001-specific) ────────────────


@dataclass(frozen=True)
class RowTest:
    """Row-grain scoring unit: one flagged source row is one exception.

    `id_kind` is "business_id" (a real, declared contract column identifies
    the row on its own -- Booking ID, Travel Request ID, or T6.1a's
    Report ID|Step|Approver ID composite) or "row_number" (the source has no
    such column -- expense_report and attendee_validity -- so the oracle
    declares the row's 1-based position in the pinned source file instead,
    per this module's own docstring)."""

    plan_test_ids: tuple[str, ...]
    source: str
    scoring_unit: str
    id_kind: str
    id_field: str
    natural_id_fn: Callable[[pd.Series], str] | None = None


@dataclass(frozen=True)
class KeyGroupTest:
    """Group-grain scoring unit identified by a natural key tuple -- the
    SAME `kind` string and column order
    orchestrator.primitives.common.keyed_unit_id used when the engine
    computed this test's own group_id, so hashing the oracle's declared key
    values reproduces the identical id with no population read needed."""

    plan_test_ids: tuple[str, ...]
    scoring_unit: str
    kind: str
    fields: tuple[tuple[str, str], ...]  # (identity_field name, coercion: int|float|date|str)


@dataclass(frozen=True)
class MemberGroupTest:
    """Group-grain scoring unit identified by row MEMBERSHIP -- the SAME
    `kind` string orchestrator.primitives.common.member_group_id used
    (T5.1's split-claim groups, CLAUDE.md build brief B3)."""

    plan_test_ids: tuple[str, ...]
    scoring_unit: str
    kind: str
    member_source: str
    member_id_field: str


def _natural_id_travel_request(row: pd.Series) -> str:
    return str(row["Travel Request ID"])


def _natural_id_booking(row: pd.Series) -> str:
    return str(int(row["Booking ID"]))


def _natural_id_approval_step(row: pd.Series) -> str:
    return f"{row['Report ID']}|{row['Step']}|{row['Approver ID']}"


# docs/specs/SKILL-001_test_specification.md §3 ("Scoring units for Surface
# 2") gives the same 13 scorable catalogue tests and the same scoring units
# -- this table is Surface 1's own version of it, at the SAME granularity
# the auditor was given the spec at (the 14 catalogue tests, T4.3 excluded
# since it is never scorable), not plan.yaml's finer sub-test ids. A
# catalogue test spanning several plan.yaml sub-tests (T3.2a, T3.3a, T6.1d)
# is scored on the UNION of its sub-tests' flagged units -- a set union, so
# this never double-counts.
CATALOGUE_TESTS: dict[str, RowTest | KeyGroupTest | MemberGroupTest] = {
    "T3.1a": RowTest(
        ("T3.1a",), "travel_requests_no_expense", "travel request",
        "business_id", "Travel Request ID", _natural_id_travel_request,
    ),
    "T3.1b": RowTest(
        ("T3.1b",), "booking_detail", "booking",
        "business_id", "Booking ID", _natural_id_booking,
    ),
    "T3.2a": RowTest(
        ("T3.2a_air_dom", "T3.2a_air_int", "T3.2a_car_dom", "T3.2a_car_int", "T3.2a_accom"),
        "expense_report", "expense line", "row_number", "Row Number",
    ),
    "T3.3a": RowTest(
        ("T3.3a_dom", "T3.3a_int", "T3.3a_very_late"), "booking_detail", "booking",
        "business_id", "Booking ID", _natural_id_booking,
    ),
    "T3.3b": KeyGroupTest(
        ("T3.3b",), "entertainment entry", "ratio",
        (("Employee ID", "int"), ("Report Name", "str"), ("Transaction Date", "date"),
         ("Vendor", "str"), ("Entry Amount", "float")),
    ),
    "T4.1": RowTest(("T4.1",), "expense_report", "expense line", "row_number", "Row Number"),
    "T4.2": RowTest(("T4.2",), "expense_report", "expense line", "row_number", "Row Number"),
    "T4.4": RowTest(("T4.4",), "expense_report", "expense line", "row_number", "Row Number"),
    "T5.1": MemberGroupTest(("T5.1",), "claim group", "claim", "expense_report", "Row Number"),
    "T5.2": KeyGroupTest(
        ("T5.2",), "claim group", "dup",
        (("Employee ID", "int"), ("Transaction Date", "date"), ("Vendor", "str"), ("Amount", "float")),
    ),
    "T6.1a": RowTest(
        ("T6.1a",), "approval_aging", "approval step",
        "business_id", "Approval Step", _natural_id_approval_step,
    ),
    "T6.1c": RowTest(("T6.1c",), "attendee_validity", "attendee row", "row_number", "Row Number"),
    "T6.1d": KeyGroupTest(
        ("T6.1d_dom", "T6.1d_int"), "employee-day-country", "thr",
        (("Employee ID", "int"), ("Transaction Date", "date"), ("country", "str")),
    ),
}

# T4.3 is a recognised name that is deliberately absent from CATALOGUE_TESTS
# above (CLAUDE.md §11: "T4.3 stays not_testable... until approved") -- kept
# here only so an oracle referencing it gets an explanatory error rather than
# a bare "unknown test_id".
_T4_3_REASON = (
    "T4.3 is not_testable for every SKILL-001 run today (classification "
    "requires model-serving governance approval that has not been granted, "
    "CLAUDE.md §11) and is never scored -- remove it from the oracle"
)

_RUN_LEVEL_TEST_ID = "RUN"


def _field_names_for(cfg: RowTest | KeyGroupTest | MemberGroupTest) -> tuple[str, ...]:
    if isinstance(cfg, RowTest):
        return (cfg.id_field,)
    if isinstance(cfg, KeyGroupTest):
        return tuple(name for name, _ in cfg.fields)
    return (cfg.member_id_field,)


# ── oracle parsing ───────────────────────────────────────────────────────────

_REQUIRED_COLUMNS = (
    "record_type", "test_id", "exception_id", "expected",
    "identity_field", "identity_value", "metric_name", "expected_value",
)


@dataclass
class OracleException:
    exception_id: str
    expected: bool
    fields: dict[str, str] = field(default_factory=dict)  # identity_field -> raw string value
    members: list[str] = field(default_factory=list)  # T5.1 only: raw "Row Number" strings, in file order


@dataclass
class OracleDocument:
    sha256: str
    exceptions: dict[str, dict[str, OracleException]] = field(default_factory=dict)  # test_id -> exception_id -> ...
    metrics: dict[str, dict[str, str]] = field(default_factory=dict)  # test_id -> metric_name -> raw expected_value


def _read_oracle_frame(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
    elif suffix == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    else:
        raise Surface1OracleError([f"oracle file {path}: unsupported extension {suffix!r} (expected .csv or .xlsx)"])
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise Surface1OracleError([f"oracle file {path}: missing required column(s) {missing}"])
    return df


def _parse_bool(raw: str, *, where: str) -> bool:
    v = raw.strip().lower()
    if v in ("true", "1"):
        return True
    if v in ("false", "0"):
        return False
    raise Surface1OracleError([f"{where}: expected column must be 'true' or 'false' (got {raw!r})"])


def load_oracle(path: str | Path) -> OracleDocument:
    path = Path(path)
    if not path.is_file():
        raise Surface1OracleError([f"oracle file not found: {path}"])
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    df = _read_oracle_frame(path)

    violations: list[str] = []
    exceptions: dict[str, dict[str, OracleException]] = {}
    metrics: dict[str, dict[str, str]] = {}
    seen_metric_keys: set[tuple[str, str]] = set()

    for i, row in df.iterrows():
        line = i + 2  # 1-based data row, plus the header row
        record_type = (row["record_type"] or "").strip()
        test_id = (row["test_id"] or "").strip()

        if record_type not in ("exception", "metric"):
            violations.append(f"row {line}: record_type must be 'exception' or 'metric' (got {row['record_type']!r})")
            continue
        if not test_id:
            violations.append(f"row {line}: test_id is required")
            continue
        if test_id == "T4.3":
            violations.append(f"row {line}: {_T4_3_REASON}")
            continue
        if test_id != _RUN_LEVEL_TEST_ID and test_id not in CATALOGUE_TESTS:
            violations.append(
                f"row {line}: unknown test_id {test_id!r} -- expected one of "
                f"{sorted(CATALOGUE_TESTS)} or {_RUN_LEVEL_TEST_ID!r} (for a run-level metric)"
            )
            continue

        if record_type == "metric":
            metric_name = (row["metric_name"] or "").strip()
            expected_value = row["expected_value"]
            if not metric_name:
                violations.append(f"row {line}: record_type=metric requires metric_name")
                continue
            if expected_value == "" or expected_value is None:
                violations.append(f"row {line}: record_type=metric requires expected_value")
                continue
            key = (test_id, metric_name)
            if key in seen_metric_keys:
                violations.append(f"row {line}: duplicate metric ({test_id}, {metric_name!r}) already declared")
                continue
            seen_metric_keys.add(key)
            metrics.setdefault(test_id, {})[metric_name] = str(expected_value)
            continue

        # record_type == "exception"
        if test_id == _RUN_LEVEL_TEST_ID:
            violations.append(f"row {line}: record_type=exception may not use test_id={_RUN_LEVEL_TEST_ID!r}")
            continue
        exception_id = (row["exception_id"] or "").strip()
        identity_field = (row["identity_field"] or "").strip()
        identity_value = (row["identity_value"] or "").strip()
        if not exception_id:
            violations.append(f"row {line}: record_type=exception requires exception_id")
            continue
        if not identity_field:
            violations.append(f"row {line}: {test_id}/{exception_id}: identity_field is required")
            continue
        if not identity_value:
            violations.append(f"row {line}: {test_id}/{exception_id}: identity_value is required")
            continue
        try:
            expected = _parse_bool(row["expected"], where=f"row {line}: {test_id}/{exception_id}")
        except Surface1OracleError as exc:
            violations.extend(exc.violations)
            continue

        cfg = CATALOGUE_TESTS[test_id]
        valid_fields = _field_names_for(cfg)
        is_member_field = isinstance(cfg, MemberGroupTest) and identity_field == cfg.member_id_field
        if identity_field not in valid_fields:
            violations.append(
                f"row {line}: {test_id}/{exception_id}: identity_field {identity_field!r} is not one "
                f"of this test's declared field(s) {list(valid_fields)}"
            )
            continue

        bucket = exceptions.setdefault(test_id, {})
        entry = bucket.get(exception_id)
        if entry is None:
            entry = OracleException(exception_id=exception_id, expected=expected)
            bucket[exception_id] = entry
        elif entry.expected != expected:
            violations.append(
                f"row {line}: {test_id}/{exception_id}: expected={expected} conflicts with an earlier "
                f"row's expected={entry.expected} for the same exception_id"
            )
            continue

        if is_member_field:
            entry.members.append(identity_value)
        else:
            if identity_field in entry.fields:
                violations.append(
                    f"row {line}: {test_id}/{exception_id}: identity_field {identity_field!r} is "
                    f"already declared for this exception_id (was {entry.fields[identity_field]!r}, "
                    f"now {identity_value!r}) -- duplicate identity"
                )
                continue
            entry.fields[identity_field] = identity_value

    # Second pass: every exception_id supplies EXACTLY the field set its
    # test's grain requires (no fuzzy partial identities, CLAUDE.md NN14).
    for test_id, bucket in exceptions.items():
        cfg = CATALOGUE_TESTS[test_id]
        if isinstance(cfg, MemberGroupTest):
            for eid, entry in bucket.items():
                if len(entry.members) < 2:
                    violations.append(
                        f"{test_id}/{eid}: a claim group needs at least 2 '{cfg.member_id_field}' "
                        f"member rows, got {len(entry.members)}"
                    )
        else:
            required = set(_field_names_for(cfg))
            for eid, entry in bucket.items():
                got = set(entry.fields)
                missing = required - got
                extra = got - required
                if missing:
                    violations.append(f"{test_id}/{eid}: missing identity_field(s) {sorted(missing)}")
                if extra:
                    violations.append(f"{test_id}/{eid}: unexpected identity_field(s) {sorted(extra)}")

    if violations:
        raise Surface1OracleError(violations)

    return OracleDocument(sha256=sha256, exceptions=exceptions, metrics=metrics)


# ── scoring-unit identity resolution ─────────────────────────────────────────


def _coerce_field(value: str, kind: str, *, where: str) -> Any:
    value = value.strip()
    try:
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "date":
            return pd.Timestamp(value).date().isoformat()
        return value
    except (TypeError, ValueError) as exc:
        raise Surface1OracleError([f"{where}: {value!r} is not a valid {kind}: {exc}"]) from exc


def _row_key_for_number(df: pd.DataFrame, raw_number: str, *, where: str) -> str:
    try:
        n = int(raw_number.strip())
    except ValueError as exc:
        raise Surface1OracleError([f"{where}: Row Number {raw_number!r} is not an integer"]) from exc
    if n < 1 or n > len(df):
        raise Surface1OracleError(
            [f"{where}: Row Number {n} is out of range -- the pinned source has {len(df)} data row(s)"]
        )
    return str(df.iloc[n - 1]["__row_key"])


def _raw_population(data_source: Any, contract_sources: dict, bindings: dict[str, str], source: str) -> pd.DataFrame:
    version = bindings.get(source)
    if version is None:
        raise Surface1RunError(
            f"this run has no data_assets binding for source {source!r} -- cannot resolve oracle "
            f"identities against it"
        )
    df = data_source.read_population(source, version=version)
    validate_contract(df, contract_sources.get(source, {}))
    return df


def _key_group_expected_id(cfg: KeyGroupTest, entry: OracleException, *, test_id: str) -> str:
    values = tuple(
        _coerce_field(entry.fields[name], kind, where=f"{test_id}/{entry.exception_id}.{name}")
        for name, kind in cfg.fields
    )
    return keyed_unit_id(cfg.kind, values)


# ── scoring ──────────────────────────────────────────────────────────────────


@dataclass
class TestScore:
    __test__ = False  # not a pytest test class -- silences pytest's name-based collection heuristic

    test_id: str
    scoring_unit: str
    status: str  # "scored" | "not_testable" | "not_covered_by_oracle"
    reason: str | None = None
    partial_not_testable: list[dict] = field(default_factory=list)
    n_oracle_exceptions: int = 0
    n_oracle_negatives: int = 0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    precision: float | None = None
    recall: float | None = None
    false_positive_ids: list[str] = field(default_factory=list)
    false_negative_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _score_ids(
    expected_true: dict[str, str], expected_false: dict[str, str], engine_ids: set[str],
) -> tuple[int, int, int, list[str], list[str]]:
    """`expected_true`/`expected_false` map a computed scoring-unit id to the
    oracle's own exception_id label, for readable false_positive_ids/
    false_negative_ids. Scoped exactly like orchestrator.eval.surface2._score
    (CLAUDE.md §9's ~100-row SAMPLE, not the whole population): an engine id
    neither side declared is out of scope, neither a false positive nor a
    false negative."""
    scope = set(expected_true) | set(expected_false)
    flagged_in_scope = engine_ids & scope
    tp = flagged_in_scope & set(expected_true)
    fp = flagged_in_scope & set(expected_false)
    fn = set(expected_true) - engine_ids
    fp_labels = sorted(expected_false[i] for i in fp)
    fn_labels = sorted(expected_true[i] for i in fn)
    return len(tp), len(fp), len(fn), fp_labels, fn_labels


def _precision_recall(tp: int, fp: int, fn: int) -> tuple[float | None, float | None]:
    precision = (tp / (tp + fp)) if (tp or fp) else None
    recall = (tp / (tp + fn)) if (tp or fn) else None
    return precision, recall


def _score_row_test(
    test_id: str, cfg: RowTest, oracle_block: dict[str, OracleException] | None, engine_ids: set[str],
    *, data_source: Any, contract_sources: dict, bindings: dict[str, str],
) -> tuple[int, int, int, int, int, float | None, float | None, list[str], list[str]]:
    expected_true: dict[str, str] = {}
    expected_false: dict[str, str] = {}
    df: pd.DataFrame | None = None
    natural_map: dict[str, str] = {}
    for eid, entry in (oracle_block or {}).items():
        raw = entry.fields[cfg.id_field]
        if cfg.id_kind == "row_number":
            if df is None:
                df = _raw_population(data_source, contract_sources, bindings, cfg.source)
            row_key = _row_key_for_number(df, raw, where=f"{test_id}/{eid}")
        else:
            if df is None:
                df = _raw_population(data_source, contract_sources, bindings, cfg.source)
                natural_map = {cfg.natural_id_fn(r): r["__row_key"] for _, r in df.iterrows()}
            row_key = natural_map.get(raw)
            if row_key is None:
                raise Surface1OracleError(
                    [f"{test_id}/{eid}: {cfg.id_field} {raw!r} was not found in the pinned "
                     f"{cfg.source!r} source"]
                )
        (expected_true if entry.expected else expected_false)[row_key] = eid
    tp, fp, fn, fp_ids, fn_ids = _score_ids(expected_true, expected_false, engine_ids)
    precision, recall = _precision_recall(tp, fp, fn)
    return (
        len(expected_true), len(expected_false), tp, fp, fn, precision, recall, fp_ids, fn_ids,
    )


def _score_group_test(
    test_id: str, cfg: KeyGroupTest, oracle_block: dict[str, OracleException] | None, engine_ids: set[str],
) -> tuple[int, int, int, int, int, float | None, float | None, list[str], list[str]]:
    expected_true: dict[str, str] = {}
    expected_false: dict[str, str] = {}
    for eid, entry in (oracle_block or {}).items():
        gid = _key_group_expected_id(cfg, entry, test_id=test_id)
        (expected_true if entry.expected else expected_false)[gid] = eid
    tp, fp, fn, fp_ids, fn_ids = _score_ids(expected_true, expected_false, engine_ids)
    precision, recall = _precision_recall(tp, fp, fn)
    return len(expected_true), len(expected_false), tp, fp, fn, precision, recall, fp_ids, fn_ids


def _score_member_group_test(
    test_id: str, cfg: MemberGroupTest, oracle_block: dict[str, OracleException] | None, engine_ids: set[str],
    *, data_source: Any, contract_sources: dict, bindings: dict[str, str],
) -> tuple[int, int, int, int, int, float | None, float | None, list[str], list[str]]:
    expected_true: dict[str, str] = {}
    expected_false: dict[str, str] = {}
    df: pd.DataFrame | None = None
    for eid, entry in (oracle_block or {}).items():
        if df is None:
            df = _raw_population(data_source, contract_sources, bindings, cfg.member_source)
        member_row_keys = [_row_key_for_number(df, m, where=f"{test_id}/{eid}") for m in entry.members]
        gid = member_group_id(cfg.kind, member_row_keys)
        (expected_true if entry.expected else expected_false)[gid] = eid
    tp, fp, fn, fp_ids, fn_ids = _score_ids(expected_true, expected_false, engine_ids)
    precision, recall = _precision_recall(tp, fp, fn)
    return len(expected_true), len(expected_false), tp, fp, fn, precision, recall, fp_ids, fn_ids


# ── metrics ──────────────────────────────────────────────────────────────────


@dataclass
class MetricComparison:
    test_id: str
    metric_name: str
    expected: str
    actual: Any
    match: bool
    reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _values_equal(expected_raw: str, actual: Any) -> bool:
    """Deterministic means exact (CLAUDE.md §5) -- a Decimal-safe compare
    (same technique as orchestrator.nodes.fieldwork._decimal_variance) when
    both sides parse as numbers, so summation-order float noise below a cent
    never registers as a mismatch while a genuine difference always does;
    an exact, trimmed string compare otherwise."""
    from decimal import Decimal, InvalidOperation

    try:
        expected_dec = Decimal(expected_raw.strip())
        actual_dec = Decimal(str(actual))
        return expected_dec == actual_dec
    except (InvalidOperation, TypeError, ValueError):
        return expected_raw.strip() == ("" if actual is None else str(actual).strip())


def _score_metrics(oracle_metrics: dict[str, dict[str, str]], run_metrics: dict[str, dict]) -> list[MetricComparison]:
    out: list[MetricComparison] = []
    for test_id, entries in sorted(oracle_metrics.items()):
        for metric_name, expected_raw in sorted(entries.items()):
            metric = run_metrics.get(metric_name)
            if metric is None:
                out.append(MetricComparison(
                    test_id=test_id, metric_name=metric_name, expected=expected_raw, actual=None,
                    match=False, reason=f"metric {metric_name!r} is not present in this run's run_metrics",
                ))
                continue
            actual = metric.get("value")
            match = _values_equal(expected_raw, actual)
            out.append(MetricComparison(
                test_id=test_id, metric_name=metric_name, expected=expected_raw, actual=actual, match=match,
            ))
    return out


# ── report ───────────────────────────────────────────────────────────────────


@dataclass
class Surface1Report:
    run_id: str
    generated_at: str
    oracle_path: str
    oracle_sha256: str
    fingerprint_id: str | None
    skill_id: str | None
    skill_version: str | None
    run_status: str
    source_versions: dict[str, str]
    tests: dict[str, TestScore]
    metrics: list[MetricComparison]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "oracle_path": self.oracle_path,
            "oracle_sha256": self.oracle_sha256,
            "fingerprint_id": self.fingerprint_id,
            "skill_id": self.skill_id,
            "skill_version": self.skill_version,
            "run_status": self.run_status,
            "source_versions": dict(sorted(self.source_versions.items())),
            "tests": {tid: score.to_dict() for tid, score in sorted(self.tests.items())},
            "metrics": [m.to_dict() for m in self.metrics],
        }


def run_surface1(
    run_id: str, oracle_path: str | Path, *, ctx: Any = None, env: dict | None = None, now: str | None = None,
) -> Surface1Report:
    """`ctx`: an already-built orchestrator.service.AppContext (tests pass a
    LocalPersistence-backed one against tests/fixtures/tne_planted data with
    no environment at all); omitted, this builds one from `env`
    (orchestrator.service.build_app_context, the same construction the App
    itself uses) -- the path scripts/run_surface1.py takes."""
    from orchestrator import service

    if ctx is None:
        ctx = service.build_app_context(env)

    state = ctx.persistence.load_state(run_id)
    if state.status not in ("completed", "awaiting_signoff"):
        raise Surface1RunError(
            f"run {run_id!r} has status {state.status!r} -- Surface 1 needs a run whose `execute` "
            f"node has completed (status 'awaiting_signoff' or 'completed')"
        )

    oracle = load_oracle(oracle_path)

    node_ctx = service.build_node_context(ctx, state)
    bindings = {b["source"]: b["version"] for b in state.data_assets}
    contract_sources = node_ctx.skill.contract.get("sources", {})

    sub_status = {e["test_id"]: e["status"] for e in state.exceptions}
    flags_by_subtest: dict[str, set[str]] = {}
    for e in plan_test_flags(node_ctx.skill.plan.get("tests", [])):
        flags_by_subtest.setdefault(e["test_id"], set()).add(e["flag"])

    flagged_by_flag: dict[str, list[dict]] = {}
    for r in ctx.persistence.list_flagged_rows(run_id):
        flagged_by_flag.setdefault(r["flag"], []).append(r)

    run_metrics = ctx.persistence.get_run_metrics(run_id)

    tests: dict[str, TestScore] = {}
    for test_id, cfg in sorted(CATALOGUE_TESTS.items()):
        testable_subs = [s for s in cfg.plan_test_ids if sub_status.get(s) != "not_testable"]
        not_testable_subs = [s for s in cfg.plan_test_ids if sub_status.get(s) == "not_testable"]
        oracle_block = oracle.exceptions.get(test_id)

        if not testable_subs:
            reason = "; ".join(
                e.get("reason") or "not_testable" for e in state.exceptions
                if e["test_id"] in not_testable_subs
            ) or None
            if oracle_block:
                raise Surface1OracleError(
                    [f"{test_id}: not_testable for run {run_id!r} ({reason or 'no reason recorded'}) "
                     f"but the oracle declares {len(oracle_block)} exception(s) for it -- remove them"]
                )
            tests[test_id] = TestScore(
                test_id=test_id, scoring_unit=cfg.scoring_unit, status="not_testable", reason=reason,
            )
            continue

        if oracle_block is None:
            tests[test_id] = TestScore(
                test_id=test_id, scoring_unit=cfg.scoring_unit, status="not_covered_by_oracle",
                partial_not_testable=[
                    {"sub_test_id": s, "reason": next(
                        (e.get("reason") for e in state.exceptions if e["test_id"] == s), None,
                    )}
                    for s in not_testable_subs
                ],
            )
            continue

        flags = {f for s in testable_subs for f in flags_by_subtest.get(s, set())}
        if isinstance(cfg, RowTest):
            engine_ids = {r["row_key"] for f in flags for r in flagged_by_flag.get(f, [])}
            n_true, n_false, tp, fp, fn, precision, recall, fp_ids, fn_ids = _score_row_test(
                test_id, cfg, oracle_block, engine_ids,
                data_source=node_ctx.data_source, contract_sources=contract_sources, bindings=bindings,
            )
        elif isinstance(cfg, KeyGroupTest):
            engine_ids = {r["group_id"] for f in flags for r in flagged_by_flag.get(f, []) if r.get("group_id")}
            n_true, n_false, tp, fp, fn, precision, recall, fp_ids, fn_ids = _score_group_test(
                test_id, cfg, oracle_block, engine_ids,
            )
        else:
            engine_ids = {r["group_id"] for f in flags for r in flagged_by_flag.get(f, []) if r.get("group_id")}
            n_true, n_false, tp, fp, fn, precision, recall, fp_ids, fn_ids = _score_member_group_test(
                test_id, cfg, oracle_block, engine_ids,
                data_source=node_ctx.data_source, contract_sources=contract_sources, bindings=bindings,
            )

        tests[test_id] = TestScore(
            test_id=test_id, scoring_unit=cfg.scoring_unit, status="scored",
            partial_not_testable=[
                {"sub_test_id": s, "reason": next(
                    (e.get("reason") for e in state.exceptions if e["test_id"] == s), None,
                )}
                for s in not_testable_subs
            ],
            n_oracle_exceptions=n_true, n_oracle_negatives=n_false,
            true_positives=tp, false_positives=fp, false_negatives=fn,
            precision=precision, recall=recall,
            false_positive_ids=fp_ids, false_negative_ids=fn_ids,
        )

    metrics = _score_metrics(oracle.metrics, run_metrics)

    return Surface1Report(
        run_id=run_id,
        generated_at=now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        oracle_path=str(Path(oracle_path)),
        oracle_sha256=oracle.sha256,
        fingerprint_id=state.fingerprint_id,
        skill_id=state.skill_id,
        skill_version=state.skill_version,
        run_status=state.status,
        source_versions=bindings,
        tests=tests,
        metrics=metrics,
    )


# ── output ───────────────────────────────────────────────────────────────────


def write_json_report(report: Surface1Report, path: str | Path) -> None:
    Path(path).write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")


_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _xlsx_safe(value: Any) -> Any:
    """Spreadsheet formula-injection escaping: a string cell whose first
    character is one Excel/Sheets would treat as a formula trigger is
    prefixed with a leading apostrophe, the standard escape both
    applications treat as "this is text, never evaluate it"."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def write_xlsx_report(report: Surface1Report, path: str | Path) -> None:
    wb = Workbook()

    summary = wb.active
    summary.title = "Summary"
    summary.append([
        "test_id", "scoring_unit", "status", "reason", "n_oracle_exceptions", "n_oracle_negatives",
        "true_positives", "false_positives", "false_negatives", "precision", "recall",
    ])
    for test_id, score in sorted(report.tests.items()):
        summary.append([
            _xlsx_safe(test_id), _xlsx_safe(score.scoring_unit), _xlsx_safe(score.status),
            _xlsx_safe(score.reason), score.n_oracle_exceptions, score.n_oracle_negatives,
            score.true_positives, score.false_positives, score.false_negatives,
            score.precision, score.recall,
        ])

    unmatched = wb.create_sheet("Unmatched")
    unmatched.append(["test_id", "kind", "exception_id"])
    for test_id, score in sorted(report.tests.items()):
        for eid in score.false_positive_ids:
            unmatched.append([_xlsx_safe(test_id), "false_positive", _xlsx_safe(eid)])
        for eid in score.false_negative_ids:
            unmatched.append([_xlsx_safe(test_id), "false_negative", _xlsx_safe(eid)])

    metrics_sheet = wb.create_sheet("Metrics")
    metrics_sheet.append(["test_id", "metric_name", "expected", "actual", "match", "reason"])
    for m in report.metrics:
        metrics_sheet.append([
            _xlsx_safe(m.test_id), _xlsx_safe(m.metric_name), _xlsx_safe(m.expected),
            _xlsx_safe(m.actual) if not isinstance(m.actual, (int, float)) else m.actual,
            m.match, _xlsx_safe(m.reason),
        ])

    header = wb.create_sheet("Report", 0)
    header.append(["run_id", "generated_at", "oracle_path", "oracle_sha256", "fingerprint_id",
                    "skill_id", "skill_version", "run_status"])
    header.append([
        _xlsx_safe(report.run_id), report.generated_at, _xlsx_safe(str(report.oracle_path)),
        report.oracle_sha256, report.fingerprint_id, report.skill_id, report.skill_version,
        report.run_status,
    ])
    header.append([])
    header.append(["source", "version"])
    for source, version in sorted(report.source_versions.items()):
        header.append([_xlsx_safe(source), _xlsx_safe(version)])

    wb.save(str(path))
