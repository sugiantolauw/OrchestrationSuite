"""Tests for orchestrator/frames.py's per-run row snapshots (CLAUDE.md build
brief P4 perf fix): a snapshot's rows equal the tested population's row keys
-- checked independently of the scanning logic that built it, never taken on
trust from the same code path (CLAUDE.md §9 "never use a generator as its
own test oracle" applies just as much here) -- snapshot bytes are
deterministic across independent builds, a sha256 mismatch raises rather
than silently re-reading, and a run that predates this change falls back to
the old full-source-read path. Reuses the SKILL-001 planted fixture
(tests/fixtures/tne_planted/data/) test_p3_tne_gates.py's own gates run
against, so this exercises the real Skill rather than a purpose-built stub.
"""

from __future__ import annotations

import dataclasses
import logging

import pandas as pd
import pytest

from orchestrator.adapters.export_storage import LocalExportStorage
from orchestrator.contract import LocalFileDataSource
from orchestrator.engine import execute_skill
from orchestrator.frames import (
    FrameSnapshotIntegrityError,
    build_row_snapshots,
    frame_parquet_bytes,
    read_frame_parquet,
    sha256_bytes,
)
from orchestrator.skills import load_skill
from tests.test_p3_tne_gates import DATA_DIR, SKILL_DIR, pytestmark  # noqa: F401

AUDIT_PERIOD = ("2025-01-01", "2026-04-30")


def _engine_result():
    skill = load_skill(SKILL_DIR)
    skill.validate()
    ds = LocalFileDataSource(root_dir=DATA_DIR, sources=skill.contract["sources"])
    result = execute_skill(
        skill, data_source=ds, audit_period=AUDIT_PERIOD, run_context={"run_id": "frames-unit-test"},
    )
    return skill, result


def _flagged_rows(result) -> list[dict]:
    return [
        {"source": r["__source"], "row_key": r["__row_key"], "flag": r["flag"], "group_id": r["group_id"]}
        for r in result.flags_long.to_dict("records")
    ]


# ── snapshot content == the tested population, checked independently ────────


def test_expense_report_snapshot_equals_the_union_of_its_own_tested_populations():
    skill, result = _engine_result()
    snapshots = build_row_snapshots(skill, result, _flagged_rows(result))

    # Named directly from reading skills/tne_exco/plan.yaml's `tests:` section
    # by hand, independent of orchestrator.frames.tested_population_names'
    # own scan -- this would catch that scan missing or over-including a
    # population, not just confirm it agrees with itself.
    known_tested_populations_for_expense_report = [
        "p_exp", "p_exp_hv", "p_exp_dup", "p_exp_split",
        "p_exp_pref_air_dom", "p_exp_pref_air_int", "p_exp_pref_car_dom", "p_exp_pref_car_int",
        "t42_pop", "t61d_pop_dom", "t61d_pop_int",
    ]
    expected_keys: set[str] = set()
    for name in known_tested_populations_for_expense_report:
        pop = result.population_objects[name]
        assert pop.source == "expense_report"
        expected_keys.update(pop.df["__row_key"].tolist())

    actual_keys = set(snapshots["expense_report"]["__row_key"].tolist())
    assert actual_keys == expected_keys
    assert len(expected_keys) > 0


def test_attendee_validity_snapshot_includes_the_raw_named_population_t42_reads():
    # T4.2 reads `right_population: raw_attendee_validity` (plan.yaml) despite
    # the `raw_` name -- the ONE population outside the reconciliation-only
    # convention that a real test reads. Confirms tested_population_names()
    # picks it up by actual usage, not by a naive "skip anything named raw_*"
    # rule.
    skill, result = _engine_result()
    snapshots = build_row_snapshots(skill, result, _flagged_rows(result))

    expected_keys: set[str] = set()
    for name in ("t33b_pop", "t61c_pop", "raw_attendee_validity"):
        pop = result.population_objects[name]
        assert pop.source == "attendee_validity"
        expected_keys.update(pop.df["__row_key"].tolist())

    assert set(snapshots["attendee_validity"]["__row_key"].tolist()) == expected_keys


def test_unfiltered_population_source_snapshot_equals_the_whole_source():
    # missing_receipt_pop (T4.1's right_population) declares no filters at
    # all, so missing_receipt's snapshot must be the WHOLE source -- checked
    # against DataSourceAdapter.row_count(), an INDEPENDENT count never
    # derived from the population object the snapshot itself was built from
    # (the same independence G6 requires of reconciliation).
    skill, result = _engine_result()
    ds = LocalFileDataSource(root_dir=DATA_DIR, sources=skill.contract["sources"])
    independent_row_count = ds.row_count("missing_receipt", version=result.source_versions["missing_receipt"])

    snapshots = build_row_snapshots(skill, result, _flagged_rows(result))
    assert len(snapshots["missing_receipt"]) == independent_row_count
    assert independent_row_count > 0


