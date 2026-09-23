from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from orchestrator.skill_registry import register_skill
from orchestrator.skills import SkillValidationError, load_skill
from tests.conftest import canonical_ts

SKILL_DIR = Path(__file__).parent.parent / "skills" / "tne_exco"

# P2: register_skill (CLAUDE.md §4.6, §4.8, §4.9). Runs against every persistence
# backend (local_memory, local_file, delta) via the `persistence` fixture in
# conftest.py, same contract as tests/test_persistence_p2.py -- `register_skill`
# only calls persistence methods that file already exercises directly, so this
# file focuses on the registration wiring, not persistence internals.


@pytest.fixture(scope="module")
def tne_skill():
    skill = load_skill(SKILL_DIR)
    skill.validate()
    return skill


def test_register_skill_records_the_skill_version(persistence, tne_skill):
    result = register_skill(tne_skill, persistence, actor="auditor@example.com", now=canonical_ts(0))

    row = result["skill_version"]
    assert row["skill_id"] == "SKILL-001"
    assert row["version"] == tne_skill.version
    assert row["content_hash"] == tne_skill.content_hash
    assert row["content"]["manifest"]["id"] == "SKILL-001"
    assert row["content"]["plan"] == tne_skill.plan
    assert row["content"]["findings"] == tne_skill.findings
    assert row["content"]["thresholds"] == tne_skill.thresholds

    stored = persistence.get_skill_version("SKILL-001", tne_skill.version)
    assert stored is not None
    assert stored["content_hash"] == tne_skill.content_hash


def test_register_skill_seeds_every_risk_and_control_from_risk_control_yaml(persistence, tne_skill):
    result = register_skill(tne_skill, persistence, actor="auditor@example.com", now=canonical_ts(0))

    # skills/tne_exco/risk_control.yaml declares 13 risks and 13 controls, one
    # pair per test_catalogue.py control_objective (CLAUDE.md §4.9).
    assert result["risks_registered"] == 13
    assert result["controls_registered"] == 13

    risks = {r["risk_id"]: r for r in persistence.list_risks()}
    assert risks["RSK-TNE-01"]["title"] == "Unapproved travel is undertaken and reimbursed"
    assert risks["RSK-TNE-01"]["status"] == "proposed"
    assert risks["RSK-TNE-01"]["source"] == "manual"
    assert risks["RSK-TNE-01"]["engagement_id"] is None  # register entry, not engagement-scoped

    controls = {c["control_id"]: c for c in persistence.list_controls()}
    assert controls["CTL-TNE-01"]["risk_id"] == "RSK-TNE-01"
    assert controls["CTL-TNE-01"]["title"] == "All travel should be pre-approved before expenses are incurred"
    assert controls["CTL-TNE-01"]["engagement_id"] is None


def test_register_skill_is_idempotent(persistence, tne_skill):
    first = register_skill(tne_skill, persistence, actor="alice", now=canonical_ts(0))
    second = register_skill(tne_skill, persistence, actor="alice", now=canonical_ts(1))

    assert first["skill_version"]["content_hash"] == second["skill_version"]["content_hash"]
    versions = [
        v for v in persistence.list_skill_versions("SKILL-001") if v["content_hash"] == tne_skill.content_hash
    ]
    assert len(versions) == 1

    risk_rows = [r for r in persistence.list_risks() if r["risk_id"] == "RSK-TNE-01"]
    assert len(risk_rows) == 1
    control_rows = [c for c in persistence.list_controls() if c["control_id"] == "CTL-TNE-01"]
    assert len(control_rows) == 1


def test_register_skill_every_control_risk_id_matches_a_registered_risk(persistence, tne_skill):
    register_skill(tne_skill, persistence, actor="auditor@example.com", now=canonical_ts(0))
    risk_ids = {r["risk_id"] for r in persistence.list_risks()}
    for control in persistence.list_controls():
        if control["control_id"].startswith("CTL-TNE-"):
            assert control["risk_id"] in risk_ids


def test_every_control_title_equals_the_catalogue_control_objective_it_was_seeded_from(tne_skill):
    # N10: risk_control.yaml's controls are seeded FROM
    # reference_app/src/test_catalogue.py's control_objective (CLAUDE.md §4.9,
    # P2 DoD) -- pinned here so the two can never drift silently. A control's
    # `tests` list names every test_id sharing that control_objective in
    # catalogue.yaml; every one of them must agree with the control's title.
    risk_control = yaml.safe_load((SKILL_DIR / "risk_control.yaml").read_text())
    catalogue = yaml.safe_load((SKILL_DIR / "catalogue.yaml").read_text())
    catalogue_objective = {t["test_id"]: t["control_objective"] for t in catalogue["tests"]}

    def _catalogue_objective_for(plan_test_id: str) -> str:
        # plan.yaml/risk_control.yaml split some catalogue tests into
        # per-class/per-region sub-tests (e.g. catalogue T3.2a -> plan
        # T3.2a_air_dom, T3.2a_air_int, ...) -- match on the catalogue test_id
        # itself, or as the prefix before the first '_'.
        if plan_test_id in catalogue_objective:
            return catalogue_objective[plan_test_id]
        base = plan_test_id.split("_", 1)[0]
        assert base in catalogue_objective, f"{plan_test_id}: neither it nor {base!r} is in catalogue.yaml"
        return catalogue_objective[base]

    assert risk_control["controls"], "risk_control.yaml declared no controls"
    for control in risk_control["controls"]:
        for test_id in control["tests"]:
            assert control["title"] == _catalogue_objective_for(test_id), (
                f"{control['control_id']} title != catalogue.yaml control_objective for {test_id}"
            )


def test_skill_content_hash_changes_when_risk_control_yaml_changes(tne_skill, tmp_path):
    # N10: risk_control.yaml is part of the Skill's content hash.
    skill_copy = tmp_path / "tne_exco"
    shutil.copytree(SKILL_DIR, skill_copy, ignore=shutil.ignore_patterns("__pycache__"))
    mutated = yaml.safe_load((skill_copy / "risk_control.yaml").read_text())
    mutated["controls"][0]["title"] = mutated["controls"][0]["title"] + " (mutated)"
    (skill_copy / "risk_control.yaml").write_text(yaml.safe_dump(mutated))
    mutated_skill = load_skill(skill_copy)
    assert mutated_skill.content_hash != tne_skill.content_hash


def test_skill_content_hash_changes_when_catalogue_yaml_changes(tne_skill, tmp_path):
    skill_copy = tmp_path / "tne_exco"
    shutil.copytree(SKILL_DIR, skill_copy, ignore=shutil.ignore_patterns("__pycache__"))
    mutated = yaml.safe_load((skill_copy / "catalogue.yaml").read_text())
    mutated["tests"][0]["rule"] = mutated["tests"][0]["rule"] + " (mutated)"
    (skill_copy / "catalogue.yaml").write_text(yaml.safe_dump(mutated))
    mutated_skill = load_skill(skill_copy)
    assert mutated_skill.content_hash != tne_skill.content_hash


def test_missing_risk_control_yaml_is_a_validation_error(tmp_path):
    skill_copy = tmp_path / "tne_exco"
    shutil.copytree(SKILL_DIR, skill_copy, ignore=shutil.ignore_patterns("__pycache__"))
    (skill_copy / "risk_control.yaml").unlink()
    with pytest.raises(SkillValidationError):
        load_skill(skill_copy)
