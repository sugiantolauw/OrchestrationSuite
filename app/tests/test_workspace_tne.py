from __future__ import annotations

from src import workspace_tne
from src.platform import adapters


def _completed_run():
    run_id = adapters.start_audit_run(
        skill_id="SKILL-001",
        bindings={"expense_report": "test_catalog.tne_source.expense_report"},
        audit_period=("2025-01-01", "2026-04-30"),
        objective="Assess spend.",
        run_owner="auditor@example.com",
    )
    adapters.sign_off(run_id, "auditor@example.com")
    return run_id


def test_exposure_summary_says_not_yet_computed_when_all_none():
    findings = [{"exposure_amount": None}, {"exposure_amount": None}]
    assert "Not yet computed" in workspace_tne._exposure_summary(findings)


def test_exposure_summary_sums_known_values_and_never_fabricates():
    findings = [{"exposure_amount": 100.0}, {"exposure_amount": None}]
    summary = workspace_tne._exposure_summary(findings)
    assert "$100" in summary
    assert "1 finding(s) still pending" in summary


def test_latest_completed_run_id_finds_the_run():
    run_id = _completed_run()
    assert workspace_tne.latest_completed_run_id() == run_id


def test_workspace_layout_with_no_run_id_shows_empty_state():
    layout = workspace_tne.tne_workspace_layout(None)
    assert "No completed run yet" in str(layout)


def test_finding_card_flags_analyst_set_threshold():
    finding = {
        "severity": "High", "test_id": "T4.1", "title": "Missing receipts",
        "observation": "obs", "recommendation": "rec", "management_questions": ["q?"],
        "analyst_set_severity": True, "exposure_amount": None,
    }
    card = workspace_tne._finding_card(0, finding)
    text = str(card)
    assert "Analyst-set threshold" in text
    assert "not yet computed" in text


def test_render_filtered_findings_shows_top_three_and_collapses_rest():
    findings = [{"severity": "High", "title": f"F{i}", "observation": "", "recommendation": "",
                 "management_questions": []} for i in range(5)]
    children = workspace_tne._render_filtered_findings(findings, [], "authored")
    assert "Showing the top 3" in str(children[0])


def test_detail_tab_degrades_gracefully_without_a_frame():
    body = workspace_tne._detail_tab({})
    assert "No row-level population frame" in str(body)


def test_detail_tab_builds_charts_from_a_real_frame():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    body = workspace_tne._detail_tab(bundle["frames"])
    assert "p1-monthly" in str(body)
