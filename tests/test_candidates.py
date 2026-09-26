"""T-C1 (P6 WP N8, docs/specs/P6_narration_design.md §5.1, §11): the
finding-candidates validator's identity, monetary-basis derivation, headline
eligibility, the C-1..C-5 data rules (positive and negative, on
`orchestrator.narration.candidates._build_candidate_row` directly -- these
are Python-only drops, never a repair round, §5.1), the C-6 prose gate, G10
(a clean dataset never even attempts the call), the cap, `rule_id` stability
across separate runs, the switch, and an unavailable model giving zero
candidates -- never an invented one."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from orchestrator.adapters.model_fake import RaisingModelClient
from orchestrator.llm.errors import ModelUnavailable
from orchestrator.narration.candidates import (
    _build_candidate_row,
    _derive_monetary_basis,
    _headline_eligibility,
    _validate_response,
    candidate_id_for,
    rule_id_for,
)
from orchestrator.narration.placeholders import PlaceholderEntry
from orchestrator.nodes.narration import narrate
from tests.narration_test_support import (
    DispatchingModelClient,
    happy_responses,
    make_narration_harness,
    resp,
    run_to_narrate_input,
)

MINI_CANDIDATES_SKILL_DIR = Path(__file__).parent / "fixtures" / "skills" / "mini_candidates"
MARKER_CANDIDATES = "finding-candidates/1"


# ── identity (§5.1's own table) ─────────────────────────────────────────────


def test_rule_id_is_deterministic_and_order_independent():
    a = rule_id_for("SKILL-X", ["T2"], ["missing_amount"])
    b = rule_id_for("SKILL-X", ["T2"], ["missing_amount"])
    assert a == b
    assert a.startswith("SKILL-X.ai.")

    c = rule_id_for("SKILL-X", ["T2", "T1"], ["missing_amount", "hv_amount"])
    d = rule_id_for("SKILL-X", ["T1", "T2"], ["hv_amount", "missing_amount"])
    assert c == d


def test_rule_id_differs_for_a_different_metric_set():
    a = rule_id_for("SKILL-X", ["T2"], ["missing_amount"])
    b = rule_id_for("SKILL-X", ["T2"], ["missing_count"])
    assert a != b


def test_candidate_id_is_deterministic_per_run_generation_rule_but_not_shared_across_generations():
    rid = rule_id_for("SKILL-X", ["T2"], ["missing_amount"])
    a = candidate_id_for("RUN-1", 0, rid)
    b = candidate_id_for("RUN-1", 0, rid)
    assert a == b
    c = candidate_id_for("RUN-1", 1, rid)
    assert c != a
    d = candidate_id_for("RUN-2", 0, rid)
    assert d != a


# ── monetary basis derivation (§5.1) ────────────────────────────────────────


def test_basis_with_no_amount_metrics_is_none():
    basis, note = _derive_monetary_basis([], [], {})
    assert (basis, note) == ("none", None)


def test_basis_uses_the_agreeing_rule_findings_own_basis():
    findings = [{"metrics_cited": {"missing_amount": {}}, "monetary_basis": "spend"}]
    basis, note = _derive_monetary_basis(["missing_amount"], findings, {})
    assert basis == "spend"
    assert note is None


def test_basis_disagreeing_rules_citing_the_same_metric_give_none():
    findings = [
        {"metrics_cited": {"x": {}}, "monetary_basis": "spend"},
        {"metrics_cited": {"x": {}}, "monetary_basis": "excess"},
    ]
    basis, note = _derive_monetary_basis(["x"], findings, {})
    assert basis == "none"
    assert "disagree" in note


def test_basis_falls_back_to_excess_kind_when_no_rule_cites_it():
    basis, note = _derive_monetary_basis(["daily_over_amount"], [], {"daily_over_amount": "excess"})
    assert basis == "excess"


def test_basis_with_no_rule_and_a_non_excess_kind_is_none_with_a_reason():
    basis, note = _derive_monetary_basis(["missing_amount"], [], {"missing_amount": "sum"})
    assert basis == "none"
    assert "no declared monetary basis" in note


def test_basis_disagreement_across_two_metrics_gives_none():
    findings = [{"metrics_cited": {"a": {}}, "monetary_basis": "spend"}]
    basis, note = _derive_monetary_basis(["a", "b"], findings, {"b": "excess"})
    assert basis == "none"
    assert "disagree" in note


def test_basis_agreement_across_two_metrics_uses_the_shared_basis():
    findings = [{"metrics_cited": {"a": {}}, "monetary_basis": "spend"}]
    basis, note = _derive_monetary_basis(["a", "b"], findings, {"b": "sum"})
    assert basis == "spend"


# ── headline eligibility, read only from persisted test_line_values ────────


def test_headline_eligible_on_spend_with_a_priced_line():
    rows = {"T2": [{"line_key": "L1", "spend_amount": 100.0, "excess_amount": None}]}
    ok, reason = _headline_eligibility("spend", ["T2"], rows)
    assert ok and reason is None


def test_headline_ineligible_on_excess_when_no_row_has_an_excess_amount():
    rows = {"T2": [{"line_key": "L1", "spend_amount": 100.0, "excess_amount": None}]}
    ok, reason = _headline_eligibility("excess", ["T2"], rows)
    assert not ok
    assert "excess allocation" in reason


def test_headline_eligible_on_excess_with_a_priced_line():
    rows = {"T2": [{"line_key": "L1", "spend_amount": 100.0, "excess_amount": 40.0}]}
    ok, reason = _headline_eligibility("excess", ["T2"], rows)
    assert ok


def test_headline_ineligible_with_no_test_line_values_rows_at_all():
    ok, reason = _headline_eligibility("spend", ["T2"], {})
    assert not ok
    assert "no test_line_values rows" in reason


def test_headline_ineligible_for_approved_not_spent():
    rows = {"T2": [{"line_key": "L1", "spend_amount": 1.0, "excess_amount": None}]}
    ok, reason = _headline_eligibility("approved_not_spent", ["T2"], rows)
    assert not ok
    assert "reported separately" in reason


def test_headline_ineligible_for_a_none_basis():
    ok, reason = _headline_eligibility("none", ["T2"], {})
    assert not ok


# ── C-1..C-5 (§5.1), on _build_candidate_row directly ───────────────────────

_METRICS = {
    "hv_count": {"value": 3, "unit": "count", "test_id": "T1"},
    "hv_amount": {"value": 2200.0, "unit": "AUD", "test_id": "T1"},
    "missing_count": {"value": 3, "unit": "count", "test_id": "T2"},
    "missing_pct": {"value": 60.0, "unit": "%", "test_id": "T2"},
    "missing_amount": {"value": 1700.0, "unit": "AUD", "test_id": "T2"},
    "t3_metric": {"value": 5, "unit": "count", "test_id": "T3"},
    "run_finding_count": {"value": 2, "unit": "count", "test_id": None},
    # Uncited by any rule finding AND not an additive-currency metric --
    # a valid C-2 anchor with no dollar figure at all.
    "missing_employees": {"value": 2, "unit": "count", "test_id": "T2"},
}
_FINDINGS = [
    {"metrics_cited": {"hv_count": _METRICS["hv_count"], "hv_amount": _METRICS["hv_amount"]}, "monetary_basis": "spend"},
    {"metrics_cited": {"missing_count": _METRICS["missing_count"], "missing_pct": _METRICS["missing_pct"]}, "monetary_basis": "none"},
]
_RULE_CITED = {"hv_count", "hv_amount", "missing_count", "missing_pct"}
_TEST_RESULTS = {
    "T1": {"test_id": "T1", "status": "exception", "exception_units": 3},
    "T2": {"test_id": "T2", "status": "exception", "exception_units": 3},
    "T3": {"test_id": "T3", "status": "not_testable", "exception_units": 0},
}
_AMOUNT_NAMES = {"hv_amount", "missing_amount"}
_KIND_BY_NAME = {"hv_amount": "sum", "missing_amount": "sum"}
_TLV = {"T2": [{"line_key": "L1", "spend_amount": 600.0, "excess_amount": None}, {"line_key": "L2", "spend_amount": 50.0, "excess_amount": None}]}

_VALID_ITEM = {
    "title": "Missing Register Match Amount",
    "metrics_cited": ["missing_amount"],
    "proposed_severity": "Low",
    "severity_reason": "reason",
    "rationale": "rationale",
    "observation": "obs",
    "recommendation": "rec",
    "management_questions": ["What causes a claim to be missing its register match?"],
}


def _build(item: dict, **overrides) -> dict | None:
    kwargs = dict(
        run_id="RUN-1", generation=0, skill_id="SKILL-MINI-CAND", engagement_id="ENG-1",
        metrics=_METRICS, rule_cited_metric_names=_RULE_CITED, test_results_by_id=_TEST_RESULTS,
        amount_metric_names=_AMOUNT_NAMES, metric_kind_by_name=_KIND_BY_NAME, findings=_FINDINGS,
        decided_rule_ids=set(), seen_rule_ids=set(), test_line_values_by_test=_TLV, call_id="CALL-1",
    )
    kwargs.update(overrides)
    return _build_candidate_row(item, **kwargs)


def test_c1_a_valid_anchored_candidate_builds_a_row():
    row = _build(_VALID_ITEM)
    assert row is not None
    assert row["rule_id"] == rule_id_for("SKILL-MINI-CAND", ["T2"], ["missing_amount"])
    assert row["metrics_cited"] == ["missing_amount"]
    assert row["producing_test_ids"] == ["T2"]
    assert row["exposure_amount"] == 1700.0
    assert row["candidate_status"] == "candidate"


def test_c1_empty_metrics_cited_is_dropped():
    assert _build({**_VALID_ITEM, "metrics_cited": []}) is None


def test_c1_more_than_eight_metrics_is_dropped():
    item = {**_VALID_ITEM, "metrics_cited": ["missing_amount"] * 1 + [f"x{i}" for i in range(8)]}
    assert _build(item) is None


def test_c1_unknown_metric_name_is_dropped():
    assert _build({**_VALID_ITEM, "metrics_cited": ["not_a_real_metric"]}) is None


def test_c1_a_run_level_metric_is_dropped():
    assert _build({**_VALID_ITEM, "metrics_cited": ["run_finding_count"]}) is None


def test_c2_anchor_a_metric_already_cited_by_a_rule_finding_is_dropped():
    # hv_amount is fully covered by T1's rule finding -- no anchor left.
    assert _build({**_VALID_ITEM, "metrics_cited": ["hv_amount"]}) is None


def test_c2_anchor_zero_exception_test_gives_no_candidate():
    tr = {**_TEST_RESULTS, "T2": {"test_id": "T2", "status": "pass", "exception_units": 0}}
    assert _build(_VALID_ITEM, test_results_by_id=tr) is None


def test_c2_anchor_zero_valued_metric_gives_no_candidate():
    metrics = {**_METRICS, "missing_amount": {"value": 0, "unit": "AUD", "test_id": "T2"}}
    assert _build(_VALID_ITEM, metrics=metrics) is None


def test_c2_anchor_present_even_when_a_second_cited_metric_is_already_covered():
    # missing_amount is the anchor; missing_count is already cited by T2's
    # own rule finding -- that does not disqualify the candidate (§5.1: "at
    # least one" anchor).
    item = {**_VALID_ITEM, "metrics_cited": ["missing_amount", "missing_count"]}
    row = _build(item)
    assert row is not None


def test_c3_a_not_testable_producing_test_is_dropped():
    item = {**_VALID_ITEM, "metrics_cited": ["missing_amount", "t3_metric"]}
    assert _build(item) is None


def test_c4_duplicate_of_an_already_decided_candidate_is_dropped():
    rid = rule_id_for("SKILL-MINI-CAND", ["T2"], ["missing_amount"])
    assert _build(_VALID_ITEM, decided_rule_ids={rid}) is None


def test_c4_duplicate_within_one_batch_keeps_the_first():
    rid = rule_id_for("SKILL-MINI-CAND", ["T2"], ["missing_amount"])
    assert _build(_VALID_ITEM, seen_rule_ids={rid}) is None


def test_invalid_proposed_severity_is_dropped():
    assert _build({**_VALID_ITEM, "proposed_severity": "Critical"}) is None


def test_a_candidate_with_no_additive_amount_metric_is_never_headline_eligible():
    item = {**_VALID_ITEM, "metrics_cited": ["missing_employees"]}
    row = _build(item)
    assert row is not None
    assert row["exposure_amount"] is None
    assert row["headline_eligible"] is False


# ── C-6 (prose validation) ──────────────────────────────────────────────────


def _table_for(name: str, *, unit: str = "AUD", value=1700.0) -> dict[str, PlaceholderEntry]:
    return {
        name: PlaceholderEntry(
            name=name, unit=unit, value=value, source_field=f"run_metrics.{name}", meaning="a test metric",
        )
    }


def test_validate_response_rejects_a_placeholder_in_the_title():
    table = _table_for("missing_amount")
    parsed = {"candidates": [{**_VALID_ITEM, "title": "Amount {money:missing_amount} at risk"}]}
    ok, violations = _validate_response(parsed, table, _METRICS, {})
    assert not ok
    assert any(v["rule_id"] == "N-L2" for v in violations)


def test_validate_response_requires_coverage_of_the_cited_metric():
    table = _table_for("missing_amount")
    parsed = {"candidates": [{**_VALID_ITEM, "observation": "No placeholder appears in this sentence at all."}]}
    ok, violations = _validate_response(parsed, table, _METRICS, {})
    assert not ok
    assert any(v["rule_id"] == "N-C1" for v in violations)


def test_validate_response_accepts_well_formed_prose():
    table = _table_for("missing_amount")
    parsed = {
        "candidates": [
            {**_VALID_ITEM, "observation": "This candidate identifies {money:missing_amount} at risk."},
        ]
    }
    ok, violations = _validate_response(parsed, table, _METRICS, {})
    assert ok, violations


# ── C-6/C-7 (BUG-R5-3, independent review 2026-09-26): a candidate combining
# a zero-valued exception metric with an unrelated non-zero one from the
# same test, or metrics from two different tests, is rejected -- reproducing
# RUN-8A1EDA1D86C5's "0 rows ... conflict, involving 1 employees" candidate
# (daily_over_employees_dom=1, t61d_dom_city_currency_conflict_rows=0, same
# test_id) ─────────────────────────────────────────────────────────────────


def _two_metric_table(metrics: dict[str, dict]) -> dict[str, PlaceholderEntry]:
    return {
        name: PlaceholderEntry(name=name, unit=row["unit"], value=row["value"], meaning="a test metric")
        for name, row in metrics.items()
    }


def test_validate_response_rejects_a_zero_valued_exception_metric_next_to_a_real_one():
    metrics = {
        **_METRICS,
        "conflict_rows": {"value": 0, "unit": "count", "test_id": "T2"},
        "over_limit_employees": {"value": 1, "unit": "count", "test_id": "T2"},
    }
    table = _two_metric_table(metrics)
    parsed = {
        "candidates": [
            {
                **_VALID_ITEM,
                "metrics_cited": ["conflict_rows", "over_limit_employees"],
                "observation": "This identifies {count:conflict_rows} conflict(s) involving "
                "{count:over_limit_employees} employee(s).",
            }
        ]
    }
    ok, violations = _validate_response(parsed, table, metrics, {})
    assert not ok
    assert any(v["rule_id"] == "C-6" for v in violations)


def test_validate_response_rejects_metrics_from_two_different_tests():
    table = _two_metric_table(
        {"missing_amount": _METRICS["missing_amount"], "hv_amount": _METRICS["hv_amount"]}
    )
    parsed = {
        "candidates": [
            {
                **_VALID_ITEM,
                "metrics_cited": ["missing_amount", "hv_amount"],
                "observation": "This identifies {money:missing_amount} and {money:hv_amount} at risk.",
            }
        ]
    }
    ok, violations = _validate_response(parsed, table, _METRICS, {})
    assert not ok
    assert any(v["rule_id"] == "C-7" for v in violations)


def test_validate_response_allows_a_zero_context_metric_alongside_a_real_one():
    # A population-size denominator legitimately citable even if the count
    # it counts happens to be zero -- never flagged as C-6.
    metrics = {
        "missing_count": _METRICS["missing_count"],
        "missing_pct": {"value": 0.0, "unit": "%", "test_id": "T2"},
    }
    table = _two_metric_table(metrics)
    kind_by_name = {"missing_pct": "pct_of_population"}
    parsed = {
        "candidates": [
            {
                **_VALID_ITEM,
                "metrics_cited": ["missing_count", "missing_pct"],
                "observation": "This identifies {count:missing_count} claim(s), {pct:missing_pct} of the population.",
            }
        ]
    }
    ok, violations = _validate_response(parsed, table, metrics, {}, kind_by_name, {})
    assert ok, violations


# ── integration: narrate() with the candidate step wired in ────────────────


def _candidate_response(*, metrics_cited=("missing_amount",), title="Missing Register Match Amount"):
    return resp(
        {
            "schema_version": MARKER_CANDIDATES,
            "candidates": [
                {
                    "title": title,
                    "metrics_cited": list(metrics_cited),
                    "proposed_severity": "Low",
                    "severity_reason": "This is a modest amount relative to total spend.",
                    "rationale": "No rule finding currently writes up this test's dollar impact.",
                    "observation": (
                        "The claims missing a register match total {money:missing_amount} in this run."
                    ),
                    "recommendation": "Quantify the dollar impact of unmatched claims in the review.",
                    "management_questions": ["What is the dollar impact of claims missing a register match?"],
                }
            ],
        }
    )


def test_switch_off_makes_no_find_candidates_call(local_persistence, tmp_path):
    responses = happy_responses()  # deliberately no MARKER_CANDIDATES entry
    client = DispatchingModelClient(responses)
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client, skill_dir=MINI_CANDIDATES_SKILL_DIR,
        ai_proposed_findings_enabled=False,
    )
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    assert local_persistence.list_candidates(state.run_id) == []
    calls = local_persistence.list_llm_calls(state.run_id)
    assert all(c["task"] != "find_candidates" for c in calls)


def test_a_successful_proposal_is_persisted_as_a_candidate_with_narratives(local_persistence, tmp_path):
    responses = happy_responses()
    responses[MARKER_CANDIDATES] = _candidate_response()
    client = DispatchingModelClient(responses)
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client, skill_dir=MINI_CANDIDATES_SKILL_DIR,
        ai_proposed_findings_enabled=True,
    )
    state = run_to_narrate_input(h)
    result = narrate(h.ctx, state)

    candidates = local_persistence.list_candidates(state.run_id)
    assert len(candidates) == 1
    row = candidates[0]
    assert row["candidate_status"] == "candidate"
    assert row["metrics_cited"] == ["missing_amount"]
    assert row["rule_id"] == rule_id_for("SKILL-MINI-CAND", ["T2"], ["missing_amount"])

    narratives = {
        n["field"]: n for n in local_persistence.get_narratives(state.run_id)
        if n["target_kind"] == "candidate" and n["target_id"] == row["candidate_id"]
    }
    assert set(narratives) == {"observation", "recommendation", "management_questions"}
    assert narratives["observation"]["origin"] == "model"
    assert "{money:missing_amount}" in narratives["observation"]["template_text"]

    assert any("AI-proposed candidate" in e["message"] for e in result.events)
    assert any(e["message"].endswith("1 AI-proposed") for e in result.events)


def test_rule_id_is_stable_across_two_separate_runs(local_persistence, tmp_path):
    responses1 = happy_responses()
    responses1[MARKER_CANDIDATES] = _candidate_response()
    h1 = make_narration_harness(
        local_persistence, tmp_path / "run1", model_client=DispatchingModelClient(responses1),
        skill_dir=MINI_CANDIDATES_SKILL_DIR, ai_proposed_findings_enabled=True,
    )
    state1 = run_to_narrate_input(h1)
    narrate(h1.ctx, state1)
    row1 = local_persistence.list_candidates(state1.run_id)[0]

    responses2 = happy_responses()
    responses2[MARKER_CANDIDATES] = _candidate_response()
    h2 = make_narration_harness(
        local_persistence, tmp_path / "run2", model_client=DispatchingModelClient(responses2),
        skill_dir=MINI_CANDIDATES_SKILL_DIR, ai_proposed_findings_enabled=True,
    )
    state2 = run_to_narrate_input(h2)
    narrate(h2.ctx, state2)
    row2 = local_persistence.list_candidates(state2.run_id)[0]

    assert state1.run_id != state2.run_id
    assert row1["rule_id"] == row2["rule_id"]
    assert row1["candidate_id"] != row2["candidate_id"]


def test_cap_truncates_the_candidates_list(local_persistence, tmp_path, monkeypatch):
    # The wire schema's own `maxItems == max_candidates` (§5.1: schema
    # builders, `finding_candidates_schema`) already stops a strict-mode
    # model from OVER-proposing -- so C-5's Python-side truncation is
    # defense in depth for a model that does not honour it (GPT-OSS
    # non-strict mode, or Sonnet without strict-schema support, CLAUDE.md
    # §6). Exercising that path means bypassing the schema-validating
    # gateway call itself, not feeding it an over-large response it would
    # legitimately reject before C-5 is ever reached.
    from orchestrator.narration import runner as runner_module

    real_generate_item = runner_module._generate_item

    def fake_generate_item(rc, *, task, payload, schema, extra_params, validate_fn, **_):
        if task != "find_candidates":
            return real_generate_item(
                rc, task=task, payload=payload, schema=schema, extra_params=extra_params, validate_fn=validate_fn,
            )
        candidates = [
            {
                "title": f"Matter {i}",
                "metrics_cited": ["missing_amount"],
                "proposed_severity": "Low",
                "severity_reason": "reason",
                "rationale": "rationale",
                "observation": "This candidate identifies {money:missing_amount} at risk.",
                "recommendation": "Investigate.",
                "management_questions": ["q?"],
            }
            for i in range(3)
        ]
        return runner_module.NarrationOutcome("model", {"candidates": candidates}, ["CALL-1"], "test-model", None)

    monkeypatch.setattr(runner_module, "_generate_item", fake_generate_item)

    client = DispatchingModelClient(happy_responses())
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client, skill_dir=MINI_CANDIDATES_SKILL_DIR,
        ai_proposed_findings_enabled=True, narration_max_candidates=1,
    )
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    rows = local_persistence.list_candidates(state.run_id)
    assert len(rows) == 1
    assert rows[0]["title"] == "Matter 0"


def test_unavailable_model_gives_no_candidates(local_persistence, tmp_path):
    client = RaisingModelClient(ModelUnavailable("ep-sonnet", "rate limit 0", permanent=True))
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client, skill_dir=MINI_CANDIDATES_SKILL_DIR,
        ai_proposed_findings_enabled=True,
    )
    state = run_to_narrate_input(h)
    narrate(h.ctx, state)

    assert local_persistence.list_candidates(state.run_id) == []


def test_g10_clean_dataset_makes_no_find_candidates_call_and_gives_no_candidates(local_persistence, tmp_path):
    def _write_clean_data(root: Path) -> None:
        claims = pd.DataFrame(
            [
                {"Employee ID": 1, "Transaction Date": "2026-01-05", "Amount": 100, "Vendor": "VendorA"},
                {"Employee ID": 2, "Transaction Date": "2026-01-10", "Amount": 200, "Vendor": "VendorB"},
            ]
        )
        # T2's `mode: semi` flags MATCHED rows as the exception (an
        # anti_join_gap "linkage check", not a gap check -- see
        # orchestrator/primitives/anti_join_gap.py) -- a clean T2 needs
        # register keys that match NEITHER claim, not the same keys.
        register = pd.DataFrame(
            [
                {"Employee ID": 9, "Transaction Date": "2026-01-20", "Vendor": "VendorZ"},
            ]
        )
        claims.to_csv(root / "claims.csv", index=False)
        register.to_csv(root / "register.csv", index=False)

    responses = happy_responses()
    responses[MARKER_CANDIDATES] = _candidate_response()
    client = DispatchingModelClient(responses)
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client, skill_dir=MINI_CANDIDATES_SKILL_DIR,
        ai_proposed_findings_enabled=True, data_writer=_write_clean_data,
    )
    state = run_to_narrate_input(h)
    assert state.findings == []  # a genuinely clean run: neither rule trigger fired

    result = narrate(h.ctx, state)

    assert local_persistence.list_candidates(state.run_id) == []
    calls = local_persistence.list_llm_calls(state.run_id)
    assert all(c["task"] != "find_candidates" for c in calls)
    assert not any("AI-proposed candidate" in e["message"] for e in result.events)


def test_regenerate_supersedes_the_prior_generations_undecided_candidate(local_persistence, tmp_path):
    responses = happy_responses()
    responses[MARKER_CANDIDATES] = _candidate_response()
    client = DispatchingModelClient(responses)
    h = make_narration_harness(
        local_persistence, tmp_path, model_client=client, skill_dir=MINI_CANDIDATES_SKILL_DIR,
        ai_proposed_findings_enabled=True,
    )
    state = run_to_narrate_input(h)
    state = narrate(h.ctx, state)
    first = local_persistence.list_candidates(state.run_id)
    assert len(first) == 1 and first[0]["candidate_status"] == "candidate"

    import dataclasses

    responses2 = happy_responses()
    responses2[MARKER_CANDIDATES] = _candidate_response()
    h.ctx.model_client = DispatchingModelClient(responses2)
    state = dataclasses.replace(state, options={**state.options, "narration_generation": 1})
    result = narrate(h.ctx, state)

    rows = local_persistence.list_candidates(state.run_id)
    statuses = {r["generation"]: r["candidate_status"] for r in rows}
    assert statuses[0] == "superseded"
    assert statuses[1] == "candidate"
    assert any("superseded" in e["message"] for e in result.events)
