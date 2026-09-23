from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.skill_registry import register_skill
from orchestrator.skills import load_skill
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
