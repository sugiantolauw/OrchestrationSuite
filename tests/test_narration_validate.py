"""T-V1 (docs/specs/P6_narration_design.md §11, §12 WP N1): a positive and a
negative case per rule id in §3.3-§3.4, plus the human-edit number rule from
CLAUDE.md §14 Answers Q3 (N-H1, this implementer's own rule id -- the golden
set in §3.6 is model-output-only and does not cover human edits)."""

from __future__ import annotations

from orchestrator.narration.placeholders import PlaceholderEntry
from orchestrator.narration.validate import (
    validate_human_edit,
    validate_id_set,
    validate_prose,
    validate_themes,
)


def _rule_ids(result):
    return result.rule_ids()


TABLE = {
    "missing_receipt_count": PlaceholderEntry("missing_receipt_count", "count", 12),
    "missing_receipt_pct": PlaceholderEntry("missing_receipt_pct", "%", 8.5),
    "missing_receipt_amount": PlaceholderEntry("missing_receipt_amount", "AUD", 6000.0),
    "null_metric": PlaceholderEntry("null_metric", "count", None),
}


# ---------------------------------------------------------------------------
# N-G1: grammar -- {{, format specs, conversions, attribute access.
# ---------------------------------------------------------------------------
def test_n_g1_positive_well_formed_placeholder_is_not_flagged():
    r = validate_prose("{count:missing_receipt_count} claims were reviewed.", TABLE, field="observation")
    assert "N-G1" not in _rule_ids(r)


def test_n_g1_negative_format_spec_is_flagged():
    r = validate_prose("{count:missing_receipt_count:,.0f} claims were reviewed.", TABLE, field="observation")
    assert "N-G1" in _rule_ids(r)


def test_n_g1_negative_double_brace_escaping_is_flagged():
    r = validate_prose("{{missing_receipt_count}} claims were reviewed.", TABLE, field="observation")
    assert "N-G1" in _rule_ids(r)


def test_n_g1_negative_attribute_access_is_flagged():
    r = validate_prose("{count:a.b} claims were reviewed.", TABLE, field="observation")
    assert "N-G1" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-G2: unknown class.
# ---------------------------------------------------------------------------
def test_n_g2_positive_known_class_is_not_flagged():
    r = validate_prose("{money:missing_receipt_amount} was unsupported.", TABLE, field="observation")
    assert "N-G2" not in _rule_ids(r)


def test_n_g2_negative_unknown_class_is_flagged():
    r = validate_prose("{number:missing_receipt_count} claims were reviewed.", TABLE, field="observation")
    assert "N-G2" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-G3: placeholder not in this item's table (the v1 G11 failure mode).
# ---------------------------------------------------------------------------
def test_n_g3_positive_known_name_is_not_flagged():
    r = validate_prose("{count:missing_receipt_count} claims were reviewed.", TABLE, field="observation")
    assert "N-G3" not in _rule_ids(r)


def test_n_g3_negative_another_findings_metric_is_flagged():
    r = validate_prose("{count:months_covered} claims were reviewed.", TABLE, field="observation")
    assert "N-G3" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-U1: declared class != the class of the metric's own unit.
# ---------------------------------------------------------------------------
def test_n_u1_positive_matching_class_is_not_flagged():
    r = validate_prose("{pct:missing_receipt_pct} of claims were missing receipts.", TABLE, field="observation")
    assert "N-U1" not in _rule_ids(r)


def test_n_u1_negative_mismatched_class_is_flagged():
    r = validate_prose("{count:missing_receipt_pct} of claims were missing receipts.", TABLE, field="observation")
    assert "N-U1" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-U2: a money/pct placeholder immediately followed by a count noun.
# ---------------------------------------------------------------------------
def test_n_u2_positive_pct_followed_by_of_is_not_flagged():
    r = validate_prose("{pct:missing_receipt_pct} of claims were missing receipts.", TABLE, field="observation")
    assert "N-U2" not in _rule_ids(r)


def test_n_u2_negative_money_followed_by_claims_is_flagged():
    r = validate_prose("{money:missing_receipt_amount} claims were unsupported.", TABLE, field="observation")
    assert "N-U2" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-V1: the placeholder's value is null.
