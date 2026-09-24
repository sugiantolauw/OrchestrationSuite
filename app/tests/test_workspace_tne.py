from __future__ import annotations

import fake_service
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


def test_exposure_summary_says_not_available_when_all_none():
    findings = [{"exposure_amount": None}, {"exposure_amount": None}]
    assert "Not available" in workspace_tne._exposure_summary(findings)


def test_exposure_summary_never_sums_per_finding_amounts_without_a_headline():
    """B4 (CLAUDE.md NN14, P2/P3 gate review): with no run-level headline in
    the payload, this must NOT fall back to summing each finding's own
    exposure_amount -- that double-counts a row cited by more than one
    finding, the exact bug the headline exists to prevent (CLAUDE.md §0.3).
    It must say the headline is not available, never show a fabricated sum."""
    findings = [{"exposure_amount": 100.0}, {"exposure_amount": None}]
    summary = workspace_tne._exposure_summary(findings)
    assert "$100" not in summary
    assert "Not available" in summary


def test_exposure_summary_prefers_the_run_headline_over_summing_findings():
    payload = {"exposure": {"headline": 1725.0, "basis": "de-duplicated"}}
    findings = [{"exposure_amount": 999999.0}]
    summary = workspace_tne._exposure_summary(findings, payload)
    assert "$1,725" in summary
    assert "de-duplicated" in summary


def test_exposure_summary_prefers_label_over_basis():
    # B2 (CLAUDE.md P2/P3 gate review): `label` is the short display name;
    # `basis` is the full methodology paragraph, shown separately -- when
    # both are present the short label wins the hero line.
    payload = {"exposure": {
        "headline": 1725.0, "label": "Potential exposure",
        "basis": "a very long methodology paragraph " * 5,
    }}
    summary = workspace_tne._exposure_summary([{"exposure_amount": 1.0}], payload)
    assert "Potential exposure" in summary
    assert "methodology paragraph" not in summary


def test_latest_completed_run_id_finds_the_run():
    run_id = _completed_run()
    assert workspace_tne.latest_completed_run_id() == run_id


def test_workspace_layout_with_no_run_id_shows_empty_state():
    layout = workspace_tne.tne_workspace_layout(None)
    assert "No completed run yet" in str(layout)


def test_workspace_layout_shows_error_panel_when_payload_load_fails(monkeypatch):
    """CLAUDE.md NN14, P2/P3 gate review item 7: a get_run_payload failure
    (e.g. orchestrator.frames.FrameSnapshotIntegrityError -- a sha256
    mismatch on a run's persisted row snapshot) must render an explicit
    error panel naming the exception, never a silent empty-looking page."""
    run_id = _completed_run()

    class _FrameSnapshotIntegrityError(Exception):
        pass

    def _raise(ctx, rid):
        raise _FrameSnapshotIntegrityError("sha256 mismatch for source expense_report")

    monkeypatch.setattr(fake_service, "get_run_payload", _raise)
    layout = workspace_tne.tne_workspace_layout(run_id)
    text = str(layout)
    assert "could not be loaded" in text
    assert "_FrameSnapshotIntegrityError" in text
    assert "sha256 mismatch" in text


def test_workspace_layout_shows_error_panel_when_actions_load_fails(monkeypatch):
    """Independent review 2026-09-24 item 7: list_management_actions'
    failure used to be swallowed into an empty action list with no
    load_error recorded -- unlike the payload/frames loads right above it
    in _load_bundle, which already surface an explicit error panel. A
    genuine actions-load failure (e.g. a Delta connectivity error) must not
    render as "this run drafted zero management actions"."""
    run_id = _completed_run()

    class _ActionsLoadError(Exception):
        pass

    def _raise(ctx, filters=None):
        raise _ActionsLoadError("connection reset reading management_actions")

    monkeypatch.setattr(fake_service, "list_management_actions", _raise)
    layout = workspace_tne.tne_workspace_layout(run_id)
    text = str(layout)
    assert "could not be loaded" in text
    assert "_ActionsLoadError" in text
    assert "connection reset" in text


