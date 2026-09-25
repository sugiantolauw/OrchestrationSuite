from __future__ import annotations

import fake_service
from src import workspace_tne
from src.platform import adapters


def _completed_run(skill_id="SKILL-001"):
    run_id = adapters.start_audit_run(
        skill_id=skill_id,
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


def test_latest_completed_run_id_ignores_a_completed_explorer_run():
    # CLAUDE.md §6 D5: "/workspace/tne stays SKILL-001's" -- this page's
    # every chart assumes tne_exco's own contract columns (this module's
    # own docstring), so a completed Explorer (or any non-SKILL-001) run
    # must never become the "latest completed run" this page renders. The
    # Explorer run is created FIRST and the SKILL-001 run second: fake_
    # service stamps every run with the same last_updated, so an unfiltered
    # pick (the bug) would return whichever run sorts first for ties --
    # here that's the Explorer run inserted first -- while the fix must
    # still return the SKILL-001 run regardless of insertion order.
    _completed_run(skill_id="SKILL-EXPLORER-1")
    tne_run_id = _completed_run(skill_id="SKILL-001")
    assert workspace_tne.latest_completed_run_id() == tne_run_id


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


# ── UI-5/UI-6 (docs/specs/P6_narration_design.md §7): the accepted-AI-
# proposed chip and the NN12 sources tooltip on /workspace/tne's finding
# card -- both additive, no other visible change (CLAUDE.md §11 "the UI is
# the prototype's, exactly"). ────────────────────────────────────────────


def _base_finding(**overrides) -> dict:
    finding = {
        "severity": "High", "test_id": "T4.1", "title": "Missing receipts",
        "observation": "obs", "recommendation": "rec", "management_questions": ["q?"],
        "analyst_set_severity": True, "severity_basis": "threshold", "exposure_amount": None,
        "finding_id": "F1",
    }
    finding.update(overrides)
    return finding


def test_finding_card_shows_no_ai_chip_for_a_rule_finding():
    card = workspace_tne._finding_card(0, _base_finding())
    text = str(card)
    assert "AI-proposed" not in text


def test_finding_card_shows_accepted_chip_for_an_ai_proposed_finding():
    finding = _base_finding(origin="ai_proposed", accepted_by="reviewer@example.com")
    card = workspace_tne._finding_card(0, finding)
    text = str(card)
    assert "AI-proposed, accepted by reviewer@example.com" in text


def test_finding_card_observation_has_no_tooltip_when_no_metrics_cited():
    card = workspace_tne._finding_card(0, _base_finding(metrics_cited={}))
    # No `title=` kwarg reaches the rendered P at all (UI-6: "no visible
    # change" only makes sense if absent metrics means no attribute, never
    # an empty `title=""` that would still show a blank tooltip on hover).
    assert "title=" not in str(card)


def test_finding_card_observation_tooltip_names_the_findings_metrics_cited_source_fields():
    finding = _base_finding(metrics_cited={"missing_receipt_count": {"value": 3, "unit": "count"}})
    card = workspace_tne._finding_card(0, finding)
    text = str(card)
    assert "findings[F1].metrics_cited.missing_receipt_count" in text
    # UI-6 must not change the visible observation text itself.
    assert "obs" in text


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
    # Independent review 2026-09-24 gap #3: this claim is only true once
    # owner/status/target-date/response edits actually persist
    # (orchestrator.service.update_management_action) -- "session-only" was
    # a true statement about the OLD dcc.Store-only behaviour; asserting it
    # here would pin the bug this fix removes.
    assert "session-only" not in text
    assert "Action ownership and responses are saved with this run" in text
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


# ── Catalogue-grain "tests with exceptions" counting (independent review
# 2026-09-24 gap #5) ─────────────────────────────────────────────────────
#
# One catalogue test ("T-A") can resolve to several plan.yaml sub-tests
# ("T-A_x1", "T-A_x2") -- counting test_results directly (sub-test grain)
# instead of the 14-entry catalogue over/under-counts. Fixture below has 3
# catalogue tests behind 5 plan sub-tests: T-A is 2 sub-tests, BOTH
# "exception" (must combine to ONE catalogue exception, not two); T-B is a
# single passing sub-test; T-C is 2 sub-tests, BOTH not_testable (must
# report not_testable, never as a clean pass).

_CATALOGUE_TESTS = [
    {"test_id": "T-A", "category": "Cat", "test_name": "Test A", "threshold": "0",
     "control_objective": "obj", "population": "pop", "rule": "rule"},
    {"test_id": "T-B", "category": "Cat", "test_name": "Test B", "threshold": "0",
     "control_objective": "obj", "population": "pop", "rule": "rule"},
    {"test_id": "T-C", "category": "Cat", "test_name": "Test C", "threshold": "0",
     "control_objective": "obj", "population": "pop", "rule": "rule"},
]

_SUBTEST_RESULTS = [
    {"test_id": "T-A_x1", "status": "exception", "exception_units": 3},
    {"test_id": "T-A_x2", "status": "exception", "exception_units": 4},
    {"test_id": "T-B", "status": "pass", "exception_units": 0},
    {"test_id": "T-C_x1", "status": "not_testable", "reason": "no endpoint"},
    {"test_id": "T-C_x2", "status": "not_testable", "reason": "no endpoint"},
]


def _kpi_values(component) -> dict:
    """Walks a Dash component tree and collects every kpi_card's
    {label: value} -- avoids brittle substring matching when a KPI's number
    might coincide with something else rendered on the same page."""
    values: dict = {}

    def walk(node):
        children = getattr(node, "children", None)
        if getattr(node, "className", None) == "kpi-tile" and isinstance(children, list) and len(children) >= 2:
            values[children[0].children] = children[1].children
        if isinstance(children, list):
            for c in children:
                walk(c)
        elif children is not None:
            walk(children)

    walk(component)
    return values


def test_catalogue_test_statuses_combines_subtests_under_one_catalogue_id():
    statuses = workspace_tne._catalogue_test_statuses(_CATALOGUE_TESTS, _SUBTEST_RESULTS)
    assert statuses["T-A"]["status"] == "exception"
    assert statuses["T-A"]["exception_units"] == 7  # 3 + 4, summed across T-A's own sub-tests only
    assert statuses["T-B"]["status"] == "pass"
    assert statuses["T-C"]["status"] == "not_testable", "never reported as a clean pass"


def test_catalogue_tab_kpis_count_catalogue_tests_not_plan_subtests():
    body = workspace_tne._catalogue_tab(_CATALOGUE_TESTS, _SUBTEST_RESULTS, None)
    kpis = _kpi_values(body)
    assert kpis["Tests Executed"] == "3", "3 catalogue tests, not 5 plan.yaml sub-tests"
    assert kpis["Exceptions"] == "1", "T-A's two exception sub-tests are one catalogue exception"
    assert kpis["Pass"] == "1"
    assert kpis["Not Testable"] == "1"


def test_executive_tab_tests_with_exceptions_counts_catalogue_tests_not_plan_subtests():
    run = {"test_results": _SUBTEST_RESULTS}
    body = workspace_tne._executive_tab(run, findings=[], payload=None, actions=[], tests=_CATALOGUE_TESTS)
    kpis = _kpi_values(body)
    assert kpis["Tests with exceptions"] == "1", "one catalogue test (T-A), not two exception sub-test rows"


def test_executive_tab_falls_back_to_subtest_grain_when_no_catalogue_rows_given():
    # No `tests` -- the KPI still renders rather than raising, using the
    # old (wrong-grain) count as a fallback rather than crashing a caller
    # that genuinely has no catalogue rows to hand in.
    run = {"test_results": _SUBTEST_RESULTS}
    body = workspace_tne._executive_tab(run, findings=[], payload=None, actions=[])
    kpis = _kpi_values(body)
    assert kpis["Tests with exceptions"] == "2"


# ── Management action edits persist for real (independent review 2026-09-24
# gap #3, requirement #4) ────────────────────────────────────────────────

def _write_tiny_mini_data(root):
    import pandas as pd

    pd.DataFrame([
        {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Vendor": "VendorA"},
        {"Employee ID": 1, "Transaction Date": "2026-01-06", "Amount": 600, "Vendor": "VendorA"},
        {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 700, "Vendor": "VendorB"},
        {"Employee ID": 3, "Transaction Date": "2026-01-15", "Amount": 50, "Vendor": "VendorC"},
        {"Employee ID": 4, "Transaction Date": "2026-02-01", "Amount": 900, "Vendor": "VendorD"},
    ]).to_csv(root / "claims.csv", index=False)
    pd.DataFrame([
        {"Employee ID": 1, "Transaction Date": "2026-01-05", "Vendor": "VendorA"},
        {"Employee ID": 2, "Transaction Date": "2026-01-10", "Vendor": "VendorB"},
        {"Employee ID": 4, "Transaction Date": "2026-02-01", "Vendor": "VendorD"},
    ]).to_csv(root / "register.csv", index=False)


def test_management_action_edit_survives_a_fresh_bundle_load_against_real_local_persistence(monkeypatch, tmp_path):
    """Independent review 2026-09-24 gap #3, requirement #4: an edit made
    through adapters.update_management_action (the same call _save_action
    makes) must be readable back from a completely fresh _load_bundle() call
    -- what a genuine page reload makes -- against the REAL LocalPersistence
    backend, never fake_service.py. Also exercises the one thing that could
    silently defeat this: _load_bundle's own per-run cache, which does not
    key on anything a management-action edit changes, so it must be
    invalidated by the save path (workspace_tne._save_action) rather than
    serving a snapshot taken before the edit."""
    import time
    from pathlib import Path as _Path

    from orchestrator import service as real_service

    repo_root = _Path(__file__).resolve().parents[2]
    mini_skill_dir = repo_root / "tests" / "fixtures" / "skills" / "mini"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_tiny_mini_data(data_dir)

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(mini_skill_dir.parent),
        "CODE_REVISION": "test-fixed-revision",
    }
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))
    workspace_tne._CACHE.clear()

    run_ctx = adapters.get_context()
    run_ctx.executor.start()
    try:
        bindings = real_service.suggest_bindings(run_ctx, "SKILL-MINI")
        run_id = adapters.start_audit_run(
            skill_id="SKILL-MINI", bindings=bindings, audit_period=("2026-01-01", "2026-02-28"),
            objective="reload test", run_owner="tester@example.com",
        )

        def _wait(statuses, timeout=15):
            deadline = time.time() + timeout
            while time.time() < deadline:
                status = adapters.get_run(run_id)["status"]
                if status in statuses:
                    return status
                time.sleep(0.05)
            raise AssertionError(f"run {run_id} did not reach {statuses} in time")

        _wait({"awaiting_signoff", "failed"})
        adapters.sign_off(run_id, "approver@example.com")
        status = _wait({"completed", "failed"})
        assert status == "completed", adapters.get_run(run_id).get("status_reason")

        # Simulates the FIRST page view -- also primes _CACHE, which is the
        # exact staleness risk a save must defeat.
        first_bundle = workspace_tne._load_bundle(run_id)
        actions = first_bundle["actions"]
        assert actions, "the mini Skill's findings should have drafted management actions"
        action_id = actions[0]["action_id"]
        assert actions[0].get("owner") in (None, "")

        adapters.update_management_action(
            action_id, owner="Alex Chen", status="agreed", target_date="2026-03-01",
            response="Agreed with management.", actor="reviewer@example.com",
        )
        # What workspace_tne._save_action itself does immediately after this
        # same persistence call -- proven necessary by the assertion below:
        # without it, _load_bundle would still serve the bundle cached above.
        workspace_tne._CACHE.clear()

        # A genuinely fresh page load -- new call, no session dcc.Store data
        # carried over. _load_bundle must not still be serving the bundle
        # cached above.
        fresh_bundle = workspace_tne._load_bundle(run_id)
        fresh_action = next(a for a in fresh_bundle["actions"] if a["action_id"] == action_id)
        assert fresh_action["owner"] == "Alex Chen"
        assert fresh_action["status"] == "Agreed"
        assert fresh_action["target_date"] == "2026-03-01"
        assert fresh_action["description"] == "Agreed with management."

        # And the rendered tab's table shows the edit too, exactly as a real
        # reload would (the response text itself only surfaces in the edit
        # modal, opened separately -- not this table's own columns).
        body = workspace_tne._actions_tab(fresh_bundle)
        text = str(body)
        assert "Alex Chen" in text
        assert "Agreed" in text
    finally:
        run_ctx.executor.stop()
        adapters._ctx = None
        workspace_tne._CACHE.clear()


