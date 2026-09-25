"""src/runs_page.py — BUG-RUNS-1's fix (live test round 3, mirroring
tests/test_trace_page.py exactly): the /runs page's "Filter by Skill" and
"Status" dropdowns must actually filter the runs list. Rendered against the
REAL orchestrator.service + LocalPersistence (overriding this directory's
autouse fake_backend fixture)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_app_entry
from orchestrator.state import RunState
from orchestrator.timeutil import utc_now
from src import runs_page
from src.platform import adapters


@pytest.fixture
def real_ctx(monkeypatch, tmp_path):
    from orchestrator import service as real_service

    repo_root = Path(__file__).resolve().parents[2]
    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(tmp_path),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(repo_root / "skills"),
    }
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))

    ctx = adapters.get_context()
    try:
        yield ctx
    finally:
        ctx.executor.stop()
        adapters._ctx = None


def _fingerprint(fp_id: str) -> dict:
    return dict(
        fingerprint_id=fp_id,
        source_table_versions="{}",
        uploaded_file_hashes="{}",
        reference_data_hashes="{}",
        skill_content_hash=None,
        code_revision="rev1",
        dependency_lock_hash="dep1",
        runtime_config_hash="rc1",
        endpoint_config="{}",
        prompt_template_version="none",
        created_at=utc_now(),
    )


def _make_run(ctx, run_id: str, *, objective: str, status: str) -> None:
    """Explorer-mode runs resolve skill_name to f"Explorer: {objective}"
    (orchestrator/service.py list_runs) without needing a registered Skill
    -- the simplest way to get two runs with distinct, predictable
    skill_name values to filter on."""
    now = utc_now()
    state = RunState(
        run_id=run_id,
        run_kind="fieldwork",
        engagement_id="ENG-DEFAULT",
        mode="explorer",
        phase="plan",
        audit_period=("2026-01-01", "2026-01-31"),
        objective=objective,
        run_owner="alice",
        fingerprint_id=f"FP-{run_id}",
        created_at=now,
        last_state_change_at=now,
        status=status,
    )
    ctx.persistence.create_run(state, _fingerprint(f"FP-{run_id}"))


def test_clearing_both_filters_shows_every_run_default(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="running")

    rows = runs_page._rows_for_filter(None, None)
    text = str(rows)
    assert "RUN-A" in text
    assert "RUN-B" in text


def test_skill_filter_narrows_to_the_matching_skill_name(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="completed")

    rows = runs_page._rows_for_filter("Explorer: Alpha objective", None)
    text = str(rows)
    assert "RUN-A" in text
    assert "RUN-B" not in text


def test_status_filter_narrows_to_the_matching_status(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="running")

    rows = runs_page._rows_for_filter(None, "Running")
    text = str(rows)
    assert "RUN-B" in text
    assert "RUN-A" not in text


def test_combined_skill_and_status_filters_apply_together(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Alpha objective", status="running")
    _make_run(real_ctx, "RUN-C", objective="Beta objective", status="completed")

    rows = runs_page._rows_for_filter("Explorer: Alpha objective", "Completed")
    text = str(rows)
    assert "RUN-A" in text
    assert "RUN-B" not in text
    assert "RUN-C" not in text


def test_needs_review_matches_runs_waiting_at_either_gate(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="awaiting_confirmation")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="awaiting_signoff")
    _make_run(real_ctx, "RUN-C", objective="Gamma objective", status="completed")

    text = str(runs_page._rows_for_filter(None, "Needs review"))
    assert "RUN-A" in text
    assert "RUN-B" in text
    assert "RUN-C" not in text


def test_empty_string_filter_values_behave_like_cleared(real_ctx):
    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")

    rows = runs_page._rows_for_filter("", "")
    assert "RUN-A" in str(rows)


def test_filtering_issues_exactly_one_call_not_one_per_run(real_ctx, monkeypatch):
    """CLAUDE.md §2.3 rule 4 / orchestrator/service.py list_runs' own N+1
    fix: filtering must not turn one page render into one query per run."""
    calls: list[int] = []
    original = adapters.list_audit_runs

    def _tracked():
        calls.append(1)
        return original()

    monkeypatch.setattr(adapters, "list_audit_runs", _tracked)

    _make_run(real_ctx, "RUN-A", objective="Alpha objective", status="completed")
    _make_run(real_ctx, "RUN-B", objective="Beta objective", status="running")
    _make_run(real_ctx, "RUN-C", objective="Gamma objective", status="failed")

    runs_page._rows_for_filter("Explorer: Alpha objective", None)
    assert len(calls) == 1


def test_callback_is_registered_on_the_real_app(real_ctx):
    entry = load_app_entry()
    assert "runs-list.children" in entry.app.callback_map
