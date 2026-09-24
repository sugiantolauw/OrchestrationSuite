"""Independent review 2026-09-24 item 3: a missing dollar figure renders as
"—", never a fabricated "$0" -- covers the shared helper and the two run/
action-row call sites that used to mask None with `or 0`."""

from __future__ import annotations

from src.platform.components import action_row, format_money_or_dash, run_card


def test_format_money_or_dash_none_is_a_dash():
    assert format_money_or_dash(None) == "—"


def test_format_money_or_dash_real_zero_is_dollar_zero():
    assert format_money_or_dash(0) == "$0"
    assert format_money_or_dash(0.0) == "$0"


def test_format_money_or_dash_real_value():
    assert format_money_or_dash(1234.5) == "$1,234" or format_money_or_dash(1234.5) == "$1,235"


def _run(potential_exposure):
    return {
        "run_id": "RUN-1", "skill_name": "T&E ExCo", "status": "Completed",
        "audit_period": "2025-01-01 – 2025-03-31", "run_owner": "alice",
        "run_timestamp": "2026-01-01T00:00:00Z", "findings_count": 1,
        "high_risk_count": 0, "open_actions": 0, "potential_exposure": potential_exposure,
    }


def test_run_card_shows_dash_for_missing_exposure_not_zero():
    text = str(run_card(_run(None)))
    assert "—" in text
    assert "$0" not in text


def test_run_card_shows_real_zero_exposure():
    text = str(run_card(_run(0.0)))
    assert "$0" in text


def test_run_card_appends_self_approved_text_when_self_approved():
    # CLAUDE.md §11 "Approval decisions" / independent review scenario 6b:
    # the self-approved label is required on /run/<id> AND /runs (run_card
    # is /runs' own row component). Backend already computes self_approved
    # in orchestrator/service.py list_runs -- this only covers the render.
    run = _run(0.0)
    run["self_approved"] = True
    text = str(run_card(run))
    assert "self-approved — segregation of duties not enforced" in text


def test_run_card_omits_self_approved_text_when_not_self_approved():
    run = _run(0.0)
    run["self_approved"] = False
    text = str(run_card(run))
    assert "self-approved" not in text.lower()


def _action(potential_exposure):
    return {
        "finding_title": "Missing receipts", "skill_name": "T&E ExCo", "risk": "Medium",
        "owner": None, "status": "Open", "target_date": None,
        "potential_exposure": potential_exposure, "evidence_link": "T4.1",
    }


def test_action_row_shows_dash_for_missing_exposure_not_zero():
    text = str(action_row(_action(None)))
    assert "—" in text
    assert "$0" not in text


def test_action_row_shows_real_zero_exposure():
    text = str(action_row(_action(0.0)))
    assert "$0" in text
