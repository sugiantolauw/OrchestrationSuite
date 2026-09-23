from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.skills import Skill, SkillValidationError, load_skill

MINI_SKILL_DIR = Path(__file__).parent / "fixtures" / "skills" / "mini"


def _base_manifest() -> dict:
    return {"id": "SKILL-T", "name": "Test", "domain": "test", "version": "0.1.0", "owner": "test", "status": "draft"}


def _base_contract() -> dict:
    return {
        "timezone": "Australia/Sydney",
        "sources": {
            "claims": {
                "format": "csv",
                "file": "claims.csv",
                "columns": {"Amount": {"type": "number", "nullable": False}},
            }
        },
    }


def _base_thresholds() -> dict:
    return {
        "hv": {
            "value": 5000,
            "unit": "AUD",
            "description": "High value",
            "used_by": ["T1"],
            "effective_date": "2025-01-01",
            "provenance": {"type": "analyst-set", "pending_policy_confirmation": True},
        }
    }


def _base_plan() -> dict:
    return {
        "populations": {"pop": {"source": "claims"}},
        "tests": [
            {
                "test_id": "T1",
                "control_id": "CTL-1",
                "risk_id": "RSK-1",
                "assertion": "operating",
                "flag": "RF_HV",
                "primitive": "threshold_exceedance",
                "params": {
                    "population": "pop",
                    "column": "Amount",
                    "limit": {"threshold": "hv"},
                    "direction": "above",
                    "flag": "RF_HV",
                    "metrics": {"hv_count": {"kind": "count"}},
                },
            }
        ],
    }


def _base_findings() -> dict:
    return {
        "findings": [
            {
                "id": "T1",
                "test_id": "T1",
                "title": "High value",
                "trigger": "hv_count > 0",
                "severity": [{"else": "Low"}],
                "metrics_cited": ["hv_count"],
                "observation": "{hv_count} claims exceeded.",
                "recommendation": "Review.",
            }
        ]
    }


def _skill(**overrides) -> Skill:
    kwargs = dict(
        skill_dir=Path("."),
        manifest=_base_manifest(),
        contract=_base_contract(),
        plan=_base_plan(),
        findings=_base_findings(),
        thresholds=_base_thresholds(),
    )
    kwargs.update(overrides)
    return Skill(**kwargs)


def test_mini_skill_loads_and_validates():
    skill = load_skill(MINI_SKILL_DIR)
    skill.validate()  # must not raise
    assert skill.skill_id == "SKILL-MINI"
    assert skill.version == "0.1.0"
    assert set(t["test_id"] for t in skill.plan["tests"]) == {"T1", "T2", "T3"}


def test_valid_skill_passes():
    _skill().validate()


def test_population_unknown_source_fails():
    plan = _base_plan()
    plan["populations"]["pop"]["source"] = "does_not_exist"
    with pytest.raises(SkillValidationError, match="unknown source"):
        _skill(plan=plan).validate()


def test_population_amount_column_not_in_contract_fails():
    """Item 3 (CLAUDE.md NN14, P2/P3 gate review): a population's declared
    amount_column must be a real column of its contract source -- caught at
    Skill load, not silently zeroed at run time."""
    plan = _base_plan()
    plan["populations"]["pop"]["amount_column"] = "Not A Real Column"
    with pytest.raises(SkillValidationError, match="amount_column"):
        _skill(plan=plan).validate()


def test_population_date_column_not_in_contract_fails():
    plan = _base_plan()
    plan["populations"]["pop"]["date_column"] = "Not A Real Column"
    with pytest.raises(SkillValidationError, match="date_column"):
        _skill(plan=plan).validate()


def test_plan_unknown_reference_fails():
    plan = _base_plan()
    plan["populations"]["pop"]["filters"] = [{"column": "Amount", "op": "in", "value": {"ref": "no_such_ref"}}]
    with pytest.raises(SkillValidationError, match="unknown reference"):
        _skill(plan=plan).validate()


def test_duplicate_test_id_fails():
    plan = _base_plan()
    plan["tests"].append(dict(plan["tests"][0]))
    with pytest.raises(SkillValidationError, match="duplicate test_id"):
        _skill(plan=plan).validate()


def test_flag_must_start_with_rf_prefix():
    plan = _base_plan()
    plan["tests"][0]["flag"] = "NOT_RF"
    plan["tests"][0]["params"]["flag"] = "NOT_RF"
    with pytest.raises(SkillValidationError, match="must start with 'RF_'"):
        _skill(plan=plan).validate()


def test_unknown_primitive_fails():
    plan = _base_plan()
    plan["tests"][0]["primitive"] = "not_a_primitive"
    with pytest.raises(SkillValidationError, match="unknown primitive"):
        _skill(plan=plan).validate()


def test_invalid_primitive_params_fails():
    plan = _base_plan()
    del plan["tests"][0]["params"]["column"]
    with pytest.raises(SkillValidationError, match="params invalid"):
        _skill(plan=plan).validate()