# ---------------------------------------------------------------------------
def test_n_v1_positive_non_null_value_is_not_flagged():
    r = validate_prose("{count:missing_receipt_count} claims were reviewed.", TABLE, field="observation")
    assert "N-V1" not in _rule_ids(r)


def test_n_v1_negative_null_value_is_flagged():
    r = validate_prose("{count:null_metric} claims were reviewed.", TABLE, field="observation")
    assert "N-V1" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-D1: no numeral survives, except a whole token from the allowed
# identifier set; no currency symbol; no "per cent"/"percent".
# ---------------------------------------------------------------------------
def test_n_d1_positive_placeholders_and_allowed_ids_are_not_flagged():
    r = validate_prose(
        "T4.1 found {count:missing_receipt_count} claims missing receipts.",
        TABLE,
        field="observation",
        allowed_identifiers={"T4.1"},
    )
    assert "N-D1" not in _rule_ids(r)


def test_n_d1_negative_literal_digit_is_flagged():
    r = validate_prose("12 claims were missing receipts.", TABLE, field="observation")
    assert "N-D1" in _rule_ids(r)


def test_n_d1_negative_dollar_sign_is_flagged():
    r = validate_prose("A $ amount was unsupported.", TABLE, field="observation")
    assert "N-D1" in _rule_ids(r)


def test_n_d1_negative_per_cent_is_flagged():
    r = validate_prose("A per cent of claims were missing receipts.", TABLE, field="observation")
    assert "N-D1" in _rule_ids(r)


def test_n_d1_negative_year_is_flagged():
    r = validate_prose("The FY2025 review found issues.", TABLE, field="observation")
    assert "N-D1" in _rule_ids(r)


def test_n_d1_negative_test_id_not_in_this_runs_allowlist_is_flagged():
    r = validate_prose("See T9.9 for detail.", TABLE, field="observation", allowed_identifiers={"T4.1"})
    assert "N-D1" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-D2: number words (bare "one"/"single" are allowed).
# ---------------------------------------------------------------------------
def test_n_d2_positive_bare_one_and_single_are_not_flagged():
    r = validate_prose("A single claim had one exception.", TABLE, field="observation")
    assert "N-D2" not in _rule_ids(r)


def test_n_d2_negative_number_word_is_flagged():
    r = validate_prose("Twelve claims were missing receipts.", TABLE, field="observation")
    assert "N-D2" in _rule_ids(r)


def test_n_d2_negative_ratio_phrase_is_flagged():
    r = validate_prose("Roughly one in ten claims had no receipt.", TABLE, field="observation")
    assert "N-D2" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-D3: ordinal/ranking words -- priority rationale only.
# ---------------------------------------------------------------------------
def test_n_d3_positive_ordinal_word_outside_rationale_is_not_flagged():
    r = validate_prose("This is the highest priority item.", TABLE, field="observation")
    assert "N-D3" not in _rule_ids(r)


def test_n_d3_negative_ordinal_word_in_rationale_is_flagged():
    r = validate_prose("This is the highest priority item.", TABLE, field="rationale")
    assert "N-D3" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-V1/N-C1 setup note: a cited metric missing from the observation (N-C1)
# and a null-valued placeholder used anyway (N-V1) are covered above and
# below; N-C1 itself:
# ---------------------------------------------------------------------------
def test_n_c1_positive_full_coverage_is_not_flagged():
    r = validate_prose(
        "{count:missing_receipt_count} claims totalling {money:missing_receipt_amount} were unsupported.",
        TABLE,
        field="observation",
        require_coverage=True,
        required_placeholders=["missing_receipt_count", "missing_receipt_amount"],
    )
    assert "N-C1" not in _rule_ids(r)


def test_n_c1_negative_missing_cited_metric_is_flagged():
    r = validate_prose(
        "{count:missing_receipt_count} claims were unsupported.",
        TABLE,
        field="observation",
        require_coverage=True,
        required_placeholders=["missing_receipt_count", "missing_receipt_amount"],
    )
    assert "N-C1" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-Q1: vague magnitude words, observation-type fields only.