def test_finding_card_shows_exposure_not_yet_computed():
    finding = {
        "severity": "High", "test_id": "T4.1", "title": "Missing receipts",
        "observation": "obs", "recommendation": "rec", "management_questions": ["q?"],
        "analyst_set_severity": True, "severity_basis": "threshold", "exposure_amount": None,
    }
    card = workspace_tne._finding_card(0, finding)
    text = str(card)
    assert "not yet computed" in text


def test_finding_card_raises_if_analyst_set_severity_was_never_persisted():
    """CLAUDE.md §0.4/G8, P2/P3 gate review item 3: never render an
    unattributed severity as if it were policy-backed."""
    import pytest

    finding = {
        "severity": "High", "test_id": "T4.1", "title": "Missing receipts",
        "observation": "obs", "recommendation": "rec", "management_questions": ["q?"],
        "exposure_amount": None,
    }
    with pytest.raises(ValueError, match="analyst_set_severity"):
        workspace_tne._finding_card(0, finding)


def test_render_filtered_findings_shows_top_three_and_collapses_rest():
    findings = [{"severity": "High", "title": f"F{i}", "observation": "", "recommendation": "",
                 "management_questions": [], "analyst_set_severity": False} for i in range(5)]
    children = workspace_tne._render_filtered_findings(findings, [], [], "authored")
    assert "Showing the top 3" in str(children[0])


def test_render_filtered_findings_applies_category_filter():
    tests = [{"test_id": "T4.1", "category": "Spend compliance"}, {"test_id": "T5.1", "category": "Fraud risk"}]
    findings = [
        {"severity": "High", "test_id": "T4.1", "title": "Missing Receipts Finding", "observation": "", "recommendation": "", "management_questions": [], "analyst_set_severity": False},
        {"severity": "High", "test_id": "T5.1", "title": "Split Claims Finding", "observation": "", "recommendation": "", "management_questions": [], "analyst_set_severity": True},
    ]
    children = workspace_tne._render_filtered_findings(findings, [], ["Fraud risk"], "authored", tests)
    text = str(children)
    assert "Split Claims Finding" in text
    assert "Missing Receipts Finding" not in text


def test_workspace_layout_builds_all_tabs_on_a_completed_run():
    run_id = _completed_run()
    layout = workspace_tne.tne_workspace_layout(run_id)
    text = str(layout)
    # Ids match the prototype exactly, no "tne-" prefix (CLAUDE.md item E
    # layout-parity fix): reference_app/app.py's tne_workspace_layout() sets
    # these same tab_id values (main-tabs/tab-executive/.../sub-catalogue).
    for tab_id in ["tab-executive", "tab-findings", "tab-audit",
                    "sub-findings", "sub-actions",
                    "sub-overview", "sub-risk", "sub-approval", "sub-catalogue"]:
        assert tab_id in text


def test_p1_layout_and_update_build_charts_from_real_frames():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    layout = workspace_tne._p1_layout(bundle)
    assert "p1-monthly" in str(layout)
    meta = workspace_tne._skill_flag_meta(bundle["run"].get("skill_id"), bundle["skill"].get("tests", []))
    result = workspace_tne._p1_update(bundle, None, None, None, meta)
    assert len(result) == 10
    kpis = result[0]
    assert len(kpis) == 4


def test_p2_spend_by_employee_is_coloured_by_role():
    # orchestrator/frames.py `frame_tags` (CLAUDE.md build brief P4 perf fix)
    # -- restores reference_app/app.py's Source_Population colouring on
    # 'Spend by Employee' now that a real per-row P_EXP role is available.
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    tests = bundle["skill"].get("tests", [])
    meta = workspace_tne._skill_flag_meta(bundle["run"].get("skill_id"), tests)
    result = workspace_tne._p2_update(bundle, None, None, None, None, meta)
    fig = result[1]
    trace_names = {trace.name for trace in fig.data}
    assert trace_names and trace_names <= {"prepared", "approved", "both"}
    assert len(fig.data) > 1  # more than one role present in the fake frame


def test_p3_update_derives_receipt_status_from_real_columns():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    result = workspace_tne._p3_update(bundle, None, None, None)
    assert len(result) == 8
    kpis = result[0]
    assert len(kpis) == 4


