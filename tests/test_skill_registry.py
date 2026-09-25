from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

import pytest
import yaml

from orchestrator.adapters.persistence_local import LocalPersistence
from orchestrator.errors import SkillVersionConflict
from orchestrator.skill_registry import register_skill
from orchestrator.skills import SkillValidationError, load_skill
from tests.conftest import canonical_ts

SKILL_DIR = Path(__file__).parent.parent / "skills" / "tne_exco"


@contextlib.contextmanager
def _count_sql_statements():
    """P3/P4 perf gap review 2026-09-25: same base pattern as
    tests/test_p3_service.py's helper of the same name -- counts SQL
    statements sqlite3 actually executes, across every connection
    LocalPersistence opens during this block, via
    `sqlite3.Connection.set_trace_callback`. Duplicated here (not imported)
    so this file's statement-count tests do not depend on test_p3_service.py's
    internals, with one difference: PRAGMA statements are excluded. Local's
    file-mode `_connect()` opens a BRAND NEW connection per call
    (LocalPersistence._open, CLAUDE.md build brief P1A/P3), and every new
    connection runs `PRAGMA busy_timeout` + `PRAGMA journal_mode=WAL` before
    any real query -- connection-setup noise with no DeltaPersistence
    equivalent (its pooled connections pay that cost once, at pool
    creation), not a round trip this fix is about. Counting only real
    SELECT/INSERT/UPDATE/MERGE statements makes this comparable to
    DeltaPersistence's FakeConnection statement counts in
    tests/test_delta_sql.py."""
    import orchestrator.adapters.persistence_local as pl

    count = [0]
    real_connect = pl.sqlite3.connect

    def _trace(stmt):
        if not stmt.upper().startswith("PRAGMA"):
            count[0] += 1

    def _traced_connect(*a, **k):
        conn = real_connect(*a, **k)
        conn.set_trace_callback(_trace)
        return conn

    pl.sqlite3.connect = _traced_connect
    try:
        yield count
    finally:
        pl.sqlite3.connect = real_connect

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


def test_stored_content_covers_every_file_hashed_into_skill_content_hash(persistence, tne_skill):
    # CLAUDE.md P2/P3 gate review item 9: skill_versions must store the FULL
    # hashed Skill content -- manifest, contract, plan, findings, thresholds,
    # catalogue, risk_control, prompts, custom.py, workspace.py, and every
    # reference data file -- not just the five structured keys most callers
    # read. `files` is that full set, as raw text.
    result = register_skill(tne_skill, persistence, actor="auditor@example.com", now=canonical_ts(0))
    files = result["skill_version"]["content"]["files"]

    for name in ("manifest.yaml", "contract.yaml", "plan.yaml", "findings.yaml",
                 "thresholds.yaml", "risk_control.yaml", "catalogue.yaml",
                 "custom.py", "workspace.py"):
        assert name in files, f"{name} missing from stored skill_versions content"
        assert files[name] == (SKILL_DIR / name).read_text()

    reference_files = [p for p in (SKILL_DIR / "reference").rglob("*") if p.is_file()]
    assert reference_files  # the Skill really does have reference data to cover
    for p in reference_files:
        rel = str(p.relative_to(SKILL_DIR))
        assert rel in files
        assert files[rel] == p.read_text()


def test_rehashing_stored_content_reproduces_skill_content_hash(persistence, tne_skill):
    # The point of storing `files` at all (item 9): the Skill this run used
    # can be reconstructed and VERIFIED from the ledger alone, with no
    # dependency on skills/tne_exco/ still existing on disk in its
    # then-current form.
    from orchestrator.fingerprint import hash_skill_content_entries

    result = register_skill(tne_skill, persistence, actor="auditor@example.com", now=canonical_ts(0))
    files = result["skill_version"]["content"]["files"]

    entries = [(path, text.encode("utf-8")) for path, text in files.items()]
    rehashed = hash_skill_content_entries(entries)
    assert rehashed == result["skill_version"]["content_hash"]
    assert rehashed == tne_skill.content_hash


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


