from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# CLAUDE.md §5 Tier B: "a historical regression comparison against
# computation.py on the same inputs. This is not a correctness test --
# computation.py has documented semantic defects (§0.2) -- it is a regression
# test that ensures every difference between the new engine and the old one
# is EXPLAINED by a known defect or a deliberate correction. The gate is 'no
# unexplained divergence', not 'matches computation.py'."

REPO_ROOT = Path(__file__).parent.parent
REFERENCE_APP_DIR = REPO_ROOT / "reference_app"
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
SYNTHETIC_DATA_DIR = REPO_ROOT / "synthetic_data"
EXPLANATIONS_PATH = REPO_ROOT / "docs" / "specs" / "SKILL-001_regression_explanations.yaml"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

pytestmark = pytest.mark.skipif(
    not SYNTHETIC_DATA_DIR.is_dir(), reason="synthetic_data/ not present in this checkout"
)


@pytest.fixture(scope="module")
def exco_display_names() -> list[str]:
    return yaml.safe_load((SKILL_DIR / "reference" / "exco_display_names.yaml").read_text())


@pytest.fixture(scope="module")
def old_computation_module(exco_display_names):
    """Imports reference_app's `src.data_loader`/`src.computation` with
    EXCO_MEMBERS monkeypatched to the 11 SYNTHETIC ExCo display names
    (CLAUDE.md §11 recorded data decision 1: the prototype's original 11
    names do not occur in synthetic_data/ at all). `src` is reference_app's
    own package name, not this repo's -- inserted onto sys.path only for the
    duration of this fixture's use, same pattern reference_app/tests/
    itself uses when run from its own directory."""
    sys.path.insert(0, str(REFERENCE_APP_DIR))
    try:
        import src.computation as computation
        import src.data_loader as data_loader

        computation.EXCO_MEMBERS = exco_display_names
        data_loader.EXCO_MEMBERS = exco_display_names
        yield {"computation": computation, "data_loader": data_loader}
    finally:
        sys.path.remove(str(REFERENCE_APP_DIR))
        for name in list(sys.modules):
            if name == "src" or name.startswith("src."):
                del sys.modules[name]


@pytest.fixture(scope="module")
def old_data(old_computation_module):
    data_loader = old_computation_module["data_loader"]
    data = data_loader.load_all_files(str(SYNTHETIC_DATA_DIR))
    populations = data_loader.build_exco_populations(data["expense_report"])
    return {"files": data, "populations": populations}


