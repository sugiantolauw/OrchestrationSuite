"""docs/specs/P7_mapping_authoring_design.md §2.3 "Generic scorer": scores
already-generated fixture data against its `plants.yaml` sidecar. Unlike
`orchestrator.eval.surface2` (SKILL-001's bespoke scorer, scoped to each
test's own declared plants because SKILL-001's real population has several
tests legitimately sharing rows -- see that module's own docstring), a
generic sidecar Skill's fixture is small and self-contained: every row that
exists at all came either from `background:` (declared clean) or from some
test's own declared `rows`/`groups` block, so there is no "neither declared"
row to carve out. The rule here is therefore the simpler, stricter one
§2.3 states directly: **anything flagged for test X that X's own block did
not declare `exception: true` counts as a false positive for X** -- a
background row (or another test's plant) that trips X's rule is exposed as
reduced precision, never silently excluded."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yaml

from orchestrator.contract import LocalFileDataSource
from orchestrator.engine import ExecutionResult, execute_skill
from orchestrator.skills import load_skill


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


def _score(
    test_id: str,
    scoring_unit: str,
    expected_true: set,
    flagged: set,
    *,
    n_negatives: int,
    id_of: Callable[[Any], str] = str,
) -> TestScore:
    tp = flagged & expected_true
    fp = flagged - expected_true
    fn = expected_true - flagged
    precision = (len(tp) / (len(tp) + len(fp))) if (tp or fp) else None
    recall = (len(tp) / (len(tp) + len(fn))) if (tp or fn) else None
    return TestScore(
        test_id=test_id,
        scoring_unit=scoring_unit,
        n_plants=len(expected_true),
        n_negatives=n_negatives,
        true_positives=len(tp),
        false_positives=len(fp),
        false_negatives=len(fn),
        precision=precision,
        recall=recall,
        false_positive_ids=sorted(id_of(x) for x in fp),
        false_negative_ids=sorted(id_of(x) for x in fn),
    )


def _natural_id_map(raw_frames: dict[str, pd.DataFrame], source: str, natural_id_column: str) -> dict[str, str]:
    df = raw_frames.get(source)
    if df is None or natural_id_column not in df.columns:
        return {}
    return dict(zip(df["__row_key"], df[natural_id_column].astype(str)))


def _flagged_groups(result: ExecutionResult, flag_name: str | None, row_map: dict[str, str]) -> set[frozenset]:
    """Every flagged GROUP for `flag_name`, as a frozenset of member natural
    ids -- read from `flags_long` (long-format: one row per exception
    instance, __row_key + group_id intact), not from `scored_units` (whose
    group_id is an opaque engine-internal hash we would otherwise have to
    reproduce). A flag row with no group_id (a primitive that never grouped)
    is its own singleton group."""
    if not flag_name:
        return set()
    long = result.flags_long
    subset = long[long["flag"] == flag_name]
    members: dict[str, set[str]] = {}
    for _, row in subset.iterrows():
        row_key = row["__row_key"]
        group_id = row.get("group_id")
        key = group_id if isinstance(group_id, str) and group_id else row_key
        members.setdefault(key, set()).add(row_map.get(row_key, row_key))
    return {frozenset(v) for v in members.values()}


def score_fixtures(
    skill_dir: str | Path, plants_path: str | Path, data_dir: str | Path, *, audit_period: tuple[str, str] | None = None
) -> dict[str, TestScore]:
    """For every scorable test named in plants.yaml's `tests:` block, runs
    the real engine (`orchestrator.engine.execute_skill`, the SAME function
    the pipeline's `execute` node calls) over `data_dir` and scores its
    flagged rows/groups against the sidecar's declared exceptions.
    `audit_period` defaults to plants.yaml's own `audit_period`."""
    skill_dir = Path(skill_dir)
    skill = load_skill(skill_dir)
    skill.validate()
    plants = yaml.safe_load(Path(plants_path).read_text())
    natural_id_column = plants["natural_id_column"]
    period = tuple(audit_period) if audit_period else tuple(plants["audit_period"])

    data_source = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    result = execute_skill(
        skill, data_source=data_source, audit_period=period, run_context={"run_id": "authoring-score"}
    )

    plan_tests = {t["test_id"]: t for t in skill.plan.get("tests", [])}
    scores: dict[str, TestScore] = {}
    for test_id, block in (plants.get("tests") or {}).items():
        if test_id.startswith("_"):
            continue
        source = block["source"]
        row_map = _natural_id_map(result.raw_frames, source, natural_id_column)

        if block["scoring_unit"] == "row":
            rows = block.get("rows", [])
            expected_true = {r["natural_id"] for r in rows if r["exception"]}
            n_negatives = sum(1 for r in rows if not r["exception"])
            flagged_row_keys = set(result.scored_units.get(test_id, []))
            flagged = {row_map[rk] for rk in flagged_row_keys if rk in row_map}
            scores[test_id] = _score(test_id, "row", expected_true, flagged, n_negatives=n_negatives)
        else:
            groups = block.get("groups", [])
            expected_true = {frozenset(m["natural_id"] for m in g["members"]) for g in groups if g["exception"]}
            n_negatives = sum(1 for g in groups if not g["exception"])
            flag_name = (plan_tests.get(test_id) or {}).get("flag")
            flagged = _flagged_groups(result, flag_name, row_map)
            scores[test_id] = _score(
                test_id, "group", expected_true, flagged, n_negatives=n_negatives,
                id_of=lambda fs: "+".join(sorted(fs)),
            )

    return scores
