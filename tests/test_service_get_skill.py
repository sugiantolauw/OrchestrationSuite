"""service.get_skill's drill-down mapping (CLAUDE.md build brief P3 §1, N6):
every catalogue test entry gains `plan_tests` (the plan.yaml primitive
instance(s) -- and their RF_* flag(s) -- it resolves to), and the top-level
`flag_to_test` gives the reverse lookup a flagged_rows.flag needs to map
back to a plan test id. Exercised against the real SKILL-001 Skill, whose
plan.yaml is where one catalogue test ("T3.3a") splits into several plan
tests ("T3.3a_dom"/"T3.3a_int"/"T3.3a_very_late") -- the mini fixture Skill
used elsewhere never exercises that split."""

from __future__ import annotations

from pathlib import Path

from orchestrator import service

REPO_ROOT = Path(__file__).parent.parent


def _ctx(tmp_path):
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(REPO_ROOT / "synthetic_data"),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
    }
    return service.build_app_context(env)


def test_plan_tests_split_across_one_catalogue_entry(tmp_path):
    skill = service.get_skill(_ctx(tmp_path), "SKILL-001")
    assert skill is not None

    t33a = next(t for t in skill["tests"] if t["test_id"] == "T3.3a")
    plan_ids = {e["test_id"] for e in t33a["plan_tests"]}
    assert plan_ids == {"T3.3a_dom", "T3.3a_int", "T3.3a_very_late"}
    for entry in t33a["plan_tests"]:
        assert set(entry) == {"test_id", "flag", "primitive"}
        assert entry["primitive"] == "threshold_exceedance"
        assert entry["flag"]


def test_plan_tests_one_to_one_for_an_unsplit_catalogue_entry(tmp_path):
    skill = service.get_skill(_ctx(tmp_path), "SKILL-001")
    t41 = next(t for t in skill["tests"] if t["test_id"] == "T4.1")
    assert [e["test_id"] for e in t41["plan_tests"]] == ["T4.1"]


def test_not_testable_plan_test_has_no_primitive_but_keeps_its_flags(tmp_path):
    skill = service.get_skill(_ctx(tmp_path), "SKILL-001")
    t43 = next(t for t in skill["tests"] if t["test_id"] == "T4.3")
    assert [e["flag"] for e in t43["plan_tests"]] == ["RF_CS_PersonalExpense"]
    assert t43["plan_tests"][0]["primitive"] is None


def test_flag_to_test_is_the_reverse_lookup(tmp_path):
    skill = service.get_skill(_ctx(tmp_path), "SKILL-001")
    flag_to_test = skill["flag_to_test"]
    assert flag_to_test["RF_CS_VeryLateBooking"] == "T3.3a_very_late"
    assert flag_to_test["RF_CS_PersonalExpense"] == "T4.3"
    # Every flag named by every plan test is present, and nothing else.
    all_flags = {
        e["flag"]
        for t in skill["tests"]
        for e in t["plan_tests"]
    }
    assert set(flag_to_test) == all_flags


# ── threshold rendering (CLAUDE.md §0.4/G8, P2/P3 gate review item 4) ────────


def test_catalogue_threshold_templates_are_rendered_with_real_values(tmp_path):
    """catalogue.yaml's T3.3a threshold is a template with {..._days}
    placeholders -- get_skill must return it resolved against
    thresholds.yaml, never the raw "{...}" braces a UI would otherwise show
    verbatim (methodology.py, workspace_tne.py's catalogue tab)."""
    skill = service.get_skill(_ctx(tmp_path), "SKILL-001")
    thresholds = skill["thresholds"]
    t33a = next(t for t in skill["tests"] if t["test_id"] == "T3.3a")
    assert "{" not in t33a["threshold"] and "}" not in t33a["threshold"]
    assert str(thresholds["late_booking_domestic_days"]["value"]) in t33a["threshold"]
    assert str(thresholds["late_booking_international_days"]["value"]) in t33a["threshold"]
    assert str(thresholds["very_late_booking_days"]["value"]) in t33a["threshold"]


def test_catalogue_threshold_carries_provenance_for_ui_labelling(tmp_path):
    skill = service.get_skill(_ctx(tmp_path), "SKILL-001")
    t33a = next(t for t in skill["tests"] if t["test_id"] == "T3.3a")
    provenance_ids = {p["id"] for p in t33a["threshold_provenance"]}
    assert provenance_ids == {"late_booking_domestic_days", "late_booking_international_days", "very_late_booking_days"}
    for p in t33a["threshold_provenance"]:
        assert p["type"] == "analyst-set"
        assert p["pending_policy_confirmation"] is True


def test_get_skill_exposes_thresholds_and_risk_control(tmp_path):
    skill = service.get_skill(_ctx(tmp_path), "SKILL-001")
    assert skill["thresholds"]["high_value_limit"]["value"] == 5000
    assert any(c["control_id"] == "CTL-TNE-01" for c in skill["risk_control"]["controls"])


def test_get_skill_manifest_fields_are_not_hardcoded_stale_values(tmp_path):
    """CLAUDE.md P2/P3 gate review item 4: the app's methodology page used to
    hardcode version "1.2"/status "Published" while the real manifest says
    otherwise -- get_skill must be the one source of truth."""
    skill = service.get_skill(_ctx(tmp_path), "SKILL-001")
    import yaml as _yaml

    manifest = _yaml.safe_load((REPO_ROOT / "skills" / "tne_exco" / "manifest.yaml").read_text())
    assert skill["version"] == manifest["version"]
    assert skill["owner"] == manifest["owner"]
    assert skill["status"].lower().replace(" ", "_") == str(manifest["status"]).lower()
