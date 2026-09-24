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
    exposure" KPI) while fixing that crash: no crash, and (independent
    review 2026-09-24 item 3) with no completed run to report the KPI reads
    "—", never a fabricated $0."""
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
    assert "—" in text
    assert "$0" not in text


def test_management_actions_page_exposure_is_not_double_counted(monkeypatch):
    """CLAUDE.md §0.3: reference_app/app.py summed financial_exposure across
    findings, and the same claim can be cited by more than one finding, so
    the total inflates. Two findings on one run, each citing an overlapping
    $8,000 of flagged spend (potential_exposure=8000 each -- a naive sum
    would show $16,000), must roll up to that run's own de-duplicated
    run_exposure_headline of $8,000, and a second completed run's headline
    ($3,000) adds once, not once per finding. A non-completed run's headline
    must not be counted at all. Independent review 2026-09-24 item 2: RUN-1
    and RUN-2 are distinct engagements/periods (never summed as if a re-run
    of the same one) -- see the separate re-run collapse coverage below."""
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
            {"run_id": "RUN-1", "skill_id": "SKILL-001", "engagement_id": "ENG-A",
             "audit_period": "2025-01-01 – 2025-03-31", "run_timestamp": "2026-01-01T00:00:00Z",
             "status": "Completed", "potential_exposure": 8000},
            {"run_id": "RUN-2", "skill_id": "SKILL-001", "engagement_id": "ENG-A",
             "audit_period": "2025-04-01 – 2025-06-30", "run_timestamp": "2026-04-01T00:00:00Z",
             "status": "Completed", "potential_exposure": 3000},
            {"run_id": "RUN-3", "skill_id": "SKILL-001", "engagement_id": "ENG-A",
             "audit_period": "2025-07-01 – 2025-09-30", "run_timestamp": "2026-07-01T00:00:00Z",
             "status": "Running", "potential_exposure": 5000},
        ]

    monkeypatch.setattr(fake_service, "list_management_actions", _fake_list_management_actions)
    monkeypatch.setattr(fake_service, "list_runs", _fake_list_audit_runs)
    layout = management_actions_page()
    text = str(layout)
    assert "Total exposure" in text
    assert "$11,000" in text
    assert "$16,000" not in text
    assert "$19,000" not in text


def test_management_actions_page_does_not_double_count_a_re_run(monkeypatch):
    """Independent review 2026-09-24 item 2: two COMPLETED runs of the same
    Skill over the same engagement and audit period (a re-run, e.g. after a
    correction) are the same underlying population re-tested -- only the
    latest one's headline counts, never both summed."""
    import fake_service

    def _fake_list_management_actions(ctx, filters=None):
        return []

    def _fake_list_audit_runs(ctx, filters=None):
        return [
            {"run_id": "RUN-OLD", "skill_id": "SKILL-001", "engagement_id": "ENG-A",
             "audit_period": "2025-01-01 – 2025-03-31", "run_timestamp": "2026-01-01T00:00:00Z",
             "status": "Completed", "potential_exposure": 9000},
            {"run_id": "RUN-NEW", "skill_id": "SKILL-001", "engagement_id": "ENG-A",
             "audit_period": "2025-01-01 – 2025-03-31", "run_timestamp": "2026-02-01T00:00:00Z",
             "status": "Completed", "potential_exposure": 4000},
        ]

    monkeypatch.setattr(fake_service, "list_management_actions", _fake_list_management_actions)
    monkeypatch.setattr(fake_service, "list_runs", _fake_list_audit_runs)
    layout = management_actions_page()
    text = str(layout)
    assert "$4,000" in text, "only the later re-run's own headline counts"
    assert "$9,000" not in text
    assert "$13,000" not in text


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


def test_workspace_tne_never_shows_self_approved_text_even_when_self_approved():
    # Independent review 2026-09-24 item 5: the self-approval text belongs
    # only where the prototype shows who signed off -- /run/<id> and /runs
    # -- never /workspace/tne, which had no sign-off concept in the
    # prototype at all and gained a whole "Run signoff" panel by mistake.
    # That panel (and its text) is removed here unconditionally, regardless
    # of self-approval.
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess T&E spend.",
        run_owner="tester@example.com",
    )
    adapters.sign_off(run_id, "tester@example.com")
    layout = workspace_tne.tne_workspace_layout(run_id)
    assert _SELF_APPROVED_TEXT not in str(layout)
    assert "Run signoff" not in str(layout)


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