# ---------------------------------------------------------------------------
def test_n_q1_positive_precise_language_is_not_flagged():
    r = validate_prose(
        "{count:missing_receipt_count} claims were missing receipts.", TABLE, field="observation"
    )
    assert "N-Q1" not in _rule_ids(r)


def test_n_q1_negative_vague_word_is_flagged():
    r = validate_prose("Most claims were missing receipts.", TABLE, field="observation")
    assert "N-Q1" in _rule_ids(r)


def test_n_q1_not_checked_outside_observation_type_fields():
    r = validate_prose("Most claims were missing receipts.", TABLE, field="recommendation")
    assert "N-Q1" not in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-Q2: universal quantifiers, unless the same sentence has a 0%/100% pct
# placeholder.
# ---------------------------------------------------------------------------
def test_n_q2_positive_all_with_100_pct_in_same_sentence_is_not_flagged():
    table = dict(TABLE)
    table["all_pct"] = PlaceholderEntry("all_pct", "%", 100.0)
    r = validate_prose("All claims passed at {pct:all_pct} of the total.", table, field="observation")
    assert "N-Q2" not in _rule_ids(r)


def test_n_q2_negative_all_with_no_pct_in_sentence_is_flagged():
    r = validate_prose("All claims were missing receipts.", TABLE, field="observation")
    assert "N-Q2" in _rule_ids(r)


def test_n_q2_negative_all_with_non_boundary_pct_in_sentence_is_flagged():
    r = validate_prose(
        "All claims were reviewed, at {pct:missing_receipt_pct} exception rate.", TABLE, field="observation"
    )
    assert "N-Q2" in _rule_ids(r)


def test_n_q2_negative_42_0_pct_does_not_satisfy_the_0_or_100_exception():
    # Regression: "42.0%" contains the substring "0%" (the trailing digit of
    # "42.0" immediately before "%"), which an unanchored 0/100 regex would
    # match even though the rendered value is 42.0, not 0. A trailing-zero
    # non-boundary percentage must not silently excuse a universal
    # quantifier.
    table = dict(TABLE)
    table["other_pct"] = PlaceholderEntry("other_pct", "%", 42.0)
    r = validate_prose(
        "All claims were reviewed, at {pct:other_pct} exception rate.", table, field="observation"
    )
    assert "N-Q2" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-S1: intent/causation language.
# ---------------------------------------------------------------------------
def test_n_s1_positive_neutral_language_is_not_flagged():
    r = validate_prose(
        "{count:missing_receipt_count} claims were missing receipts.", TABLE, field="observation"
    )
    assert "N-S1" not in _rule_ids(r)


def test_n_s1_negative_deliberate_is_flagged():
    r = validate_prose("These claims appear to have been deliberately split.", TABLE, field="observation")
    assert "N-S1" in _rule_ids(r)


def test_n_s1_negative_applies_to_recommendation_too():
    r = validate_prose("Investigate potential fraud in this population.", TABLE, field="recommendation")
    assert "N-S1" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-S2: policy assertions.
# ---------------------------------------------------------------------------
def test_n_s2_positive_control_objective_language_is_not_flagged():
    r = validate_prose(
        "This test supports the control objective for receipt documentation.", TABLE, field="observation"
    )
    assert "N-S2" not in _rule_ids(r)


def test_n_s2_negative_policy_requires_is_flagged():
    r = validate_prose("Policy requires a receipt for every claim.", TABLE, field="observation")
    assert "N-S2" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-S4 (G12-lite, independent narration-content review 2026-09-25): a causal
# connective with no hedge word in the same sentence. Positive/negative
# pairs below use the real live sentences the review found (before the
# `findings.yaml` reword) and their hedged rewrites (after), so this is a
# regression test for the actual reported problem, not only a synthetic one.
# ---------------------------------------------------------------------------
def test_n_s4_positive_hedged_result_in_is_not_flagged():
    r = validate_prose(
        "Duplicate claims may result in double reimbursement, representing a potential "
        "financial loss pending confirmation.",
        TABLE, field="observation",
    )
    assert "N-S4" not in _rule_ids(r)


def test_n_s4_positive_hedge_later_in_same_sentence_still_licenses_it():
    r = validate_prose(
        "This may result in a loss, which could indicate a risk of further exposure.",
        TABLE, field="observation",
    )
    assert "N-S4" not in _rule_ids(r)