# ── CLAUDE.md §11 "Run cards on /runs (user decision, 2026-09-25)" View —
# checked directly against a real backend rather than assumed ─────────────

def test_workspace_layout_renders_an_awaiting_signoff_run_with_no_status_gate(monkeypatch, tmp_path):
    """runs_page._view_target routes a SKILL-001-shaped run (has_workspace)
    to /workspace/tne?run_id=<id> for BOTH Awaiting Signoff and Completed --
    the task this test was written for required checking, not assuming,
    that tne_workspace_layout actually renders an awaiting_signoff run
    correctly (rather than only a completed one, which would have meant
    routing Awaiting Signoff to /run/<id> instead). find/prioritise/act have
    all run by this point (CLAUDE.md gap #6's own _ELIGIBLE_STATUSES_FOR_
    TOTALS rationale: "a run in Awaiting Signoff has already run every test
    and computed every number -- only export is outstanding") and
    _load_bundle/get_run_payload (src/workspace_tne.py) build from whatever
    a run has persisted so far with no status check at all -- confirmed here
    against the REAL LocalPersistence backend, never fake_service.py."""
    import time
    from pathlib import Path as _Path

    from orchestrator import service as real_service

    repo_root = _Path(__file__).resolve().parents[2]
    mini_skill_dir = repo_root / "tests" / "fixtures" / "skills" / "mini"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_tiny_mini_data(data_dir)

    env = {
        "ORCH_BACKEND": "local",
        "ORCH_LOCAL_DB": str(tmp_path / "orch.db"),
        "ORCH_LOCAL_DATA_ROOT": str(data_dir),
        "ORCH_LOCAL_EXPORT_ROOT": str(tmp_path / "exports"),
        "SKILLS_DIR": str(mini_skill_dir.parent),
        "CODE_REVISION": "test-fixed-revision",
    }
    monkeypatch.setattr(adapters, "service", real_service)
    adapters._ctx = None
    original_build = real_service.build_app_context
    monkeypatch.setattr(real_service, "build_app_context", lambda *a, **k: original_build(env))
    workspace_tne._CACHE.clear()

    run_ctx = adapters.get_context()
    run_ctx.executor.start()
    try:
        bindings = real_service.suggest_bindings(run_ctx, "SKILL-MINI")
        run_id = adapters.start_audit_run(
            skill_id="SKILL-MINI", bindings=bindings, audit_period=("2026-01-01", "2026-02-28"),
            objective="awaiting-signoff render check", run_owner="tester@example.com",
        )

        def _wait(statuses, timeout=15):
            deadline = time.time() + timeout
            while time.time() < deadline:
                status = adapters.get_run(run_id)["status"]
                if status in statuses:
                    return status
                time.sleep(0.05)
            raise AssertionError(f"run {run_id} did not reach {statuses} in time")

        status = _wait({"awaiting_signoff", "failed"})
        assert status == "awaiting_signoff", adapters.get_run(run_id).get("status_reason")

        layout = workspace_tne.tne_workspace_layout(run_id)
        text = str(layout)
        assert "could not be loaded" not in text
        assert "No completed run yet" not in text
        assert "Executive Brief" in text
    finally:
        run_ctx.executor.stop()
        adapters._ctx = None
        workspace_tne._CACHE.clear()