def test_exception_drilldown_finds_flags_across_frames():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    tests = bundle["skill"].get("tests", [])
    meta = workspace_tne._skill_flag_meta(bundle["run"].get("skill_id"), tests)
    flags = workspace_tne._flags_for_test(meta, "T4.1")
    assert "RF_CS_MissingReceipt" in flags
    source, df = workspace_tne._frame_for_flags(bundle["frames"], flags)
    assert source == "expense_report"
    assert df is not None


def test_actions_tab_renders_real_persisted_actions():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    body = workspace_tne._actions_tab(bundle)
    text = str(body)
    assert "session-only" in text
    assert "Missing Receipt Documentation" in text


def test_namesake_disclosure_base_text_with_no_metric():
    text = workspace_tne._namesake_disclosure_text({"metrics": {}})
    assert "50040" in text
    assert "52472" in text
    assert "namesake" in text
    assert "This run's data attributes" not in text  # no live figure to append


def test_namesake_disclosure_includes_the_live_run_figure():
    payload = {
        "metrics": {
            "exco_namesake_excluded_claims_rows": {"value": 27, "unit": "count"},
            "exco_namesake_excluded_claims_amount": {"value": 4087.97, "unit": "AUD"},
        }
    }
    text = workspace_tne._namesake_disclosure_text(payload)
    assert "50040" in text
    assert "27 claim" in text
    assert "$4,088" in text


def test_methodology_panel_includes_namesake_disclosure():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    panel = workspace_tne._methodology_panel(bundle["payload"])
    text = str(panel)
    assert "50040" in text
    assert "namesake" in text


def test_catalogue_tab_shows_reconciliation_panel():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    tests = bundle["skill"].get("tests", [])
    body = workspace_tne._catalogue_tab(tests, bundle["run"].get("test_results", []), bundle["payload"])
    text = str(body)
    assert "Population reconciliation" in text
    assert "expense_report" in text
    assert "Claims population" in text
    assert "Prepared: 5 rows" in text


def test_header_shows_run_level_metrics():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    header = workspace_tne._build_header(bundle["run"], bundle["payload"])
    text = str(header)
    assert "12 records" in text
    assert "2 source files" in text
    assert "5 months covered" in text


def test_flag_to_test_from_real_get_skill_shape_used_directly():
    run_id = _completed_run()
    bundle = workspace_tne._load_bundle(run_id)
    tests = bundle["skill"].get("tests", [])
    meta = workspace_tne._skill_flag_meta(bundle["run"].get("skill_id"), tests)
    assert meta["RF_CS_MissingReceipt"]["test_id"] == "T4.1"
    assert meta["RF_CS_MissingReceipt"]["plan_test_ids"] == ["T4.1"]


def test_flags_for_test_handles_a_flag_shared_by_two_plan_sub_tests():
    # T6.1d_dom and T6.1d_int both set flag: RF_CS_DailySpendOverLimit in the
    # real skills/tne_exco/plan.yaml -- a flat {flag: plan_test_id} dict can
    # only keep one of the two; plan_tests (list form) must keep both.
    catalogue_tests = [{
        "test_id": "T6.1d", "category": "Approval effectiveness", "test_name": "Daily spend limit",
        "plan_tests": [
            {"test_id": "T6.1d_dom", "flag": "RF_CS_DailySpendOverLimit", "primitive": "threshold_exceedance"},
            {"test_id": "T6.1d_int", "flag": "RF_CS_DailySpendOverLimit", "primitive": "threshold_exceedance"},
        ],
    }]
    meta = workspace_tne._skill_flag_meta("SKILL-TEST-COLLISION", catalogue_tests)
    assert set(meta["RF_CS_DailySpendOverLimit"]["plan_test_ids"]) == {"T6.1d_dom", "T6.1d_int"}
    assert workspace_tne._flags_for_test(meta, "T6.1d_dom") == {"RF_CS_DailySpendOverLimit"}
    assert workspace_tne._flags_for_test(meta, "T6.1d_int") == {"RF_CS_DailySpendOverLimit"}
    assert workspace_tne._flags_for_test(meta, "T6.1d") == {"RF_CS_DailySpendOverLimit"}