def test_snapshot_is_much_smaller_than_the_raw_source_for_expense_report():
    # The whole point of this module (CLAUDE.md build brief P4 perf fix): the
    # snapshot is a small fraction of the raw source, not the ~92,798-row
    # full file get_run_frames used to re-read.
    skill, result = _engine_result()
    snapshots = build_row_snapshots(skill, result, _flagged_rows(result))
    assert len(snapshots["expense_report"]) < len(result.raw_frames["expense_report"])


def test_role_tag_partitions_expense_snapshot_into_prepared_approved_both():
    skill, result = _engine_result()
    snapshots = build_row_snapshots(skill, result, _flagged_rows(result))
    role = snapshots["expense_report"]["role"]
    assert set(role.dropna().unique()) <= {"prepared", "approved", "both"}

    prepared_keys = set(result.population_objects["p_exp_prepared_all"].df["__row_key"])
    approved_keys = set(result.population_objects["p_exp_approved_all"].df["__row_key"])
    for row_key, r in zip(snapshots["expense_report"]["__row_key"], role):
        if row_key in prepared_keys and row_key in approved_keys:
            assert r == "both"
        elif row_key in prepared_keys:
            assert r == "prepared"
        elif row_key in approved_keys:
            assert r == "approved"


# ── determinism (a focused unit test alongside G9's full-pipeline gate) ─────


def test_snapshot_bytes_are_deterministic_across_two_independent_builds():
    skill, result1 = _engine_result()
    _, result2 = _engine_result()
    snaps1 = build_row_snapshots(skill, result1, _flagged_rows(result1))
    snaps2 = build_row_snapshots(skill, result2, _flagged_rows(result2))
    assert set(snaps1) == set(snaps2)
    for source in snaps1:
        assert frame_parquet_bytes(snaps1[source]) == frame_parquet_bytes(snaps2[source]), source


# ── sha256 mismatch raises, never silently re-reads ──────────────────────────


def test_sha256_mismatch_raises_rather_than_re_reading(tmp_path):
    storage = LocalExportStorage(root_dir=tmp_path)
    df = pd.DataFrame({"__source": ["x"], "__row_key": ["v:1"], "Amount": [1.0]})
    content = frame_parquet_bytes(df)
    storage.write("frames/x.parquet", content)

    with pytest.raises(FrameSnapshotIntegrityError):
        read_frame_parquet(storage, source="x", path="frames/x.parquet", expected_sha256="0" * 64)

    got = read_frame_parquet(
        storage, source="x", path="frames/x.parquet", expected_sha256=sha256_bytes(content),
    )
    assert list(got["__row_key"]) == ["v:1"]


# ── fallback path for a run that predates this change ────────────────────────


def test_get_run_frames_falls_back_when_no_snapshot_was_recorded(tmp_path, caplog):
    from orchestrator import service
    from tests.test_p3_service import _build_ctx, _wait_for_status

    ctx = _build_ctx(tmp_path)
    ctx.executor.start()
    try:
        bindings = service.suggest_bindings(ctx, "SKILL-MINI")
        run_id = service.start_audit_run(
            ctx, skill_id="SKILL-MINI", bindings=bindings,
            audit_period=("2026-01-01", "2026-02-28"), objective="fallback path test", run_owner="tester",
        )
        _wait_for_status(ctx, run_id, {"awaiting_signoff", "failed"})

        frames = service.get_run_frames(ctx, run_id)
        assert "RF_HV" in frames["claims"].columns
        real_hv_sum = int(frames["claims"]["RF_HV"].sum())

        # Simulate a pre-fix run: strip state.exports["frames"], which a run
        # completed before this change would never have written.
        state = ctx.persistence.load_state(run_id)
        assert "frames" in state.exports  # the real path really did write one
        stripped = dataclasses.replace(
            state, exports={k: v for k, v in state.exports.items() if k != "frames"},
        )
        ctx.persistence.save_state(stripped)

        with caplog.at_level(logging.WARNING, logger="orchestrator.service"):
            fallback_frames = service.get_run_frames(ctx, run_id)
        assert "RF_HV" in fallback_frames["claims"].columns
        assert int(fallback_frames["claims"]["RF_HV"].sum()) == real_hv_sum
        assert any("falling back" in r.message for r in caplog.records)
    finally:
        ctx.executor.stop()
