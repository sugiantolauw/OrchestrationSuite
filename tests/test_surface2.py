from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.eval.surface2 import run_surface2

REPO_ROOT = Path(__file__).parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "tne_planted"
DATA_DIR = FIXTURE_DIR / "data"
PLANTS_PATH = FIXTURE_DIR / "plants.yaml"
SNAPSHOT_PATH = REPO_ROOT / "tests" / "snapshots" / "surface2_results.json"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

# CLAUDE.md §5, P2 DoD: "per-test precision >= 0.98 and recall >= 0.95 on
# planted data", measured on each test's declared scoring unit
# (docs/specs/SKILL-001_test_specification.md §3), against the
# HAND-AUTHORED oracle in plants.yaml -- never against this generator's own
# output (CLAUDE.md §9).

pytestmark = pytest.mark.skipif(
    not DATA_DIR.is_dir(), reason="tests/fixtures/tne_planted/data/ not present -- run generate.py first"
)


@pytest.fixture(scope="module")
def scores():
    return run_surface2(
        skill_dir=SKILL_DIR, plants_path=PLANTS_PATH, data_dir=DATA_DIR, audit_period=AUDIT_PERIOD
    )


# Every test_id in skills/tne_exco/plan.yaml that is not `not_testable`
# (T3.2a_accom, T4.3). Kept as an explicit list, not derived from plan.yaml,
# so a Skill change that silently drops a test's fixture coverage fails
# loudly here rather than the gate quietly shrinking.
SCORABLE_TEST_IDS = [
    "T3.1a", "T3.1b",
    "T3.2a_air_dom", "T3.2a_air_int", "T3.2a_car_dom", "T3.2a_car_int",
    "T3.3a_dom", "T3.3a_int", "T3.3a_very_late",
    "T3.3b",
    "T4.1", "T4.2", "T4.4",
    "T5.1", "T5.2",
    "T6.1a", "T6.1c",
    "T6.1d_dom", "T6.1d_int",
]


def test_plants_yaml_covers_every_scorable_test(scores):
    assert set(scores) == set(SCORABLE_TEST_IDS)


def test_every_test_has_at_least_15_plants_and_15_negatives(scores):
    for test_id, score in scores.items():
        assert score.n_plants >= 15, f"{test_id}: only {score.n_plants} planted exceptions (need >=15)"
        assert score.n_negatives >= 15, f"{test_id}: only {score.n_negatives} near-miss negatives (need >=15)"


@pytest.mark.parametrize("test_id", SCORABLE_TEST_IDS)
def test_precision_at_least_0_98(test_id, scores):
    score = scores[test_id]
    assert score.precision is not None, f"{test_id}: no positive or false-positive units scored at all"
    assert score.precision >= 0.98, (
        f"{test_id}: precision {score.precision:.4f} < 0.98 "
        f"(TP={score.true_positives} FP={score.false_positives}, false positives: {score.false_positive_ids})"
    )


@pytest.mark.parametrize("test_id", SCORABLE_TEST_IDS)
def test_recall_at_least_0_95(test_id, scores):
    score = scores[test_id]
    assert score.recall is not None, f"{test_id}: no true or false-negative units scored at all"
    assert score.recall >= 0.95, (
        f"{test_id}: recall {score.recall:.4f} < 0.95 "
        f"(TP={score.true_positives} FN={score.false_negatives}, false negatives: {score.false_negative_ids})"
    )


def test_results_snapshot_matches_committed_file(scores):
    assert SNAPSHOT_PATH.is_file(), "run orchestrator/eval/surface2.py and commit tests/snapshots/surface2_results.json first"
    committed = json.loads(SNAPSHOT_PATH.read_text())
    actual = {tid: score.to_dict() for tid, score in scores.items()}
    assert actual == committed, (
        "Surface 2 results changed since the committed snapshot -- if this is a "
        "deliberate engine/Skill/fixture change, regenerate and re-commit "
        "tests/snapshots/surface2_results.json"
    )