def test_n_s4_negative_live_duplicate_claims_result_in_direct_financial_loss():
    # T5.2, live workpaper (before reword): "Duplicate claims result in double
    # reimbursement and direct financial loss."
    r = validate_prose(
        "Duplicate claims result in double reimbursement and direct financial loss.",
        TABLE, field="observation",
    )
    assert "N-S4" in _rule_ids(r)


def test_n_s4_negative_live_thereby_increasing_travel_costs():
    # T3.2a, live workpaper (before reword): "...increasing travel costs."
    r = validate_prose(
        "Use of non-preferred suppliers foregoes negotiated corporate rates and volume "
        "discounts, thereby increasing travel costs.",
        TABLE, field="observation",
    )
    assert "N-S4" in _rule_ids(r)


def test_n_s4_negative_bare_leads_to_with_no_hedge_is_flagged():
    r = validate_prose("This pattern leads to a breakdown in control.", TABLE, field="observation")
    assert "N-S4" in _rule_ids(r)


def test_n_s4_positive_interrogative_sentence_is_exempt():
    # A real management question (skills/tne_exco/findings.yaml, T3.1a) and
    # the live fixture's own T2 question (tests/narration_test_support.py)
    # both use a causal connective to ASK, not to ASSERT.
    r = validate_prose(
        "Is there a follow-up process for pre-approved travel that doesn't result in an "
        "expense report?",
        TABLE, field="question",
    )
    assert "N-S4" not in _rule_ids(r)
    r2 = validate_prose("What causes a claim to be missing its register match?", TABLE, field="question")
    assert "N-S4" not in _rule_ids(r2)


def test_n_s4_negative_interrogative_exemption_does_not_hide_a_declarative_sentence_in_the_same_field():
    r = validate_prose(
        "What causes this? This pattern leads to a breakdown in control.",
        TABLE, field="question",
    )
    assert "N-S4" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-S3: code-like text.
# ---------------------------------------------------------------------------
def test_n_s3_positive_ordinary_prose_is_not_flagged():
    r = validate_prose(
        "{count:missing_receipt_count} claims were missing receipts.", TABLE, field="observation"
    )
    assert "N-S3" not in _rule_ids(r)


def test_n_s3_negative_sql_like_text_is_flagged():
    r = validate_prose("SELECT claims WHERE receipt IS NULL", TABLE, field="observation")
    assert "N-S3" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-L1: length caps, per field.
# ---------------------------------------------------------------------------
def test_n_l1_positive_within_cap_is_not_flagged():
    r = validate_prose("A short recommendation.", TABLE, field="recommendation")
    assert "N-L1" not in _rule_ids(r)


def test_n_l1_negative_over_cap_is_flagged():
    r = validate_prose("x" * 601, TABLE, field="recommendation")
    assert "N-L1" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-L2: titles (theme and candidate) contain no placeholders.
# ---------------------------------------------------------------------------
def test_n_l2_positive_plain_title_is_not_flagged():
    r = validate_prose("Missing receipt documentation", TABLE, field="theme_title")
    assert "N-L2" not in _rule_ids(r)


def test_n_l2_negative_title_with_placeholder_is_flagged():
    r = validate_prose("{count:missing_receipt_count} missing receipts", TABLE, field="candidate_title")
    assert "N-L2" in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-X1: structural checks -- ids in the enum, no duplicates, one item per
# expected id; theme membership and review-observation caps.
# ---------------------------------------------------------------------------
def test_n_x1_positive_id_set_matches_expected_is_not_flagged():
    violations = validate_id_set(["T4_1", "T6_1d"], expected_ids=["T4_1", "T6_1d"])
    assert violations == []


def test_n_x1_negative_duplicate_and_missing_id_is_flagged():
    violations = validate_id_set(["T4_1", "T4_1"], expected_ids=["T4_1", "T6_1d"])
    assert violations and all(v.rule_id == "N-X1" for v in violations)


def test_n_x1_negative_unknown_id_is_flagged():
    violations = validate_id_set(["T9_9"], expected_ids=["T4_1"])
    assert violations and all(v.rule_id == "N-X1" for v in violations)


