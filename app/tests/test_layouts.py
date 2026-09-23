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


def test_route_page_dispatches_every_known_path():
    app_module = load_app_entry()
    for pathname, search in [
        ("/", None), ("/skills", None), ("/skills/SKILL-001", None),
        ("/runs", None), ("/trace", None), ("/actions", None),
        ("/run/RUN-X", None), ("/workspace/tne", None),
    ]:
        assert app_module.route_page(pathname, search) is not None
