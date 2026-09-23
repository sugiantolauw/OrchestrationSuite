"""Tier A gates against the real SKILL-001 T&E Skill (CLAUDE.md §5), run
through the `execute` node rather than orchestrator.engine.execute_skill
directly, using the small planted-exception fixture
(tests/fixtures/tne_planted/data/, owned by the P2 primitives work -- read
only, never modified here) so this runs in seconds rather than the minutes a
full synthetic_data/ pass takes (that pass lives in
tests/test_p3_synthetic_full_run.py).

  G6  Reconciliation      -- every source's row count as the engine saw it
                             equals an independently obtained count at the
                             same pinned version; a real, injected variance
                             fails the run.
  G7  Contract conformance -- a bound source missing a declared contract
                             column fails the run with a contract message,
                             never a silent default.
  G9  Cross-process determinism -- two independent executions (two separate
                             persistence instances, two fresh NodeContexts)
                             over the same config + data produce byte-
                             identical non-narrative output: run_metrics,
                             flagged_rows, findings.
  G10 Negative control     -- a clean population (every plant removed) with
                             the SAME contract shape produces zero findings.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from orchestrator import runs as runs_module
from orchestrator.adapters.export_storage import LocalExportStorage
from orchestrator.contract import ContractViolation, LocalFileDataSource
from orchestrator.errors import ReconciliationError
from orchestrator.nodes.context import NodeContext
from orchestrator.nodes.fieldwork import discover, execute, find
from orchestrator.skills import load_skill
from tests.conftest import canonical_ts

REPO_ROOT = Path(__file__).parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "tne_exco"
DATA_DIR = REPO_ROOT / "tests" / "fixtures" / "tne_planted" / "data"
AUDIT_PERIOD = ("2025-01-01", "2026-04-30")

pytestmark = pytest.mark.skipif(
    not DATA_DIR.is_dir(), reason="tests/fixtures/tne_planted/data/ not present -- run generate.py first"
)


def _fingerprint(fp_id: str, skill_content_hash: str | None) -> dict:
    return dict(
        fingerprint_id=fp_id,
        source_table_versions="{}",
        uploaded_file_hashes="{}",
        reference_data_hashes="{}",
        skill_content_hash=skill_content_hash,
        code_revision="rev1",
        dependency_lock_hash="dep1",
        runtime_config_hash="rc1",
        endpoint_config="{}",
        prompt_template_version="none",
        created_at=canonical_ts(0),
    )


class _Settings:
    catalog = None
    schema = None


def _make_ctx_and_state(persistence, data_dir: Path, *, run_id: str):
    skill = load_skill(SKILL_DIR)
    skill.validate()
    data_source = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}

    now = canonical_ts(1)
    fp = _fingerprint(f"FP-{run_id}", skill.content_hash)
    state = runs_module.create_run(
        persistence, run_id=run_id, run_kind="fieldwork", engagement_id="ENG-DEFAULT",
        skill_id=skill.skill_id, skill_version=skill.version, mode="playbook",
        audit_period=AUDIT_PERIOD, objective="gate test", run_owner="gatebot",
        options={"auto_confirm_plan": True}, fingerprint=fp, now=now,
    )
    data_assets = [
        {"source": name, "table_fqn": name, "version": v} for name, v in source_versions.items()
    ]
    state = persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    export_dir = Path(tempfile.mkdtemp(prefix="tne-gates-exports-"))
    ctx = NodeContext(
        settings=_Settings(), persistence=persistence, data_source=data_source,
        skill=skill, clock=lambda: canonical_ts(2),
        export_storage=LocalExportStorage(root_dir=export_dir),
    )
    return ctx, state


# ── G6 ────────────────────────────────────────────────────────────────────────


def test_g6_reconciliation_passes_with_zero_variance(local_persistence, uid):
    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-G6-OK-{uid}")
    state = discover(ctx, state)
    state = execute(ctx, state)
    assert state.reconciliation  # every one of the 8 contract sources present
    for source, rec in state.reconciliation.items():
        assert rec["variance"] == 0, f"{source}: {rec}"
        assert rec["engine_rows"] == rec["independent_rows"]


def test_g6_reconciliation_fails_on_injected_variance(local_persistence, uid):
    ctx, state = _make_ctx_and_state(local_persistence, DATA_DIR, run_id=f"RUN-G6-BAD-{uid}")
    state = discover(ctx, state)

    real_row_count = ctx.data_source.row_count

    def lying_row_count(source, *, version=None):
        n = real_row_count(source, version=version)
        return n + 1 if source == "expense_report" else n

    ctx.data_source.row_count = lying_row_count
    try:
        with pytest.raises(ReconciliationError) as exc:
            execute(ctx, state)
    finally:
        ctx.data_source.row_count = real_row_count
    assert "expense_report" in str(exc.value)


# ── G7 ────────────────────────────────────────────────────────────────────────


def test_g7_contract_violation_fails_with_named_column(local_persistence, tmp_path, uid):
    corrupted_dir = tmp_path / "corrupted"
    corrupted_dir.mkdir()
    for p in DATA_DIR.iterdir():
        (corrupted_dir / p.name).write_bytes(p.read_bytes())

    df = pd.read_csv(corrupted_dir / "Complete_Per_Diem_Rates_by_Country_Region.csv")
    df = df.drop(columns=["Rate Per Day (S$)"])
    df.to_csv(corrupted_dir / "Complete_Per_Diem_Rates_by_Country_Region.csv", index=False)

    ctx, state = _make_ctx_and_state(local_persistence, corrupted_dir, run_id=f"RUN-G7-{uid}")
    state = discover(ctx, state)
    with pytest.raises(ContractViolation) as exc:
        execute(ctx, state)
    assert "Rate Per Day (S$)" in str(exc.value)


# ── G9 ────────────────────────────────────────────────────────────────────────


def test_g9_two_independent_executions_are_byte_identical(local_persistence_db_path, tmp_path, uid):
    from orchestrator.adapters.persistence_local import LocalPersistence

    results = []
    for i in (1, 2):
        db_path = str(tmp_path / f"g9_{i}.db")
        persistence = LocalPersistence(db_path)
        persistence.migrate()
        ctx, state = _make_ctx_and_state(persistence, DATA_DIR, run_id=f"RUN-G9-{uid}")
        state = discover(ctx, state)
        state = execute(ctx, state)
        state = find(ctx, state)

        metrics = persistence.get_run_metrics(state.run_id)
        flagged = sorted(
            persistence.list_flagged_rows(state.run_id),
            key=lambda r: (r["source"], r["row_key"], r["flag"]),
        )
        findings = persistence.list_findings(state.run_id)
        # Narrative fields are excluded by construction -- P3 never populates
        # them (no LLM calls, CLAUDE.md §3 NN2), so nothing to strip here, but
        # observation/recommendation ARE deterministic template text over
        # metrics_cited and legitimately part of the non-narrative surface.
        #
        # Frame snapshots (orchestrator/frames.py, CLAUDE.md build brief P4
        # perf fix) are non-narrative run output too: the `execute` node just
        # wrote one Parquet file per contract source into this iteration's own
        # export_storage. Read every one of THOSE bytes back (never re-derive
        # them) so a divergence in the snapshot-writing path itself -- not
        # just in the numbers that feed it -- would fail this gate.
        frame_exports = state.exports["frames"]
        frame_bytes = {
            source: ctx.export_storage.read(meta["path"]) for source, meta in sorted(frame_exports.items())
        }
        results.append(
            {
                "metrics": json.dumps(metrics, sort_keys=True),
                "flagged": json.dumps(flagged, sort_keys=True),
                "findings": json.dumps(
                    [{k: v for k, v in f.items() if k not in ("created_at", "updated_at")} for f in findings],
                    sort_keys=True,
                ),
                "frame_sources": sorted(frame_exports),
                "frame_sha256": {source: meta["sha256"] for source, meta in sorted(frame_exports.items())},
                "frame_bytes": frame_bytes,
            }
        )

    assert results[0]["metrics"] == results[1]["metrics"]
    assert results[0]["flagged"] == results[1]["flagged"]
    assert results[0]["findings"] == results[1]["findings"]
    assert results[0]["frame_sources"] == results[1]["frame_sources"]
    assert len(results[0]["frame_sources"]) == 8  # one snapshot per SKILL-001 contract source
    assert results[0]["frame_sha256"] == results[1]["frame_sha256"]
    assert results[0]["frame_bytes"] == results[1]["frame_bytes"]


# ── G10 ───────────────────────────────────────────────────────────────────────


# G10 is deliberately run against the "mini" Skill fixture rather than
# tne_exco: plants.yaml's natural_id -> row mapping is internal to
# generate.py (owned by the P2 primitives work) and re-deriving "which rows
# are the plants" here to strip them would risk re-implementing that
# generator's own logic rather than testing independently of it (exactly
# what CLAUDE.md §9 warns against: "never use a generator as its own test
# oracle"). The mini Skill's clean-by-construction dataset (every claim
# under the high-value limit, every claim matched in the register) is a
# clean population this file fully controls and can assert on plainly.


def test_g10_negative_control_zero_findings(local_persistence, tmp_path):
    from tests.test_p3_nodes import MINI_SKILL_DIR, _fingerprint as _mini_fingerprint

    data_dir = tmp_path / "clean"
    data_dir.mkdir()
    # T2's primitive config (mini/plan.yaml) is mode: semi -- it flags LEFT
    # rows that DO have a matching right-side row (a SQL semi-join), so a
    # clean population for T2's "missing_count > 0" trigger to stay false is
    # one where NO claim matches any register row on keys, not the reverse.
    claims = pd.DataFrame(
        [
            {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Vendor": "VendorA"},
            {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 200, "Vendor": "VendorB"},
        ]
    )
    register = pd.DataFrame(
        [
            {"Employee ID": 9, "Transaction Date": "2026-01-09", "Vendor": "VendorZ"},
        ]
    )
    claims.to_csv(data_dir / "claims.csv", index=False)
    register.to_csv(data_dir / "register.csv", index=False)

    skill = load_skill(MINI_SKILL_DIR)
    skill.validate()
    data_source = LocalFileDataSource(root_dir=data_dir, sources=skill.contract["sources"])
    source_versions = {name: data_source.resolve_version(name) for name in skill.contract["sources"]}

    now = canonical_ts(1)
    fp = _mini_fingerprint("FP-G10-CLEAN", skill.content_hash)
    state = runs_module.create_run(
        local_persistence, run_kind="fieldwork", engagement_id="ENG-DEFAULT", skill_id=skill.skill_id,
        skill_version=skill.version, mode="playbook", audit_period=("2026-01-01", "2026-02-28"),
        objective="G10", run_owner="gatebot", options={"auto_confirm_plan": True}, fingerprint=fp, now=now,
    )
    data_assets = [{"source": n, "table_fqn": n, "version": v} for n, v in source_versions.items()]
    state = local_persistence.save_state(dataclasses.replace(state, data_assets=data_assets))

    export_dir = Path(tempfile.mkdtemp(prefix="tne-gates-g10-exports-"))
    ctx = NodeContext(
        settings=_Settings(), persistence=local_persistence, data_source=data_source,
        skill=skill, clock=lambda: canonical_ts(2),
        export_storage=LocalExportStorage(root_dir=export_dir),
    )
    state = discover(ctx, state)
    state = execute(ctx, state)
    assert local_persistence.get_run_metrics(state.run_id)["hv_count"]["value"] == 0
    assert local_persistence.get_run_metrics(state.run_id)["missing_count"]["value"] == 0

    state = find(ctx, state)
    findings = local_persistence.list_findings(state.run_id)
    assert findings == []
    assert state.findings == []
