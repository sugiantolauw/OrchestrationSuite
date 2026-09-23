from __future__ import annotations

import pytest

from src import run_setup
from src.platform import adapters


def test_validate_form_rejects_missing_everything():
    errors = run_setup.validate_form(None, {}, None, None, None)
    assert any("Select a Skill" in e for e in errors)
    assert any("audit period" in e for e in errors)
    assert any("objective" in e for e in errors)


def test_validate_form_rejects_unbound_source():
    errors = run_setup.validate_form(
        "SKILL-001", {"expense_report": None}, "2025-01-01", "2026-04-30", "Assess spend.",
    )
    assert any("expense_report" in e for e in errors)


def test_validate_form_passes_when_complete():
    errors = run_setup.validate_form(
        "SKILL-001",
        {"expense_report": "test_catalog.tne_source.expense_report"},
        "2025-01-01", "2026-04-30", "Assess spend.",
    )
    assert errors == []


def test_binding_row_prefills_only_exact_non_restricted_matches():
    tables = adapters.list_governed_tables()
    suggested = {"expense_report": "test_catalog.tne_source.expense_report",
                 "attendee_validity": "test_catalog.restricted.secret_table"}
    row = run_setup._binding_row("expense_report", ["Employee"], tables, suggested)
    dropdown = row.children[1]
    assert dropdown.value == "test_catalog.tne_source.expense_report"

    restricted_row = run_setup._binding_row("attendee_validity", [], tables, suggested)
    restricted_dropdown = restricted_row.children[1]
    # the suggestion points at a restricted table, so it must not be pre-filled
    assert restricted_dropdown.value is None
    restricted_options = {o["value"]: o for o in restricted_dropdown.options}
    assert restricted_options["test_catalog.restricted.secret_table"]["disabled"] is True


def test_start_audit_run_creates_an_awaiting_signoff_run():
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess spend.",
        run_owner="auditor@example.com",
    )
    run = adapters.get_run(run_id)
    assert run["status"] == "awaiting_signoff"
    assert run["run_owner"] == "auditor@example.com"


def test_start_audit_run_review_plan_first_awaits_confirmation():
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess spend.",
        run_owner="auditor@example.com",
        review_plan_first=True,
    )
    run = adapters.get_run(run_id)
    assert run["status"] == "awaiting_confirmation"