def _run(fn):
    """Runs one computation.py test function, catching and recording any
    exception (CLAUDE.md task: "catch and record exceptions per function
    (e.g. T3.2a's missing 'Supplier Name' column) rather than crashing") so
    one broken function does not prevent comparing the other 13."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any failure is recorded, not hidden
        return f"ERROR: {type(exc).__name__}({exc})"


@pytest.fixture(scope="module")
def old_results(old_computation_module, old_data):
    computation = old_computation_module["computation"]
    files = old_data["files"]
    pops = old_data["populations"]

    raw = {
        "T3.1a": _run(lambda: computation.compute_test_3_1a(files["travel_requests_no_expense"])),
        "T3.1b": _run(
            lambda: computation.compute_test_3_1b(files["booking_detail"], files["travel_request_segment"])
        ),
        "T3.2a": _run(lambda: computation.compute_test_3_2a(files["booking_detail"])),
        "T3.3a": _run(lambda: computation.compute_test_3_3a(files["booking_detail"])),
        "T3.3b": _run(lambda: computation.compute_test_3_3b(pops["combined"])),
        "T4.1": _run(lambda: computation.compute_test_4_1(files["missing_receipt"], pops["combined"])),
        "T4.2": _run(lambda: computation.compute_test_4_2(files["attendee_validity"])),
        "T4.3": _run(lambda: computation.compute_test_4_3_cached()),
        "T4.4": _run(lambda: computation.compute_test_4_4(pops["combined"].copy())),
        "T5.1": _run(lambda: computation.compute_test_5_1(pops["combined"].copy())),
        "T5.2": _run(lambda: computation.compute_test_5_2(pops["combined"].copy())),
        "T6.1a": _run(lambda: computation.compute_test_6_1a(files["approval_aging"])),
        "T6.1c": _run(lambda: computation.compute_test_6_1c(files["attendee_validity"])),
        "T6.1d": _run(
            lambda: computation.compute_test_6_1d(pops["combined"].copy(), files["per_diem_rates"])
        ),
    }
    return raw


def _headline(test_id: str, raw) -> object:
    """The single headline number computation.py's own docstring/return shape
    represents for each test -- the same quantity the *_regression_explanations.yaml
    `old` value records. An ERROR string passes through unchanged."""
    if isinstance(raw, str):
        return raw
    if test_id == "T3.1a":
        return raw["count"]
    if test_id == "T3.1b":
        return raw["count"]
    if test_id == "T3.3a":
        return raw["count"]
    if test_id == "T3.3b":
        return raw["over_40"] + raw["over_80"]
    if test_id == "T4.1":
        return raw["count"]
    if test_id == "T4.2":
        return raw["missing"]
    if test_id == "T4.3":
        return raw["flagged"]
    if test_id == "T4.4":
        return raw["count"]
    if test_id == "T5.1":
        return raw["same_day"]
    if test_id == "T5.2":
        return raw["count"]
    if test_id == "T6.1a":
        return raw["pct"]
    if test_id == "T6.1c":
        return raw["exceptions"]
    if test_id == "T6.1d":
        return raw["exception_rows"]
    raise AssertionError(f"no headline mapping declared for {test_id}")


@pytest.fixture(scope="module")
def new_metrics():
    from orchestrator.contract import LocalFileDataSource
    from orchestrator.engine import execute_skill
    from orchestrator.skills import load_skill

    skill = load_skill(SKILL_DIR)
    skill.validate()
    ds = LocalFileDataSource(root_dir=SYNTHETIC_DATA_DIR, sources=skill.contract["sources"])
    result = execute_skill(
        skill, data_source=ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "test-regression"}
    )
    return {name: m["value"] for name, m in result.metrics.items()}


def _new_headline(test_id: str, metrics: dict) -> object:
    if test_id == "T3.2a":
        return metrics["pref_dom_airline"] + metrics["pref_int_airline"] + metrics["pref_dom_car"] + metrics["pref_int_car"]
    mapping = {
        "T3.1a": "preapproval_unlinked_count",
        "T3.1b": "no_preapproval_bookings",
        "T3.3a": lambda m: m["late_booking_count_dom"] + m["late_booking_count_int"],
        "T3.3b": lambda m: m["ent_over_internal_count"] + m["ent_over_external_count"],
        "T4.1": "missing_receipt_count",
        "T4.2": "att_missing_count",
        "T4.3": lambda m: None,  # not_testable until P6 -- no metric is produced
        "T4.4": "hv_count",
        "T5.1": "split_same_day_groups",
        "T5.2": "duplicate_lines",
        "T6.1a": "approver_no_receipt_pct",
        "T6.1c": "att_hierarchy_invalid",
        "T6.1d": lambda m: m["daily_over_count_dom"] + m["daily_over_count_int"],
    }
    spec = mapping[test_id]
    return spec(metrics) if callable(spec) else metrics[spec]


@pytest.fixture(scope="module")
def explanations() -> dict:
    assert EXPLANATIONS_PATH.is_file(), "docs/specs/SKILL-001_regression_explanations.yaml is missing"
    return yaml.safe_load(EXPLANATIONS_PATH.read_text())["tests"]


ALL_TEST_IDS = [
    "T3.1a", "T3.1b", "T3.2a", "T3.3a", "T3.3b", "T4.1", "T4.2", "T4.3",
    "T4.4", "T5.1", "T5.2", "T6.1a", "T6.1c", "T6.1d",
]


def test_explanations_file_covers_every_computation_py_test(explanations):
    assert set(explanations) == set(ALL_TEST_IDS)


@pytest.mark.parametrize("test_id", ALL_TEST_IDS)
def test_old_value_matches_committed_explanation(test_id, old_results, explanations):
    actual_old = _headline(test_id, old_results[test_id])
    recorded_old = explanations[test_id]["old"]
    assert actual_old == recorded_old, (
        f"{test_id}: computation.py's headline value changed since the committed "
        f"explanation (was {recorded_old!r}, now {actual_old!r}) -- if this is a "
        f"deliberate change, regenerate and re-explain it in "
        f"docs/specs/SKILL-001_regression_explanations.yaml"
    )


@pytest.mark.parametrize("test_id", ALL_TEST_IDS)
def test_new_value_matches_committed_explanation(test_id, new_metrics, explanations):
    actual_new = _new_headline(test_id, new_metrics)
    recorded_new = explanations[test_id]["new"]
    assert actual_new == recorded_new, (
        f"{test_id}: the new engine's headline value changed since the committed "
        f"explanation (was {recorded_new!r}, now {actual_new!r}) -- if this is a "
        f"deliberate engine/Skill change, regenerate and re-explain it in "
        f"docs/specs/SKILL-001_regression_explanations.yaml"
    )


@pytest.mark.parametrize("test_id", ALL_TEST_IDS)
def test_divergent_flag_is_never_wrong(test_id, old_results, new_metrics, explanations):
    entry = explanations[test_id]
    actual_old = _headline(test_id, old_results[test_id])
    actual_new = _new_headline(test_id, new_metrics)
    really_divergent = actual_old != actual_new
    assert entry["divergent"] == really_divergent, (
        f"{test_id}: explanations file says divergent={entry['divergent']} but "
        f"old={actual_old!r} new={actual_new!r} says divergent={really_divergent}"
    )


@pytest.mark.parametrize("test_id", ALL_TEST_IDS)
def test_every_divergent_test_has_a_non_empty_explanation_and_spec_ref(test_id, explanations):
    entry = explanations[test_id]
    if entry["divergent"]:
        assert entry.get("explanation", "").strip(), f"{test_id}: divergent with no explanation"
        assert entry.get("spec_ref", "").strip(), f"{test_id}: divergent with no spec_ref"