def test_params_flag_must_match_test_flag():
    plan = _base_plan()
    plan["tests"][0]["params"]["flag"] = "RF_DIFFERENT"
    with pytest.raises(SkillValidationError, match="params.flag"):
        _skill(plan=plan).validate()


def test_unknown_population_reference_in_params_fails():
    plan = _base_plan()
    plan["tests"][0]["params"]["population"] = "no_such_population"
    with pytest.raises(SkillValidationError, match="unknown population"):
        _skill(plan=plan).validate()


def test_unknown_threshold_reference_in_params_fails():
    plan = _base_plan()
    plan["tests"][0]["params"]["limit"] = {"threshold": "no_such_threshold"}
    with pytest.raises(SkillValidationError, match="unknown threshold id"):
        _skill(plan=plan).validate()


def test_unknown_reference_in_params_fails():
    plan = _base_plan()
    plan["tests"][0]["primitive"] = "list_membership"
    plan["tests"][0]["params"] = {
        "population": "pop",
        "column": "Amount",
        "allowed_values": {"ref": "no_such_ref"},
        "negate": False,
        "match": "exact",
        "flag": "RF_HV",
        "metrics": {"hv_count": {"kind": "count"}},
    }
    with pytest.raises(SkillValidationError, match="unknown reference"):
        _skill(plan=plan).validate()


def test_threshold_policy_provenance_requires_reference():
    thresholds = _base_thresholds()
    thresholds["hv"]["provenance"] = {"type": "policy", "pending_policy_confirmation": False}
    with pytest.raises(SkillValidationError, match="requires a reference"):
        _skill(thresholds=thresholds).validate()


def test_threshold_analyst_set_provenance_requires_pending_flag():
    thresholds = _base_thresholds()
    thresholds["hv"]["provenance"] = {"type": "analyst-set", "pending_policy_confirmation": False}
    with pytest.raises(SkillValidationError, match="pending_policy_confirmation"):
        _skill(thresholds=thresholds).validate()


def test_finding_metrics_cited_must_be_produced_by_a_test():
    findings = _base_findings()
    findings["findings"][0]["metrics_cited"] = ["hv_count", "not_a_real_metric"]
    with pytest.raises(SkillValidationError, match="not produced by any test"):
        _skill(findings=findings).validate()


def test_finding_trigger_must_compile():
    findings = _base_findings()
    findings["findings"][0]["trigger"] = "__import__('os')"
    with pytest.raises(SkillValidationError, match="trigger invalid"):
        _skill(findings=findings).validate()


def test_finding_severity_when_must_compile():
    findings = _base_findings()
    findings["findings"][0]["severity"] = [{"when": "hv_count > 5", "then": "High"}, {"else": "Low"}]
    with pytest.raises(SkillValidationError, match="severity\\[0\\].when invalid"):
        _skill(findings=findings).validate()


def test_finding_template_placeholder_must_be_cited():
    findings = _base_findings()
    findings["findings"][0]["observation"] = "{hv_count} claims, {uncited_metric} extra."
    with pytest.raises(SkillValidationError, match="not in metrics_cited"):
        _skill(findings=findings).validate()


def test_finding_test_id_must_reference_a_plan_test():
    findings = _base_findings()
    findings["findings"][0]["test_id"] = "T999"
    with pytest.raises(SkillValidationError, match="is not a test in plan.yaml"):
        _skill(findings=findings).validate()


def test_duplicate_finding_id_fails():
    findings = _base_findings()
    findings["findings"].append(dict(findings["findings"][0]))
    with pytest.raises(SkillValidationError, match="duplicate finding id"):
        _skill(findings=findings).validate()


def test_load_skill_missing_required_file(tmp_path: Path):
    (tmp_path / "manifest.yaml").write_text("id: X\nname: X\ndomain: x\nversion: '1'\nowner: x\nstatus: draft\n")
    with pytest.raises(SkillValidationError, match="missing required Skill file"):
        load_skill(tmp_path)


def test_content_hash_changes_when_any_file_byte_changes(tmp_path: Path):
    import shutil

    skill_copy = tmp_path / "mini"
    shutil.copytree(MINI_SKILL_DIR, skill_copy)
    original = load_skill(skill_copy).content_hash

    plan_path = skill_copy / "plan.yaml"
    plan_path.write_text(plan_path.read_text() + "\n# a trailing comment byte change\n")
    changed = load_skill(skill_copy).content_hash

    assert original != changed


def test_content_hash_changes_for_reference_and_custom_py(tmp_path: Path):
    import shutil

    skill_copy = tmp_path / "mini"
    shutil.copytree(MINI_SKILL_DIR, skill_copy)
    base_hash = load_skill(skill_copy).content_hash

    (skill_copy / "reference").mkdir()
    (skill_copy / "reference" / "extra.yaml").write_text("- a\n- b\n")
    with_reference_hash = load_skill(skill_copy).content_hash
    assert with_reference_hash != base_hash

    (skill_copy / "custom.py").write_text("CUSTOM_PRIMITIVES = {}\nCUSTOM_DERIVATIONS = {}\n")
    with_custom_hash = load_skill(skill_copy).content_hash
    assert with_custom_hash != with_reference_hash