def test_n_x1_positive_themes_within_caps_are_not_flagged():
    themes = [{"finding_keys": ["T4_1"], "review_observations": ["one pattern"]}]
    violations = validate_themes(themes, valid_finding_keys=["T4_1", "T6_1d"])
    assert violations == []


def test_n_x1_negative_finding_in_two_themes_is_flagged():
    themes = [
        {"finding_keys": ["T4_1"], "review_observations": []},
        {"finding_keys": ["T4_1", "T6_1d"], "review_observations": []},
    ]
    violations = validate_themes(themes, valid_finding_keys=["T4_1", "T6_1d"])
    assert violations and all(v.rule_id == "N-X1" for v in violations)


def test_n_x1_negative_too_many_themes_is_flagged():
    themes = [{"finding_keys": [f"T{i}"], "review_observations": []} for i in range(7)]
    violations = validate_themes(themes, valid_finding_keys=[f"T{i}" for i in range(7)])
    assert violations and all(v.rule_id == "N-X1" for v in violations)


def test_n_x1_negative_too_many_review_observations_is_flagged():
    themes = [{"finding_keys": ["T4_1"], "review_observations": ["a", "b", "c", "d"]}]
    violations = validate_themes(themes, valid_finding_keys=["T4_1"])
    assert violations and all(v.rule_id == "N-X1" for v in violations)


# ---------------------------------------------------------------------------
# N-H1 (this implementer's rule id; CLAUDE.md §14 Answers Q3): a human
# edit may type a number, but it must equal the displayed rendering of a
# metric this item cites, and a mismatch names the offending number.
# ---------------------------------------------------------------------------
HUMAN_EDIT_TABLE = {
    "missing_receipt_count": PlaceholderEntry("missing_receipt_count", "count", 1234),
    "missing_receipt_amount": PlaceholderEntry("missing_receipt_amount", "AUD", 6000.0),
}


def test_n_h1_positive_matching_typed_number_is_not_flagged():
    r = validate_human_edit(
        "1,234 claims totalling $6,000.00 were reviewed.", HUMAN_EDIT_TABLE, field="observation"
    )
    assert "N-H1" not in _rule_ids(r)
    assert r.valid is True


def test_n_h1_negative_mismatched_typed_number_is_flagged_with_the_offending_number():
    r = validate_human_edit(
        "1,235 claims totalling $6,000.00 were reviewed.", HUMAN_EDIT_TABLE, field="observation"
    )
    assert "N-H1" in _rule_ids(r)
    mismatches = [v for v in r.violations if v.rule_id == "N-H1"]
    assert len(mismatches) == 1
    assert mismatches[0].text == "1,235"
    assert "1,235" in mismatches[0].message


def test_n_h1_negative_mismatched_money_is_flagged():
    r = validate_human_edit(
        "1,234 claims totalling $6,500.00 were reviewed.", HUMAN_EDIT_TABLE, field="observation"
    )
    mismatches = [v for v in r.violations if v.rule_id == "N-H1"]
    assert len(mismatches) == 1
    assert mismatches[0].text == "$6,500.00"


def test_n_h1_other_rules_still_apply_to_human_edits():
    # Vague-magnitude language and causation language are not exempted for
    # a human edit -- only the literal-digit ban is relaxed.
    r = validate_human_edit(
        "Most claims were deliberately split to stay under the limit.",
        HUMAN_EDIT_TABLE,
        field="observation",
    )
    ids = _rule_ids(r)
    assert "N-Q1" in ids
    assert "N-S1" in ids
    assert "N-D1" not in ids  # the digit ban itself is off for human edits


def test_n_h1_model_origin_still_bans_digits_outright():
    # The default `origin="model"` path is unaffected by N-H1: a model may
    # never type a literal number, matching or not.
    r = validate_prose("1,234 claims were reviewed.", HUMAN_EDIT_TABLE, field="observation")
    assert "N-D1" in _rule_ids(r)
    assert "N-H1" not in _rule_ids(r)


