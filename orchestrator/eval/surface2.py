"""Surface 2 (CLAUDE.md §5 Tier A): per-test precision/recall of the SKILL-001
engine against the hand-authored oracle in tests/fixtures/tne_planted/plants.yaml
-- never against a generator's own idea of what is an exception.

For every scorable test, this module compares the engine's flagged scoring
units against plants.yaml's declared exceptions and near-miss negatives.
Precision/recall for test X is computed over the natural ids X's own
plants.yaml block declares (`expected_true` | `expected_false`) -- an engine
flag on a row/group neither declared is neither a false positive nor a false
negative for X (CLAUDE.md build brief B1). That scoping is necessary, not
just convenient: several tests share one source population (T3.1b and
T3.3a_dom/_int/_very_late both read t31b_bookings_pop; T3.2a, T4.1, T4.2,
T4.4, T5.1, T5.2 and T6.1d all read expense_report/P_EXP), so a row planted
for one test can correctly and legitimately also satisfy ANOTHER test's
exception condition (e.g. a >$5,000 line planted to test T5.1's max_line
exclusion is also, correctly, a genuine T4.4 high-value exception) -- scoring
X against the FULL population would count that correct T4.4 flag as a false
positive for X, which is wrong, not conservative.

What changed for B1: over-flagging on a row/group the fixture DOES declare
(as a negative, or as a plant of ANOTHER kind for the same test) was already
caught before B1; what B1 closes is the blind spot where a row that should
never be an exception under any test's rules (background) or that must not
enter a test's population at all (e.g. a non-ExCo row under a broken
population filter) went completely undeclared, so an engine bug flagging it
was invisible. The fix is fixture content, not a change to the comparison
formula: `tests/fixtures/tne_planted/plants.yaml` now declares, per targeted
test, ExCo background rows that are clearly non-exceptional under every rule
and non-ExCo rows that would be flagged if a population filter broke, both as
`negatives` -- so an engine flag on them becomes a scored false positive, the
same way a near-miss negative always has been. See
`tests/test_surface2_mutations.py` for the mutations this makes visible.

Row-grain tests: `scored_units` are the engine's own `__row_key` values,
mapped to a NATURAL identifier via a small per-source function (Booking ID,
Travel Request ID, Report ID|Step|Approver ID, or the generator's `Line
Marker` bookkeeping column for expense_report/attendee_validity lines --
docs/specs/SKILL-001_test_specification.md §0 "Row identity").

Group-grain tests (T3.3b, T5.2, T6.1d_dom/_int): plants.yaml declares the
group's own key-column values directly (the same columns the primitive
groups by), so the expected group_id is computed with the SAME
`keyed_unit_id` the engine itself uses (N1) -- never inferred by re-reading
engine output. T5.1 is different (B3): a claim group's identity is its ROW
MEMBERSHIP, not a key tuple, so its expected id is computed by resolving
plants.yaml's declared `members` to the __row_key values the generated data
actually assigned them (via a natural_id -> __row_key reverse lookup, same
mechanism as the row-grain case) and hashing that set with
`member_group_id("claim", ...)`, exactly mirroring split_detection.py.
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
from orchestrator.primitives.common import keyed_unit_id, member_group_id
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
    # int (e.g. 810, not 810.0). keyed_unit_id normalises numeric values via
    # Decimal(repr(v)) (N1), so this must still match the engine's dtype --
    # 810 and 810.0 normalise identically, but the raw YAML value is coerced
    # to float here for clarity/consistency with the other _t*_gid helpers.
    # B5: an entry with several attendee ROWS (`members`, mixed internal/
    # external) is declared the same shape as T5.1's group plants -- the key
    # columns are identical across every member, so the first suffices.
    f = entry["members"][0] if "members" in entry else entry["fields"]
    return keyed_unit_id(
        "ratio", (f["Employee ID"], f["Report Name"], f["Transaction Date"], f["Vendor"], float(f["Entry Amount"]))
    )


def _t51_gid(entry: dict, *, row_key_of: Callable[[str], str]) -> str:
    # B3/N1: a claim group's identity is its ROW MEMBERSHIP, not its key
    # tuple -- a window detection covering the identical row set as a
    # same-day detection is the same claim. `row_key_of` maps this entry's
    # declared members (by the SAME natural id the engine's own row-grain
    # lookup uses, e.g. "40446#30224") to the __row_key the generated data
    # actually assigned them, exactly mirroring what split_detection.py does
    # internally with member_group_id("claim", ...).
    member_row_keys = [row_key_of(f"{int(m['Report Legacy Key'])}#{int(m['Line Marker'])}") for m in entry["members"]]
    return member_group_id("claim", member_row_keys)


def _t52_gid(entry: dict) -> str:
    # group_keys = [Employee ID, Transaction Date, Vendor, Amount] -- Amount
    # needs the same float coercion as T3.3b's Entry Amount, above.
    gk = list(entry["group_keys"])
    gk[3] = float(gk[3])
    return keyed_unit_id("dup", tuple(gk))


def _t61d_gid(entry: dict) -> str:
    return keyed_unit_id("thr", tuple(entry["group_keys"]))


_GROUP_GID_FN: dict[str, Callable[..., str]] = {
    "T3.3b": _t33b_gid,
    "T5.1": _t51_gid,
    "T5.2": _t52_gid,
    "T6.1d_dom": _t61d_gid,
    "T6.1d_int": _t61d_gid,
}

# Tests whose gid function needs a natural_id -> __row_key lookup (member-set
# based ids, B3/N1) rather than being a pure function of declared field values.
_GROUP_GID_NEEDS_ROW_KEY_LOOKUP = {"T5.1"}


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


def _score_group_test(
    test_id: str, block: dict, result: ExecutionResult, row_key_of: Callable[[str], str] | None
) -> TestScore:
    gid_fn = _GROUP_GID_FN[test_id]
    if test_id in _GROUP_GID_NEEDS_ROW_KEY_LOOKUP:
        expected_true = {gid_fn(p, row_key_of=row_key_of) for p in block["plants"]}
        expected_false = {gid_fn(n, row_key_of=row_key_of) for n in block["negatives"]}
    else:
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
    skill: Skill | None = None,
) -> dict[str, TestScore]:
    """`skill`: an already-loaded (and optionally mutated) Skill to run
    against, instead of loading `skill_dir` fresh -- how
    tests/test_surface2_mutations.py applies a plan/reference-level mutation
    in-process before scoring (CLAUDE.md build brief B1)."""
    if skill is None:
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
    # B3/N1: T5.1's gid needs the INVERSE of a row map (natural_id -> __row_key)
    # to translate plants.yaml's declared members into the row keys
    # split_detection.py actually hashed. "expense_report" is already a row
    # source above (T4.1/T4.2/T4.4 also read it), so no extra data read here.
    reverse_row_maps: dict[str, dict[str, str]] = {
        source: {nid: rk for rk, nid in m.items()} for source, m in row_maps.items()
    }

    scores: dict[str, TestScore] = {}
    for test_id, block in test_blocks.items():
        if test_id in _GROUP_GID_FN:
            row_key_of = None
            if test_id in _GROUP_GID_NEEDS_ROW_KEY_LOOKUP:
                row_key_of = reverse_row_maps[block["source"]].__getitem__
            scores[test_id] = _score_group_test(test_id, block, result, row_key_of)
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
