"""P6 WP N5 (docs/specs/P6_narration_design.md §3.2, §4.1): the payload
builders (orchestrator.narration.payloads) and the per-run placeholder
table (orchestrator.narration.run_values). Fully offline -- no endpoint, no
workspace; uses the real skills/tne_exco Skill (for its catalogue.yaml/
contract.yaml) but never reads its data files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.narration.payloads import (
    build_caption_payload,
    build_candidates_payload,
    build_exec_summary_payload,
    build_finding_payload,
    build_finding_table,
    build_priority_payload,
    build_profile_payload,
    build_remediation_payload,
    build_synthesis_payload,
    build_theme_table,
    finding_key,
    narration_call_context,
    pii_columns_masked,
)
from orchestrator.narration.run_values import run_values
from orchestrator.skills import load_skill

SKILL_DIR = Path(__file__).parent.parent / "skills" / "tne_exco"


@pytest.fixture(scope="module")
def skill():
    return load_skill(SKILL_DIR)


def _finding(
    finding_id="RUN1:T4_1", rule_id="SKILL-001.T4_1", test_id="T4.1", title="Missing Receipt Documentation",
    severity="High", severity_rule="missing_receipt_pct > 10", metrics_cited=None, threshold_refs=None,
    exposure_amount=None, monetary_basis="spend", observation="template observation",
    recommendation="template recommendation", management_questions=None, analyst_set_severity=True,
):
    return {
        "finding_id": finding_id, "rule_id": rule_id, "test_id": test_id, "title": title,
        "severity": severity, "severity_rule": severity_rule,
        "metrics_cited": metrics_cited or {}, "threshold_refs": threshold_refs or [],
        "exposure_amount": exposure_amount, "monetary_basis": monetary_basis,
        "observation": observation, "recommendation": recommendation,
        "management_questions": management_questions or ["Q1?"],
        "analyst_set_severity": analyst_set_severity,
    }


def _metric(value, unit="count", source_ref=None):
    return {"value": value, "unit": unit, "source_ref": source_ref or {}}


class _State:
    def __init__(self, audit_period=("2026-01-01", "2026-06-30"), test_results=None, profile_result=None):
        self.audit_period = audit_period
        self.test_results = test_results or []
        self.profile_result = profile_result or {}


# ── run_values ───────────────────────────────────────────────────────────


def test_run_values_counts_severities_and_tests():
    state = _State(test_results=[
        {"test_id": "T1", "status": "exception", "exception_units": 3},
        {"test_id": "T2", "status": "pass", "exception_units": 0},
        {"test_id": "T3", "status": "not_testable", "exception_units": 0},
    ])
    findings = [
        _finding(severity="High"), _finding(severity="High"),
        _finding(severity="Medium"), _finding(severity="Indeterminate"),
    ]
    metrics = {
        "run_exposure_headline": _metric(1234.5, unit="AUD"),
        "run_approved_not_spent_total": _metric(50.0, unit="AUD"),
    }
    table = run_values(state, findings, metrics)

    assert table["run_finding_count"].value == 4
    assert table["run_high_count"].value == 2
    assert table["run_medium_count"].value == 1
    assert table["run_low_count"].value == 0
    assert table["run_indeterminate_count"].value == 1
    assert table["run_tests_total"].value == 3
    assert table["run_tests_with_exceptions"].value == 1
    assert table["run_tests_not_testable"].value == 1
    assert table["run_exposure_headline"].value == 1234.5
    assert table["run_exposure_headline"].unit == "AUD"
    assert table["run_approved_not_spent_total"].value == 50.0
    assert table["run_audit_period_start"].value == "2026-01-01"
    assert table["run_audit_period_end"].value == "2026-06-30"


def test_run_values_never_fabricates_the_headline_when_absent():
    # Before prioritise/finalise has run, run_metrics has no
    # run_exposure_headline row -- the entry is None, never a fabricated 0.
    state = _State()
    table = run_values(state, [], {})
    assert table["run_exposure_headline"].value is None
    assert table["run_finding_count"].value == 0


def test_run_values_catalogue_tests_give_14_grain_counts(skill):
    # Independent review 2026-09-24 gap #5: run_tests_* must count SKILL-001's
    # 14 catalogue tests (skills/tne_exco/catalogue.yaml), never the plan's
    # own, larger set of primitive-instance sub-tests -- three of the 14
    # catalogue tests (T3.2a, T3.3a, T6.1d) resolve to several plan.yaml
    # sub-tests each, inflating a naive count to 21.
    from orchestrator.pptx_export import load_catalogue_rows

    catalogue_rows = load_catalogue_rows(skill.skill_dir)
    assert len(catalogue_rows) == 14

    sub_test_ids = {
        "T3.2a": ["T3.2a_air_dom", "T3.2a_air_int", "T3.2a_car_dom", "T3.2a_car_int", "T3.2a_accom"],
        "T3.3a": ["T3.3a_dom", "T3.3a_int", "T3.3a_very_late"],
        "T6.1d": ["T6.1d_dom", "T6.1d_int"],
    }
    exception_subs = {"T3.2a_air_dom", "T3.2a_air_int", "T6.1d_dom", "T6.1d_int"}
    not_testable_subs = {"T3.3a_dom", "T3.3a_int", "T3.3a_very_late"}
    single_status = {
        "T3.1a": "pass", "T3.1b": "pass", "T3.3b": "pass",
        "T4.1": "exception", "T4.2": "pass", "T4.3": "not_testable",
        "T4.4": "exception", "T5.1": "exception", "T5.2": "pass",
        "T6.1a": "pass", "T6.1c": "pass",
    }
    assert set(single_status) | set(sub_test_ids) == {t["test_id"] for t in catalogue_rows}

    test_results = []
    for subs in sub_test_ids.values():
        for sub_id in subs:
            status = "exception" if sub_id in exception_subs else (
                "not_testable" if sub_id in not_testable_subs else "pass")
            test_results.append({"test_id": sub_id, "status": status,
                                  "exception_units": 1 if status == "exception" else 0})
    for test_id, status in single_status.items():
        test_results.append({"test_id": test_id, "status": status,
                              "exception_units": 1 if status == "exception" else 0})
    assert len(test_results) == 21  # plan grain: sub-tests inflate 14 catalogue tests to 21

    state = _State(test_results=test_results)

    catalogue_table = run_values(state, [], {}, catalogue_tests=catalogue_rows)
    assert catalogue_table["run_tests_total"].value == 14
    assert catalogue_table["run_tests_with_exceptions"].value == 5  # T3.2a, T6.1d, T4.1, T4.4, T5.1
    assert catalogue_table["run_tests_not_testable"].value == 2  # T3.3a, T4.3

    # Without catalogue_tests, the prior plan-grain fallback still counts 21
    # sub-tests -- proves the fix changes behaviour, not just adds an unused
    # parameter.
    plan_grain_table = run_values(state, [], {})
    assert plan_grain_table["run_tests_total"].value == 21
    assert plan_grain_table["run_tests_with_exceptions"].value == 7
    assert plan_grain_table["run_tests_not_testable"].value == 4


# ── build_finding_table / build_finding_payload (`find`) ───────────────────


def test_build_finding_table_omits_null_metrics_and_includes_thresholds_and_period():
    finding = _finding(
        metrics_cited={
            "missing_receipt_count": _metric(12, unit="count"),
            "missing_receipt_pct": _metric(8.5, unit="%"),
            "not_yet_computed": _metric(None, unit="count"),
        },
        threshold_refs=[{
            "id": "missing_receipt_high_pct", "value": 10, "unit": "%",
            "provenance_type": "analyst-set", "pending_policy_confirmation": True,
        }],
        exposure_amount=6000.0,
    )
    table = build_finding_table(finding, skill=_DummySkillNoCatalogue(), period=("2026-01-01", "2026-06-30"))

    assert set(table) == {
        "missing_receipt_count", "missing_receipt_pct", "missing_receipt_high_pct",
        "exposure_amount", "run_audit_period_start", "run_audit_period_end",
    }
    assert table["missing_receipt_count"].value == 12
    assert "analyst-set, pending policy confirmation" in table["missing_receipt_high_pct"].meaning
    assert table["exposure_amount"].value == 6000.0
    assert table["run_audit_period_start"].value == "2026-01-01"


class _DummySkillNoCatalogue:
    """A minimal stand-in exposing only what build_finding_table reads
    (`.contract`, `.skill_dir`) -- catalogue.yaml lookups resolve to {}
    when skill_dir has none, which is fine for a table-shape test."""

    contract = {"sources": {}}
    skill_dir = Path("/nonexistent-catalogue-dir")


def test_build_finding_payload_uses_the_real_catalogue(skill):
    finding = _finding(
        test_id="T4.1",
        metrics_cited={"missing_receipt_count": _metric(12, unit="count")},
    )
    payload, table = build_finding_payload(finding, skill=skill, period=("2026-01-01", "2026-06-30"))

    assert payload["finding_key"] == "T4_1"
    assert payload["test_id"] == "T4.1"
    assert payload["control_objective"]  # resolved from catalogue.yaml, non-empty
    assert payload["template_observation"] == "template observation"
    placeholders = {p["placeholder"]: p for p in payload["placeholders"]}
    assert "{count:missing_receipt_count}" in placeholders
    assert placeholders["{count:missing_receipt_count}"]["rendered"] == "12"
    assert "missing_receipt_count" in table


def test_build_finding_payload_json_serialisable(skill):
    finding = _finding(metrics_cited={"missing_receipt_count": _metric(12)})
    payload, _ = build_finding_payload(finding, skill=skill, period=("2026-01-01", "2026-06-30"))
    json.dumps(payload)  # must not raise


# ── build_synthesis_payload / build_theme_table (`find_synthesis`) ─────────


def test_build_synthesis_payload_lists_every_finding_with_its_table(skill):
    findings = [
        _finding(finding_id="R:T4_1", rule_id="SKILL-001.T4_1", test_id="T4.1",
                 metrics_cited={"missing_receipt_count": _metric(12)}),
        _finding(finding_id="R:T5_1", rule_id="SKILL-001.T5_1", test_id="T5.1",
                 metrics_cited={"split_claims_count": _metric(3)}),
    ]
    payload, tables = build_synthesis_payload(findings, skill=skill)
    keys = {item["key"] for item in payload["findings"]}
    assert keys == {"T4_1", "T5_1"}
    assert set(tables) == {"T4_1", "T5_1"}
    assert "missing_receipt_count" in tables["T4_1"]


def test_build_theme_table_unions_member_findings(skill):
    findings = [
        _finding(metrics_cited={"missing_receipt_count": _metric(12)}),
        _finding(finding_id="R:T5_1", rule_id="SKILL-001.T5_1", test_id="T5.1",
                 metrics_cited={"split_claims_count": _metric(3)}),
    ]
    table = build_theme_table(findings, skill=skill)
    assert set(table) == {"missing_receipt_count", "split_claims_count"}


# ── build_candidates_payload (`find_candidates`) ────────────────────────────


def _tne_test(test_id, *, flag="RF_X", metrics=None):
    return {
        "test_id": test_id, "flag": flag, "primitive": "threshold_exceedance",
        "params": {"flag": flag, "metrics": metrics or {"x_amount": {"kind": "sum", "unit": "AUD"}}},
    }


class _SkillWithPlan:
    def __init__(self, skill, tests):
        self.contract = skill.contract
        self.skill_dir = skill.skill_dir
        self.plan = {"tests": tests}


def test_build_candidates_payload_returns_none_when_no_exceptions(skill):
    s = _SkillWithPlan(skill, [_tne_test("T4.1")])
    state = _State(test_results=[{"test_id": "T4.1", "status": "pass", "exception_units": 0}])
    result = build_candidates_payload(skill=s, state=state, metrics={}, findings=[])
    assert result is None


def test_build_candidates_payload_includes_every_testable_test_with_metrics(skill):
    s = _SkillWithPlan(skill, [
        _tne_test("T4.1", metrics={"missing_receipt_count": {"kind": "sum", "unit": "count"}}),
        {"test_id": "T9.9", "not_testable": {"reason": "no source"}},
    ])
    state = _State(test_results=[
        {"test_id": "T4.1", "status": "exception", "exception_units": 5},
        {"test_id": "T9.9", "status": "not_testable", "exception_units": 0},
    ])
    metrics = {"missing_receipt_count": _metric(5)}
    finding = _finding(metrics_cited={"missing_receipt_count": _metric(5)})

    result = build_candidates_payload(skill=s, state=state, metrics=metrics, findings=[finding])
    assert result is not None
    payload, tables = result
    test_ids = {t["test_id"] for t in payload["tests"]}
    assert test_ids == {"T4.1"}  # not_testable excluded
    metric_item = payload["tests"][0]["metrics"][0]
    assert metric_item["covering_finding_keys"] == ["T4_1"]
    assert "missing_receipt_count" in tables["T4.1"]


# ── build_priority_payload / build_remediation_payload ──────────────────────


def test_build_priority_payload_preserves_caller_order(skill):
    items = [
        _finding(finding_id="R:B", rule_id="SKILL-001.B", severity="Low", title="B"),
        _finding(finding_id="R:A", rule_id="SKILL-001.A", severity="High", title="A"),
    ]
    payload, tables = build_priority_payload(items, skill=skill)
    assert [i["key"] for i in payload["items"]] == ["B", "A"]
    assert set(tables) == {"B", "A"}


def test_build_remediation_payload_carries_effective_recommendation(skill):
    items = [_finding(recommendation="Do the thing.")]
    payload, _ = build_remediation_payload(items, skill=skill)
    assert payload["items"][0]["effective_recommendation"] == "Do the thing."


# ── build_exec_summary_payload (`export_summary`) ───────────────────────────


def test_build_exec_summary_payload_none_on_a_clean_run():
    state = _State()
    assert build_exec_summary_payload(state, [], {}) is None


def test_build_exec_summary_payload_top_5_by_severity():
    state = _State()
    findings = [_finding(finding_id=f"R:{i}", rule_id=f"SKILL-001.T{i}", severity=sev, title=f"F{i}")
                for i, sev in enumerate(["Low", "High", "Medium", "High", "Low", "High"])]
    payload, table = build_exec_summary_payload(state, findings, {})
    assert len(payload["top_findings"]) == 5
    assert payload["top_findings"][0]["severity"] == "High"
    assert table["run_finding_count"].value == 6


# ── build_caption_payload (`export_caption`) ────────────────────────────────


def test_build_caption_payload_resolves_named_metrics_only():
    charts = [{"chart_id": "chart_high", "what_it_plots": "High findings", "metric_names": ["a", "missing"]}]
    metrics = {"a": _metric(3)}
    payload, tables = build_caption_payload(charts, metrics)
    assert payload["charts"][0]["chart_id"] == "chart_high"
    names = {p["placeholder"] for p in payload["charts"][0]["placeholders"]}
    assert names == {"{count:a}"}
    assert set(tables["chart_high"]) == {"a"}


# ── build_profile_payload (`profile`) ───────────────────────────────────────


def test_build_profile_payload_top_20_nulls_across_sources_and_row_counts():
    profile_result = {
        "expense_report": {
            "row_count": 100,
            "null_counts": {f"Col{i}": i for i in range(15)},
        },
        "missing_receipt": {
            "row_count": 40,
            "null_counts": {f"Other{i}": 20 - i for i in range(10)},
        },
    }
    state = _State(profile_result=profile_result)
    payload, table = build_profile_payload(state)

    assert table["rows_expense_report"].value == 100
    assert table["rows_missing_receipt"].value == 40
    null_entries = [n for n in table if n.startswith("nulls_")]
    assert len(null_entries) == 20
    # highest null counts win: Other0..Other9 (20..11) beat every Col value (<=14)
    assert "nulls_missing_receipt_other0" in table
    assert table["nulls_missing_receipt_other0"].value == 20


def test_build_profile_payload_placeholder_uses_a_slug_never_the_raw_column_name():
    # The column NAME may still appear in human-readable `meaning` text (it
    # is metadata, not a cell VALUE, and G15 is about values) -- but the
    # placeholder TOKEN itself, which is what the model is invited to
    # write back verbatim, is always the sanitised slug form.
    profile_result = {"expense_report": {"row_count": 5, "null_counts": {"Employee ID": 2}}}
    state = _State(profile_result=profile_result)
    payload, table = build_profile_payload(state)
    placeholders = [p["placeholder"] for p in payload["placeholders"]]
    assert "{count:nulls_expense_report_employee_id}" in placeholders
    assert not any("Employee" in p for p in placeholders)
    assert all(name == name.lower() for name in table if name.startswith("nulls_"))


# ── PII plumbing ─────────────────────────────────────────────────────────


def test_pii_columns_masked_matches_the_real_contract(skill):
    masked = pii_columns_masked(skill)
    assert "Employee" in masked
    assert "Employee ID" in masked
    assert masked == sorted(masked)


def test_narration_call_context_always_has_empty_whitelist(skill):
    ctx = narration_call_context(skill, run_id="RUN1")
    assert ctx.pii_whitelist == []
    assert "Employee" in ctx.pii_columns_masked


# ── G15 sentinel, at payload level (§11 "G15 ... at payload level") ────────
#
# The full G15 gate (a recording model client, a real run, every logged
# `llm_calls.messages_json` row) is WP N13 scope -- this module has no run,
# no data source and no model client to wire one through. What belongs
# here, and is testable now, is the module docstring's own claim (top of
# this file's source, orchestrator.narration.payloads): every builder reads
# only already-aggregated dicts (`run_metrics`/`findings`/`test_results`/
# `profile_result`), so a PII column's real VALUE has no path into a
# payload at all -- not because something filters it out, but because
# nothing here ever receives it. A sentinel planted where a bug COULD
# plausibly introduce a leak (a PII column's own contract metadata, which
# `pii_columns_masked` walks) proves the one function that does read
# `skill.contract` still surfaces only column NAMES.

import copy
import inspect

_PII_SENTINEL = "SENTINEL-PII-7731@example.test"


def test_g15_pii_columns_masked_never_echoes_a_value_planted_in_contract_metadata(skill):
    contract = copy.deepcopy(skill.contract)
    # A value that could only reach here via contract METADATA -- never a
    # raw row -- planted on a column spec key pii_columns_masked() does not
    # read (it only ever reads the dict KEY and the "pii" flag; see its own
    # docstring/source above).
    contract["sources"]["expense_report"]["columns"]["Employee"]["description"] = _PII_SENTINEL

    class _ContractOnly:
        pass

    stand_in = _ContractOnly()
    stand_in.contract = contract
    masked = pii_columns_masked(stand_in)

    assert "Employee" in masked
    assert all(_PII_SENTINEL not in name for name in masked)


def test_g15_payload_builders_take_no_row_level_or_data_source_argument():
    # Structural half of the same guarantee: no builder in this module can
    # be CALLED with a DataFrame, a row list or ctx.data_source in the first
    # place -- their own parameter names never invite one.
    forbidden_param_names = {"ctx", "data_source", "df", "dataframe", "rows", "row"}
    builders = [
        build_finding_payload, build_synthesis_payload, build_candidates_payload,
        build_priority_payload, build_remediation_payload, build_exec_summary_payload,
        build_caption_payload, build_profile_payload,
    ]
    for fn in builders:
        params = set(inspect.signature(fn).parameters)
        offending = params & forbidden_param_names
        assert not offending, f"{fn.__name__} accepts {offending} -- a row-level input path"


def test_g15_sentinel_absent_from_every_payload_built_with_tainted_contract_metadata(skill):
    # Runs a full round of every payload builder using ordinary, legitimate
    # aggregate inputs (no sentinel anywhere in run_metrics/findings/
    # profile_result -- those never carry one for real), alongside a
    # skill whose CONTRACT metadata carries the sentinel the way the first
    # test above plants it. If a payload builder ever started reading
    # skill.contract for anything beyond pii_columns_masked's own column-
    # name walk, this is what would catch it.
    contract = copy.deepcopy(skill.contract)
    contract["sources"]["expense_report"]["columns"]["Employee"]["description"] = _PII_SENTINEL

    class _TaintedSkill:
        pass

    tainted = _TaintedSkill()
    tainted.contract = contract
    tainted.skill_dir = skill.skill_dir

    finding = _finding(metrics_cited={"missing_receipt_count": _metric(12)})
    state = _State(profile_result={"expense_report": {"row_count": 5, "null_counts": {"Employee ID": 2}}})
    period = ("2026-01-01", "2026-06-30")

    payloads = [
        narration_call_context(tainted, run_id="RUN1").__dict__,
        build_finding_payload(finding, skill=tainted, period=period)[0],
        build_synthesis_payload([finding], skill=tainted)[0],
        build_priority_payload([finding], skill=tainted, period=period)[0],
        build_remediation_payload([finding], skill=tainted, period=period)[0],
        build_exec_summary_payload(state, [finding], {})[0],
        build_caption_payload([{"chart_id": "c", "what_it_plots": "x", "metric_names": []}], {})[0],
        build_profile_payload(state)[0],
    ]
    for payload in payloads:
        assert _PII_SENTINEL not in json.dumps(payload, default=str)


# ── finding_key ──────────────────────────────────────────────────────────


def test_finding_key_strips_the_skill_prefix():
    assert finding_key({"rule_id": "SKILL-001.T4_1"}) == "T4_1"
    assert finding_key({"rule_id": "SKILL-001.ai.abc123"}) == "ai.abc123"
    assert finding_key({"finding_id": "no-dot-here"}) == "no-dot-here"
