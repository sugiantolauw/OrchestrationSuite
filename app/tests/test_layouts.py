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


def test_management_actions_page_renders_none_exposure_without_crashing(monkeypatch):
    """A management action drawn from a non-monetary finding carries
    potential_exposure=None. `.get("potential_exposure", 0)` (the
    prototype's own formula, reference_app/app.py) does NOT substitute a
    default when the key is present with value None -- summing it directly
    raised TypeError. Reproduces the prototype's page exactly (same "Total
    exposure" KPI, same formula) while fixing that crash: a None
    contributes $0 to the sum, never raises."""
    import fake_service

    def _fake_list_management_actions(ctx, filters=None):
        return [{
            "action_id": "MA-1", "finding_id": "F1", "finding_title": "Non-monetary finding",
            "run_id": "RUN-1", "skill_id": "SKILL-001", "skill_name": "T&E ExCo",
            "risk": "Medium", "owner": None, "status": "Open", "target_date": None,
            "potential_exposure": None, "evidence_link": "T6.1a",
        }]

    monkeypatch.setattr(fake_service, "list_management_actions", _fake_list_management_actions)
    layout = management_actions_page()
    text = str(layout)
    assert "Total exposure" in text
    assert "$0" in text


def test_management_actions_page_exposure_is_not_double_counted(monkeypatch):
    """CLAUDE.md §0.3: reference_app/app.py summed financial_exposure across
    findings, and the same claim can be cited by more than one finding, so
    the total inflates. Two findings on one run, each citing an overlapping
    $8,000 of flagged spend (potential_exposure=8000 each -- a naive sum
    would show $16,000), must roll up to that run's own de-duplicated
    run_exposure_headline of $8,000, and a second completed run's headline
    ($3,000) adds once, not once per finding. A non-completed run's headline
    must not be counted at all."""
    import fake_service

    def _fake_list_management_actions(ctx, filters=None):
        return [
            {"action_id": "MA-1", "finding_id": "F1", "finding_title": "Split claims",
             "run_id": "RUN-1", "skill_id": "SKILL-001", "skill_name": "T&E ExCo",
             "risk": "High", "owner": None, "status": "Open", "target_date": None,
             "potential_exposure": 8000, "evidence_link": "T5.1"},
            {"action_id": "MA-2", "finding_id": "F2", "finding_title": "Duplicate claims",
             "run_id": "RUN-1", "skill_id": "SKILL-001", "skill_name": "T&E ExCo",
             "risk": "High", "owner": None, "status": "Open", "target_date": None,
             "potential_exposure": 8000, "evidence_link": "T5.2"},
            {"action_id": "MA-3", "finding_id": "F3", "finding_title": "Missing receipts",
             "run_id": "RUN-2", "skill_id": "SKILL-001", "skill_name": "T&E ExCo",
             "risk": "Medium", "owner": None, "status": "Open", "target_date": None,
             "potential_exposure": 3000, "evidence_link": "T4.1"},
        ]

    def _fake_list_audit_runs(ctx, filters=None):
        return [
            {"run_id": "RUN-1", "skill_id": "SKILL-001", "status": "Completed",
             "potential_exposure": 8000},
            {"run_id": "RUN-2", "skill_id": "SKILL-001", "status": "Completed",
             "potential_exposure": 3000},
            {"run_id": "RUN-3", "skill_id": "SKILL-001", "status": "Running",
             "potential_exposure": 5000},
        ]

    monkeypatch.setattr(fake_service, "list_management_actions", _fake_list_management_actions)
    monkeypatch.setattr(fake_service, "list_runs", _fake_list_audit_runs)
    layout = management_actions_page()
    text = str(layout)
    assert "Total exposure" in text
    assert "$11,000" in text
    assert "$16,000" not in text
    assert "$19,000" not in text


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


_SELF_APPROVED_TEXT = "(self-approved — segregation of duties not enforced)"


def test_workspace_tne_appends_self_approved_text_when_approver_is_run_owner():
    # When the sign-off actor is the same identity as run_owner, the small
    # appended text (not a badge/chip -- user decision 2026-09-23) must be
    # visible on the existing "Signed off by ..." sentence.
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess T&E spend.",
        run_owner="tester@example.com",
    )
    adapters.sign_off(run_id, "tester@example.com")
    layout = workspace_tne.tne_workspace_layout(run_id)
    assert _SELF_APPROVED_TEXT in str(layout)


def test_workspace_tne_omits_self_approved_text_when_approver_differs_from_owner():
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess T&E spend.",
        run_owner="owner@example.com",
    )
    adapters.sign_off(run_id, "reviewer@example.com")
    layout = workspace_tne.tne_workspace_layout(run_id)
    assert _SELF_APPROVED_TEXT not in str(layout)


def test_run_status_page_appends_self_approved_text_when_approver_is_run_owner():
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess T&E spend.",
        run_owner="tester@example.com",
    )
    adapters.sign_off(run_id, "tester@example.com")
    body = run_status._render_body(adapters.get_run(run_id), run_id)
    assert _SELF_APPROVED_TEXT in str(body)


def test_route_page_dispatches_every_known_path():
    app_module = load_app_entry()
    for pathname, search in [
        ("/", None), ("/skills", None), ("/skills/SKILL-001", None),
        ("/runs", None), ("/trace", None), ("/actions", None),
        ("/run/RUN-X", None), ("/workspace/tne", None),
    ]:
        assert app_module.route_page(pathname, search) is not None
