"""src/actions_page.py — BUG-ACTIONS-1's fix (live test round 3, mirroring
tests/test_trace_page.py exactly): the /actions page's "Filter by Skill",
"Risk level" and "Status" dropdowns must actually filter the actions
table. Rendered against the REAL orchestrator.service + LocalPersistence
(overriding this directory's autouse fake_backend fixture)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_app_entry
from orchestrator.timeutil import utc_now
from src import actions_page
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


def _action(action_id: str, *, skill_id: str, risk: str, status: str) -> dict:
    return {
        "action_id": action_id,
        "title": f"{action_id} finding",
        "skill_id": skill_id,
        "risk": risk,
        "status": status,
    }


def _make_action(ctx, run_id: str, action_id: str, *, skill_id: str, risk: str, status: str) -> None:
    """No registered Skill named `skill_id` -- list_management_actions'
    skill_name resolution then falls back to the raw skill_id itself
    (orchestrator/service.py), which is deterministic and enough to filter
    on without needing a real Skill ledger entry."""
    ctx.persistence.write_management_actions(
        run_id,
        [_action(action_id, skill_id=skill_id, risk=risk, status=status)],
        now=utc_now(),
    )


def test_clearing_all_filters_shows_every_action_default(real_ctx):
    _make_action(real_ctx, "RUN-A", "ACT-A", skill_id="SKILL-X", risk="High", status="open")
    _make_action(real_ctx, "RUN-B", "ACT-B", skill_id="SKILL-Y", risk="Low", status="closed")

    rows = actions_page._rows_for_filter(None, None, None)
    text = str(rows)
    assert "ACT-A finding" in text
    assert "ACT-B finding" in text


def test_skill_filter_narrows_to_the_matching_skill(real_ctx):
    _make_action(real_ctx, "RUN-A", "ACT-A", skill_id="SKILL-X", risk="High", status="open")
    _make_action(real_ctx, "RUN-B", "ACT-B", skill_id="SKILL-Y", risk="High", status="open")

    rows = actions_page._rows_for_filter("SKILL-X", None, None)
    text = str(rows)
    assert "ACT-A finding" in text
    assert "ACT-B finding" not in text


def test_risk_filter_narrows_to_the_matching_risk(real_ctx):
    _make_action(real_ctx, "RUN-A", "ACT-A", skill_id="SKILL-X", risk="High", status="open")
    _make_action(real_ctx, "RUN-B", "ACT-B", skill_id="SKILL-X", risk="Low", status="open")

    rows = actions_page._rows_for_filter(None, "High", None)
    text = str(rows)
    assert "ACT-A finding" in text
    assert "ACT-B finding" not in text


def test_status_filter_narrows_to_the_matching_status(real_ctx):
    _make_action(real_ctx, "RUN-A", "ACT-A", skill_id="SKILL-X", risk="High", status="open")
    _make_action(real_ctx, "RUN-B", "ACT-B", skill_id="SKILL-X", risk="High", status="closed")

    rows = actions_page._rows_for_filter(None, None, "Open")
    text = str(rows)
    assert "ACT-A finding" in text
    assert "ACT-B finding" not in text


def test_status_filter_matches_case_insensitively(real_ctx):
    """CLAUDE.md NN14 / orchestrator/service.py list_management_actions:
    a persisted "under_review" status renders as "Under Review", but the
    prototype's own frozen dropdown option text is "Under review" -- the
    filter must still match, or this control could never select that
    status at all."""
    _make_action(real_ctx, "RUN-A", "ACT-A", skill_id="SKILL-X", risk="High", status="under_review")

    rows = actions_page._rows_for_filter(None, None, "Under review")
    assert "ACT-A finding" in str(rows)


def test_combined_filters_apply_together(real_ctx):
    _make_action(real_ctx, "RUN-A", "ACT-A", skill_id="SKILL-X", risk="High", status="open")
    _make_action(real_ctx, "RUN-B", "ACT-B", skill_id="SKILL-X", risk="Low", status="open")
    _make_action(real_ctx, "RUN-C", "ACT-C", skill_id="SKILL-Y", risk="High", status="open")

    rows = actions_page._rows_for_filter("SKILL-X", "High", "Open")
    text = str(rows)
    assert "ACT-A finding" in text
    assert "ACT-B finding" not in text
    assert "ACT-C finding" not in text


def test_empty_string_filter_values_behave_like_cleared(real_ctx):
    _make_action(real_ctx, "RUN-A", "ACT-A", skill_id="SKILL-X", risk="High", status="open")

    rows = actions_page._rows_for_filter("", "", "")
    assert "ACT-A finding" in str(rows)


def test_filtering_issues_exactly_one_call_not_one_per_action(real_ctx, monkeypatch):
    """CLAUDE.md §2.3 rule 4: filtering must not turn one page render into
    one query per action."""
    calls: list[int] = []
    original = adapters.list_management_actions

    def _tracked():
        calls.append(1)
        return original()

    monkeypatch.setattr(adapters, "list_management_actions", _tracked)

    _make_action(real_ctx, "RUN-A", "ACT-A", skill_id="SKILL-X", risk="High", status="open")
    _make_action(real_ctx, "RUN-B", "ACT-B", skill_id="SKILL-Y", risk="Low", status="closed")

    actions_page._rows_for_filter("SKILL-X", None, None)
    assert len(calls) == 1


def test_callback_is_registered_on_the_real_app(real_ctx):
    entry = load_app_entry()
    assert "actions-table-body.children" in entry.app.callback_map