# ---------------------------------------------------------------------------
# N-H1 formatting-variant tolerance (user decision, 2026-09-24, "every
# number an auditor types must equal a figure the finding cites, as
# shown"): thousands separators and a leading "$" on money are optional;
# percent must still carry "%" and must match at the displayed precision.
# ---------------------------------------------------------------------------
PCT_TABLE = {
    "missing_receipt_pct_int": PlaceholderEntry("missing_receipt_pct_int", "%", 15),
    "missing_receipt_pct_float": PlaceholderEntry("missing_receipt_pct_float", "%", 15.0),
    "missing_receipt_pct_frac": PlaceholderEntry("missing_receipt_pct_frac", "%", 87.5),
}


def test_n_h1_positive_no_thousands_separator_matches_a_comma_rendered_count():
    # "1234" = "1,234": the count metric renders as "1,234"; the auditor
    # may type the digits without the thousands comma.
    r = validate_human_edit("1234 claims were reviewed.", HUMAN_EDIT_TABLE, field="observation")
    assert "N-H1" not in _rule_ids(r)


def test_n_h1_positive_dollar_and_comma_variants_of_money_are_interchangeable():
    # "$1,234.56" = "1,234.56": a leading "$" is optional either way, and
    # both forms are accepted for the same rendered money value.
    table = {"amt": PlaceholderEntry("amt", "AUD", 1234.56)}
    with_dollar = validate_human_edit("Paid $1,234.56 in total.", table, field="observation")
    without_dollar = validate_human_edit("Paid 1,234.56 in total.", table, field="observation")
    assert "N-H1" not in _rule_ids(with_dollar)
    assert "N-H1" not in _rule_ids(without_dollar)


def test_n_h1_positive_money_without_comma_or_dollar_matches():
    table = {"amt": PlaceholderEntry("amt", "AUD", 1234.56)}
    r = validate_human_edit("Paid 1234.56 in total.", table, field="observation")
    assert "N-H1" not in _rule_ids(r)


def test_n_h1_negative_mismatched_count_without_comma_is_still_refused():
    r = validate_human_edit("1235 claims were reviewed.", HUMAN_EDIT_TABLE, field="observation")
    mismatches = [v for v in r.violations if v.rule_id == "N-H1"]
    assert len(mismatches) == 1
    assert mismatches[0].text == "1235"


def test_n_h1_positive_bare_int_percent_matches_a_float_rendered_percent():
    # "15%" matches a rendered "15.0%": the underlying value is equal, the
    # difference is only int-vs-float formatting.
    r = validate_human_edit(
        "The rate was 15% against target.", PCT_TABLE, field="observation"
    )
    assert "N-H1" not in _rule_ids(r)


def test_n_h1_positive_percent_with_extra_trailing_zero_matches():
    r = validate_human_edit(
        "The rate was 15.0% against target.", PCT_TABLE, field="observation"
    )
    assert "N-H1" not in _rule_ids(r)


def test_n_h1_positive_percent_fraction_matches_exactly():
    r = validate_human_edit(
        "The rate was 87.5% against target.", PCT_TABLE, field="observation"
    )
    assert "N-H1" not in _rule_ids(r)


def test_n_h1_negative_percent_without_percent_sign_is_refused():
    # "Percent must still carry '%'" -- dropping it must not fall back to a
    # plain-number match even though the digits are otherwise right.
    r = validate_human_edit("The rate was 15 against target.", PCT_TABLE, field="observation")
    mismatches = [v for v in r.violations if v.rule_id == "N-H1"]
    assert len(mismatches) == 1
    assert mismatches[0].text == "15"


def test_n_h1_negative_percent_with_different_value_is_refused():
    # Precision must equal the displayed precision: "15%" only matches a
    # rendered "15.0%" because the VALUE is equal -- a genuinely different
    # value, even one digit off, must still be refused.
    r = validate_human_edit("The rate was 15.3% against target.", PCT_TABLE, field="observation")
    mismatches = [v for v in r.violations if v.rule_id == "N-H1"]
    assert len(mismatches) == 1
    assert mismatches[0].text == "15.3%"


def test_n_h1_negative_percent_truncated_from_a_fraction_is_refused():
    r = validate_human_edit("The rate was 87% against target.", PCT_TABLE, field="observation")
    mismatches = [v for v in r.violations if v.rule_id == "N-H1"]
    assert len(mismatches) == 1
    assert mismatches[0].text == "87%"
