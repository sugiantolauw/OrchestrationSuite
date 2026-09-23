"""The real G10 for SKILL-001 (CLAUDE.md P2/P3 gate review item 6): a
population regenerated from tests/fixtures/tne_planted/plants.yaml with
every planted exception AND near-miss negative removed -- pure background
rows, "clearly non-exceptions" by generate.py's own construction -- run
through the real SKILL-001 Skill (skills/tne_exco/, its actual plan.yaml/
findings.yaml/thresholds.yaml, not a stand-in) must produce ZERO findings.

This complements, not replaces, tests/test_p3_tne_gates.py's G10 (run
against the "mini" Skill fixture): that file's own comment explains why it
avoids re-deriving "which rows are the plants" from plants.yaml's internal
schema -- CLAUDE.md §9's "never use a generator as its own test oracle".
This test does not re-derive anything: it asks generate.py to emit a
population from a plants.yaml with the ENTIRE `tests:` section removed, so
there is nothing to decide -- every remaining row is a background row
generate.py's own background builders produce, independent of any test's
scoring logic. If a primitive, a threshold or a finding rule spontaneously
fires against that population, the bug is in the Skill/primitives, not in
this test's judgement of what counts as an exception."""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

import pytest
import yaml

from orchestrator import runs as runs_module
from orchestrator.adapters.export_storage import LocalExportStorage
from orchestrator.contract import LocalFileDataSource
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import classify, discover, execute, find, prioritise
from orchestrator.skills import load_skill
from tests.conftest import canonical_ts
from tests.fixtures.tne_planted.generate import generate

REPO_ROOT = Path(__file__).parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
PLANTS_PATH = REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "plants.yaml"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

pytestmark = pytest.mark.skipif(
    not PLANTS_PATH.is_file(), reason="tests/fixtures/tne_planted/plants.yaml not present"
)


class _Settings:
    catalog = None
    schema = None


def _fingerprint(fp_id: str, skill_content_hash: str | None) -> dict:
    return dict(
        fingerprint_id=fp_id, source_table_versions="{}", uploaded_file_hashes="{}",
        reference_data_hashes="{}", skill_content_hash=skill_content_hash, code_revision="rev1",
        dependency_lock_hash="dep1", runtime_config_hash="rc1", endpoint_config="{}",
        prompt_template_version="none", created_at=canonical_ts(0),
    )


def test_skill001_real_skill_produces_zero_findings_on_a_no_plants_population(local_persistence, tmp_path):
    doc = yaml.safe_load(PLANTS_PATH.read_text())
    # Strip every planted exception AND near-miss negative -- _collect_rows
    # (generate.py) walks doc["tests"], so removing the key entirely leaves
    # every source with only its seeded background rows.
    no_plants_doc = {"seed": doc.get("seed"), "background": doc.get("background", {})}
    no_plants_path = tmp_path / "plants_no_exceptions.yaml"
    no_plants_path.write_text(yaml.safe_dump(no_plants_doc))

    data_dir = tmp_path / "data"
    generate(no_plants_path, data_dir)

    skill = load_skill(SKILL_DIR)
    skill.validate()
    data_source = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}

    now = canonical_ts(1)
    fp = _fingerprint("FP-G10-SKILL001-NOPLANTS", skill.content_hash)
    state = runs_module.create_run(
        local_persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=skill.skill_id,
        skill_version=skill.version, mode="playbook", audit_period=AUDIT_PERIOD,
        objective="G10: real SKILL-001, no-plants population", run_owner="gatebot",
        options={"auto_confirm_plan": True}, fingerprint=fp, now=now,
    )
    data_assets = [{"source": n, "table_fqn": n, "version": v} for n, v in source_versions.items()]
    state = local_persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    export_dir = Path(tempfile.mkdtemp(prefix="g10-skill001-noplants-exports-"))
    ctx = NodeContext(
        settings=_Settings(), persistence=local_persistence, data_source=data_source,
        skill=skill, clock=lambda: canonical_ts(2), export_storage=LocalExportStorage(root_dir=export_dir),
    )

    state = discover(ctx, state)
    state = execute(ctx, state)  # must not raise (G6/G7 conformance, and every primitive's empty/near-empty safety)
    state = classify(ctx, state)
    state = find(ctx, state)
    state = prioritise(ctx, state)  # must not raise (item 5's exposure de-duplication)

    findings = local_persistence.list_findings(state.run_id)
    assert findings == [], f"a no-plants population produced {len(findings)} finding(s): {[f['test_id'] for f in findings]}"
    assert state.findings == []