# ── persistence-level idempotence (P3/P4 perf gap review 2026-09-25) ──────────
# register_skill's own ~80s measured live cost was dominated by
# upsert_risks/upsert_controls unconditionally re-running their full
# risk/control seeding pass on EVERY call, even when this exact
# (skill_id, version, content_hash) was already durably recorded. These pin
# the fix at the register_skill level (persistence-level, correct across a
# process restart -- unlike service._ensure_skill_registered's in-process-only
# cache, which sits on top of this and is not exercised by calling
# register_skill directly here) via a real sqlite statement count, and that
# the changed-content contract is unchanged.

def test_reregistering_an_unchanged_skill_issues_exactly_one_statement(tmp_path):
    persistence = LocalPersistence(str(tmp_path / "ledger.db"))
    persistence.migrate()
    skill = load_skill(SKILL_DIR)
    skill.validate()

    register_skill(skill, persistence, actor="auditor@example.com", now=canonical_ts(0))

    with _count_sql_statements() as count:
        result = register_skill(skill, persistence, actor="auditor@example.com", now=canonical_ts(1))

    assert count[0] == 1, (
        f"re-registering an unchanged Skill issued {count[0]} SQL statements, not 1 -- "
        "the persistence-level (skill_id, version, content_hash) check is not short-circuiting "
        "before upsert_risks/upsert_controls"
    )
    assert result["skill_version"]["content_hash"] == skill.content_hash
    assert result["risks_registered"] == 0
    assert result["controls_registered"] == 0


def test_first_registration_of_every_risk_and_control_is_a_small_constant_number_of_statements(tmp_path):
    persistence = LocalPersistence(str(tmp_path / "ledger.db"))
    persistence.migrate()
    skill = load_skill(SKILL_DIR)
    skill.validate()

    with _count_sql_statements() as count:
        result = register_skill(skill, persistence, actor="auditor@example.com", now=canonical_ts(0))

    assert result["risks_registered"] == 13
    assert result["controls_registered"] == 13
    # Old per-row shape: 1 (get_skill_version) + 1 (record_skill_version's own
    # SELECT+INSERT, ~2) + 13 risks x 2 (SELECT+UPDATE/INSERT) + 13 controls x
    # 2 = ~55 statements. Batched: get_skill_version + record_skill_version's
    # SELECT+INSERT + one batched risks existence-check SELECT + one batched
    # risks write + one batched controls write, well under 20 regardless of
    # risk/control count -- never 2x the row count.
    assert count[0] <= 20, (
        f"first registration of 13 risks + 13 controls issued {count[0]} SQL statements -- "
        "check for a reintroduced per-row loop"
    )


def test_register_skill_still_raises_on_a_genuine_content_change_under_the_same_version(tmp_path):
    persistence = LocalPersistence(str(tmp_path / "ledger.db"))
    persistence.migrate()
    skill = load_skill(SKILL_DIR)
    skill.validate()
    register_skill(skill, persistence, actor="auditor@example.com", now=canonical_ts(0))

    skill_copy = tmp_path / "tne_exco_mutated"
    shutil.copytree(SKILL_DIR, skill_copy, ignore=shutil.ignore_patterns("__pycache__"))
    mutated = yaml.safe_load((skill_copy / "risk_control.yaml").read_text())
    mutated["controls"][0]["title"] = mutated["controls"][0]["title"] + " (mutated)"
    (skill_copy / "risk_control.yaml").write_text(yaml.safe_dump(mutated))
    mutated_skill = load_skill(skill_copy)
    assert mutated_skill.skill_id == skill.skill_id
    assert mutated_skill.version == skill.version
    assert mutated_skill.content_hash != skill.content_hash

    # Same (skill_id, version), different content_hash -- the persistence-level
    # idempotence check above must NOT swallow this: the contract is still a
    # raise (CLAUDE.md §3 NN8), same as record_skill_version's own conflict
    # check (test_record_skill_version_same_key_different_hash_conflicts in
    # tests/test_persistence_p2.py).
    with pytest.raises(SkillVersionConflict):
        register_skill(mutated_skill, persistence, actor="auditor@example.com", now=canonical_ts(1))
