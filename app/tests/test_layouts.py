"""Every route's layout builds without raising, against the fake backend."""

from __future__ import annotations

from conftest import load_app_entry
from src import run_setup, run_status, workspace_tne
from src.platform import adapters
from src.platform.pages import (
    audit_runs_page,
    management_actions_page,
    platform_trace_page,
    skill_library_page,
    skill_methodology_page,
)


def test_home_layout_builds():
    layout = run_setup.home_layout()
    assert layout is not None


def test_skill_library_page_builds():
    assert skill_library_page() is not None


def test_skill_methodology_page_builds():
    assert skill_methodology_page("SKILL-001") is not None


def test_skill_methodology_page_reflects_the_real_skill_not_a_stale_copy(monkeypatch, tmp_path):
    """CLAUDE.md §3 NN16, P2/P3 gate review item 4: the methodology page used
    to render app/src/test_catalogue.py's own hardcoded version ("1.2"),
    status ("Published") and a stale "$1,000 per employee per day" threshold
    text, independent of the real Skill under skills/tne_exco/. Renders
    against the REAL orchestrator.service (overriding this file's autouse
    fake_backend fixture for this one test) so drift in the real wiring is
    actually caught here, not just in the fake."""
    from pathlib import Path

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

    page = skill_methodology_page("SKILL-001")
    text = str(page)

    manifest = __import__("yaml").safe_load((repo_root / "skills" / "tne_exco" / "manifest.yaml").read_text())
    assert manifest["version"] in text
    assert "1.2" not in text or manifest["version"] == "1.2"
    assert "$1,000" not in text
    assert "Published" not in text  # real status is "draft" -> "Draft"


def test_audit_runs_page_builds_with_no_runs():
    assert audit_runs_page() is not None


def test_trace_page_builds():
    assert platform_trace_page() is not None


def test_management_actions_page_builds():
    assert management_actions_page() is not None


def test_run_page_builds_for_unknown_run():
    layout = run_status.run_page("RUN-DOES-NOT-EXIST")
    assert layout is not None


def test_workspace_tne_empty_state_when_no_completed_run():
    layout = workspace_tne.tne_workspace_layout(None)
    assert layout is not None


def test_workspace_tne_renders_a_completed_run():
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess T&E spend.",
        run_owner="tester@example.com",
    )
    adapters.sign_off(run_id, "tester@example.com")
    layout = workspace_tne.tne_workspace_layout(run_id)
    assert layout is not None


def test_workspace_tne_shows_self_approved_badge_when_approver_is_run_owner():
    # CLAUDE.md §11 "allow self sign-off for now": when the sign-off actor is
    # the same identity as run_owner (as above, and in this fixture's normal
    # single-actor flow), the workspace must visibly label it, not just
    # silently accept it.
    from orchestrator.signoff_policy import SELF_APPROVED_LABEL

    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess T&E spend.",
        run_owner="tester@example.com",
    )
    adapters.sign_off(run_id, "tester@example.com")
    layout = workspace_tne.tne_workspace_layout(run_id)
    assert SELF_APPROVED_LABEL in str(layout)


def test_workspace_tne_omits_self_approved_badge_when_approver_differs_from_owner():
    from orchestrator.signoff_policy import SELF_APPROVED_LABEL

    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess T&E spend.",
        run_owner="owner@example.com",
    )
    adapters.sign_off(run_id, "reviewer@example.com")
    layout = workspace_tne.tne_workspace_layout(run_id)
    assert SELF_APPROVED_LABEL not in str(layout)


def test_runs_list_page_shows_self_approved_badge_for_a_self_signed_run():
    from orchestrator.signoff_policy import SELF_APPROVED_LABEL

    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess T&E spend.",
        run_owner="tester@example.com",
    )
    adapters.sign_off(run_id, "tester@example.com")
    page = audit_runs_page()
    assert SELF_APPROVED_LABEL in str(page)


def test_route_page_dispatches_every_known_path():
    app_module = load_app_entry()
    for pathname, search in [
        ("/", None), ("/skills", None), ("/skills/SKILL-001", None),
        ("/runs", None), ("/trace", None), ("/actions", None),
        ("/run/RUN-X", None), ("/workspace/tne", None),
    ]:
        assert app_module.route_page(pathname, search) is not None