def test_workspace_layout_degrades_gracefully_on_a_run_with_no_frames(monkeypatch):
    run_id = _completed_run()

    import fake_service

    def _empty_frames(ctx, run_id):
        return {}

    monkeypatch.setattr(fake_service, "get_run_frames", _empty_frames)
    workspace_tne._CACHE.clear()
    layout = workspace_tne.tne_workspace_layout(run_id)
    assert "tab-executive" in str(layout)


def test_skill_flag_meta_prefers_the_runs_pinned_skill_version_content(monkeypatch):
    """Independent review 2026-09-24 item 7: when a skill_version is given,
    its immutable skill_versions content wins over catalogue_tests/live-disk
    -- proven here by making the pinned content disagree with catalogue_tests
    and asserting the pinned one is what comes back."""
    workspace_tne._SKILL_FLAG_CACHE.clear()

    def _fake_get_skill_version_plan(skill_id, version):
        assert (skill_id, version) == ("SKILL-PIN-TEST", "v7")
        return {"tests": [{"test_id": "T9.9", "flag": "RF_PINNED_ONLY"}]}

    monkeypatch.setattr(adapters, "get_skill_version_plan", _fake_get_skill_version_plan)

    # catalogue_tests names a DIFFERENT flag -- if this were used instead of
    # the pinned content, RF_PINNED_ONLY would never appear.
    catalogue_tests = [{
        "test_id": "T1.1", "category": "x", "test_name": "y",
        "plan_tests": [{"test_id": "T1.1", "flag": "RF_FROM_CATALOGUE", "primitive": "threshold_exceedance"}],
    }]
    meta = workspace_tne._skill_flag_meta("SKILL-PIN-TEST", catalogue_tests, "v7")
    assert "RF_PINNED_ONLY" in meta
    assert "RF_FROM_CATALOGUE" not in meta


def test_skill_flag_meta_falls_back_when_no_version_is_pinned(monkeypatch):
    """get_skill_version_plan returning None (no skill_versions row for this
    version -- a real, expected case, e.g. a run older than the skill
    registry) is a legitimate fallback to catalogue_tests, never an error."""
    workspace_tne._SKILL_FLAG_CACHE.clear()
    monkeypatch.setattr(adapters, "get_skill_version_plan", lambda skill_id, version: None)

    catalogue_tests = [{
        "test_id": "T1.1", "category": "x", "test_name": "y",
        "plan_tests": [{"test_id": "T1.1", "flag": "RF_FROM_CATALOGUE", "primitive": "threshold_exceedance"}],
    }]
    meta = workspace_tne._skill_flag_meta("SKILL-FALLBACK-TEST", catalogue_tests, "v-unregistered")
    assert "RF_FROM_CATALOGUE" in meta


def test_skill_flag_meta_never_swallows_a_genuine_load_failure(monkeypatch):
    """Independent review 2026-09-24 item 7 -- THE REGRESSION THIS FIX
    TARGETS: a broken skills/<id>/plan.yaml on the live-disk fallback path
    (no skill_version, no catalogue_tests, no flag_to_test) used to be
    swallowed by a bare `except Exception: pass` and silently cached as an
    empty {} -- indistinguishable from "this Skill genuinely has no flags".
    Must raise instead."""
    import pytest as _pytest

    workspace_tne._SKILL_FLAG_CACHE.clear()
    monkeypatch.setattr(adapters, "get_skill", lambda skill_id: {})  # no flag_to_test

    def _broken_load_skill(skill_dir):
        raise ValueError("plan.yaml: schema violation")

    monkeypatch.setattr(workspace_tne, "_skill_dir", lambda skill_id: "/some/fake/dir")
    import orchestrator.skills as orch_skills
    monkeypatch.setattr(orch_skills, "load_skill", _broken_load_skill)

    with _pytest.raises(ValueError, match="schema violation"):
        workspace_tne._skill_flag_meta("SKILL-BROKEN-TEST", [], None)
