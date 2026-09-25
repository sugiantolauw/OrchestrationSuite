"""docs/specs/P7_mapping_authoring_design.md §2.4: a scaffolded Skill loads,
validates and hashes; `timezone` is required; sample inference writes
`pii: true` and `# REVIEW` comments."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.authoring.checks import validate_skill_dir
from orchestrator.authoring.scaffold import SampleSpec, scaffold_skill
from orchestrator.skills import load_skill


def test_scaffolded_skill_loads_validates_and_hashes(tmp_path: Path):
    dest = scaffold_skill(
        tmp_path / "sk",
        skill_id="SKILL-T1", name="Test Skill", domain="test", owner="tester",
        timezone="Australia/Sydney",
    )
    skill = load_skill(dest)
    assert skill.skill_id == "SKILL-T1"
    assert skill.version == "0.1.0-draft"
    assert skill.content_hash

    report = validate_skill_dir(dest)
    assert report.ok, report.to_dict()


def test_timezone_is_a_required_keyword_argument():
    import inspect

    sig = inspect.signature(scaffold_skill)
    assert "timezone" in sig.parameters
    assert sig.parameters["timezone"].default is inspect.Parameter.empty


def test_an_unknown_timezone_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        scaffold_skill(
            tmp_path / "sk",
            skill_id="SKILL-T2", name="Test", domain="test", owner="tester",
            timezone="Not/A_Real_Zone",
        )


def test_sample_inference_writes_pii_true_and_review_comments(tmp_path: Path):
    sample_path = tmp_path / "sample.csv"
    sample_path.write_text("Employee ID,Amount,Note\n1,10.5,a\n2,20.5,b\n")

    dest = scaffold_skill(
        tmp_path / "sk2",
        skill_id="SKILL-T3", name="Test", domain="test", owner="tester",
        timezone="Australia/Sydney",
        sources={"claims": SampleSpec(path=sample_path)},
    )
    contract_text = (dest / "contract.yaml").read_text()
    assert "pii: true" in contract_text
    assert "# REVIEW: inferred from sample" in contract_text

    skill = load_skill(dest)
    columns = skill.contract["sources"]["claims"]["columns"]
    assert columns["Employee ID"]["type"] == "integer"
    assert columns["Amount"]["type"] == "number"
    assert columns["Note"]["type"] == "string"
    assert all(col["pii"] is True for col in columns.values())

    plan = skill.plan
    assert plan["populations"]["raw_claims"] == {"source": "claims"}

    report = validate_skill_dir(dest)
    assert report.ok, report.to_dict()


def test_scaffold_never_writes_custom_or_workspace_py(tmp_path: Path):
    dest = scaffold_skill(
        tmp_path / "sk3",
        skill_id="SKILL-T4", name="Test", domain="test", owner="tester",
        timezone="Australia/Sydney",
    )
    assert not (dest / "custom.py").exists()
    assert not (dest / "workspace.py").exists()
