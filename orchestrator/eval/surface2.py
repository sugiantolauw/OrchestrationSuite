"""Surface 2 (CLAUDE.md §5 Tier A): per-test precision/recall of the SKILL-001
engine against the hand-authored oracle in tests/fixtures/tne_planted/plants.yaml
-- never against a generator's own idea of what is an exception.

For every scorable test, this module compares the engine's flagged scoring
units against plants.yaml's declared exceptions and near-miss negatives,
RESTRICTED TO THE ROWS/GROUPS THIS FIXTURE ACTUALLY DECLARED AN EXPECTATION
FOR. That scoping is deliberate: several tests share one source population
(T3.1b and T3.3a_dom/_int/_very_late both read t31b_bookings_pop; T3.2a,
T4.1, T4.2, T4.4, T5.1, T5.2 and T6.1d all read expense_report/P_EXP), so a
row planted for one test can correctly and legitimately also satisfy
ANOTHER test's exception condition (e.g. a >$5,000 line planted to test
T5.1's max_line exclusion is also, correctly, a genuine T4.4 high-value
exception). Precision/recall for test X is therefore computed only over the
natural ids X's own plants.yaml block declares -- an engine flag on a row
X's block never mentioned is neither a false positive nor a false negative
for X, because the fixture made no claim about it for X.

Row-grain tests: `scored_units` are the engine's own `__row_key` values,
mapped to a NATURAL identifier via a small per-source function (Booking ID,
Travel Request ID, Report ID|Step|Approver ID, or the generator's `Line
Marker` bookkeeping column for expense_report/attendee_validity lines --
docs/specs/SKILL-001_test_specification.md §0 "Row identity").

Group-grain tests (T3.3b, T5.1, T5.2, T6.1d_dom/_int): plants.yaml declares
the group's own key-column values directly (the same columns the primitive
groups by), so the expected group_id is computed with the SAME
`group_id_from_key` the engine itself uses -- never inferred by re-reading
engine output.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd
import yaml

from orchestrator.contract import LocalFileDataSource, validate_contract
from orchestrator.engine import ExecutionResult, execute_skill
from orchestrator.primitives.common import group_id_from_key
from orchestrator.skills import Skill, load_skill

# ── row-grain natural identifiers ───────────────────────────────────────────


def _natural_id_travel_request(row: pd.Series) -> str:
    return str(row["Travel Request ID"])


def _natural_id_booking(row: pd.Series) -> str:
    return str(int(row["Booking ID"]))


def _natural_id_expense_line(row: pd.Series) -> str:
    # Matches plants.yaml's authored natural_id exactly: "{Report Legacy
    # Key}#{Line Marker}" (spec §0 "Report Legacy Key + line marker").
    return f"{int(row['Report Legacy Key'])}#{int(row['Line Marker'])}"


def _natural_id_line_marker(row: pd.Series) -> str:
    return str(int(row["Line Marker"]))


def _natural_id_approval_step(row: pd.Series) -> str:
    return f"{row['Report ID']}|{row['Step']}|{row['Approver ID']}"


_NATURAL_ID_FN: dict[str, Callable[[pd.Series], str]] = {
    "travel_requests_no_expense": _natural_id_travel_request,
    "booking_detail": _natural_id_booking,
    "expense_report": _natural_id_expense_line,
    "attendee_validity": _natural_id_line_marker,
    "approval_aging": _natural_id_approval_step,
}

# ── group-grain expected group_id computation ───────────────────────────────


def _t33b_gid(entry: dict) -> str:
    # Entry Amount is contract type "number" -- the engine's own coerced
    # value is always a float (pandas _coerce_number), even when this
    # fixture's authored value happens to be a whole number stored as a YAML
    # int (e.g. 810, not 810.0). group_id_from_key formats ints and floats
    # differently ("810" vs "810.0"), so this must match the engine's dtype.
    f = entry["fields"]
    return group_id_from_key(
        (f["Employee ID"], f["Report Name"], f["Transaction Date"], f["Vendor"], float(f["Entry Amount"]))
    )


def _t51_gid(entry: dict) -> str:
    gk = entry["group_keys"]
    if entry.get("kind") == "window":
        start = min(m["Transaction Date"] for m in entry["members"])  # ISO strings sort chronologically
        return group_id_from_key(tuple(gk) + (start,))
    return group_id_from_key(tuple(gk))


def _t52_gid(entry: dict) -> str:
    # group_keys = [Employee ID, Transaction Date, Vendor, Amount] -- Amount
    # needs the same float coercion as T3.3b's Entry Amount, above.
    gk = list(entry["group_keys"])
    gk[3] = float(gk[3])
    return group_id_from_key(tuple(gk))


def _t61d_gid(entry: dict) -> str:
    return group_id_from_key(tuple(entry["group_keys"]))


_GROUP_GID_FN: dict[str, Callable[[dict], str]] = {
    "T3.3b": _t33b_gid,
    "T5.1": _t51_gid,
    "T5.2": _t52_gid,
    "T6.1d_dom": _t61d_gid,
    "T6.1d_int": _t61d_gid,
}


@dataclass
class TestScore:
    test_id: str
    scoring_unit: str
    n_plants: int
    n_negatives: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    false_positive_ids: list[str] = field(default_factory=list)
    false_negative_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _row_key_natural_id_map(skill: Skill, data_source: LocalFileDataSource, source: str, version: str) -> dict[str, str]:
    df = data_source.read_population(source, version=version)
    validate_contract(df, skill.contract["sources"][source])
    fn = _NATURAL_ID_FN[source]
    return {row["__row_key"]: fn(row) for _, row in df.iterrows()}


def _score_row_test(test_id: str, block: dict, result: ExecutionResult, row_map: dict[str, str]) -> TestScore:
    expected_true = {p["natural_id"] for p in block["plants"]}
    expected_false = {n["natural_id"] for n in block["negatives"]}

    flagged_row_keys = set(result.scored_units.get(test_id, []))
    flagged_natural_ids = {row_map[rk] for rk in flagged_row_keys if rk in row_map}

    return _score(test_id, block, expected_true, expected_false, flagged_natural_ids)


def _score_group_test(test_id: str, block: dict, result: ExecutionResult) -> TestScore:
    gid_fn = _GROUP_GID_FN[test_id]
    expected_true = {gid_fn(p) for p in block["plants"]}
    expected_false = {gid_fn(n) for n in block["negatives"]}
    flagged = set(result.scored_units.get(test_id, []))
    return _score(test_id, block, expected_true, expected_false, flagged)


def _score(test_id: str, block: dict, expected_true: set, expected_false: set, flagged: set) -> TestScore:
    scope = expected_true | expected_false
    flagged_in_scope = flagged & scope

    tp = flagged_in_scope & expected_true
    fp = flagged_in_scope & expected_false
    fn = expected_true - flagged

    precision = (len(tp) / (len(tp) + len(fp))) if (tp or fp) else None
    recall = (len(tp) / (len(tp) + len(fn))) if (tp or fn) else None

    return TestScore(
        test_id=test_id,
        scoring_unit=block["scoring_unit"],
        n_plants=len(block["plants"]),
        n_negatives=len(block["negatives"]),
        true_positives=len(tp),
        false_positives=len(fp),
        false_negatives=len(fn),
        precision=precision,
        recall=recall,
        false_positive_ids=sorted(fp),
        false_negative_ids=sorted(fn),
    )


def run_surface2(
    *,
    skill_dir: str | Path,
    plants_path: str | Path,
    data_dir: str | Path,
    audit_period: tuple[str, str],
) -> dict[str, TestScore]:
    skill = load_skill(skill_dir)
    skill.validate()
    plants_doc = yaml.safe_load(Path(plants_path).read_text())

    ds = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    result = execute_skill(
        skill, data_source=ds, audit_period=audit_period, run_context={"run_id": "surface2"}
    )

    test_blocks = {tid: block for tid, block in plants_doc["tests"].items() if not tid.startswith("_")}

    row_sources = {block["source"] for block in test_blocks.values() if block["source"] in _NATURAL_ID_FN}
    row_maps: dict[str, dict[str, str]] = {
        source: _row_key_natural_id_map(skill, ds, source, result.source_versions[source])
        for source in row_sources
    }

    scores: dict[str, TestScore] = {}
    for test_id, block in test_blocks.items():
        if test_id in _GROUP_GID_FN:
            scores[test_id] = _score_group_test(test_id, block, result)
        else:
            scores[test_id] = _score_row_test(test_id, block, result, row_maps[block["source"]])
    return scores


def write_results(scores: dict[str, TestScore], path: str | Path) -> None:
    payload = {tid: score.to_dict() for tid, score in sorted(scores.items())}
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    _THIS_DIR = Path(__file__).resolve().parent
    REPO_ROOT = _THIS_DIR.parents[1]
    scores = run_surface2(
        skill_dir=REPO_ROOT / "skills" / "tne_exco",
        plants_path=REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "plants.yaml",
        data_dir=REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "data",
        audit_period=("2025-01-01", "2026-04-30"),
    )
    for test_id, score in sorted(scores.items()):
        print(
            f"{test_id:16s} plants={score.n_plants:3d} negs={score.n_negatives:3d} "
            f"TP={score.true_positives:3d} FP={score.false_positives:2d} FN={score.false_negatives:2d} "
            f"precision={score.precision} recall={score.recall}"
        )
