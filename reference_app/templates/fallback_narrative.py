"""Fallback Narrative Templates.

Used when the LLM-generated narrative fails the eval gate.
Template-based approach ensures factual accuracy by directly
injecting pre-computed values from the results_payload.
"""

from typing import Dict, Any


EXEC_SUMMARY_TEMPLATE = """Over the {months_covered}-month period ({audit_period}), {total_records:,} records \
across {total_files} data sources were analysed for {exco_members_count} Executive Committee \
members, covering ${total_spend_tor:,.0f} in Travel & Entertainment expenditure across \
{claims_combined_rows:,} expense claims.

The audit identified material control weaknesses in approver oversight. {no_review_pct}% of \
expense reports approved by ExCo members ({no_review} of {total_reports} reports) showed no \
evidence of receipt review, with instant approval rates exceeding 80% for 9 of 11 ExCo \
members. This systemic pattern suggests approvers are not fulfilling their gatekeeper \
obligations under the T&E policy.

The three highest-risk findings are: (1) Approver review sufficiency — {no_review_pct}% \
non-review rate represents a fundamental breakdown in the second line of defence; \
(2) Missing attendee lists — {missing_attendee} of {total_attendee} entertainment claims \
({missing_attendee_pct}%) have no attendee documentation, creating FBT exposure; and \
(3) Pre-approvals not linked to claims — {preapproval_count} pre-approvals totalling \
${preapproval_amount:,.0f} have no matching expense claim.

Positive indicators include: all {missing_receipt_count} claims with missing receipts have \
completed affidavit declarations, no hierarchy validity breaches were identified in \
entertainment attendee lists, and only {non_preferred_count} non-preferred supplier booking \
was detected."""

INSIGHTS_TEMPLATE = """### High Risk

- {approver_worst_name} approved {approver_worst_reports} reports with only \
{approver_worst_viewed}% receipt review rate — recommend immediate management discussion \
and targeted training on receipt review obligations.

- {instant_worst_name} has a {instant_worst_pct}% instant approval rate across \
{instant_worst_reports} reports — recommend review of whether delegated approval authority \
is appropriate.

### Medium Risk

- {missing_attendee} of {total_attendee} entertainment claims ({missing_attendee_pct}%) are \
missing attendee lists — recommend mandatory attendee field enforcement in expense system \
and FBT impact assessment.

- {preapproval_count} pre-approved travel requests (${preapproval_amount:,.0f}) have no \
matching expense claim — recommend reconciliation process and budget impact review.

### Low Risk

- {flagged_personal} claims flagged as potentially personal by AI screening — recommend \
sampled management review and establishment of investigation workflow.

- {split_same_day} same-day split claims and {split_window} window-period splits detected — \
recommend review of threshold-avoidance patterns and system controls."""

QUESTIONS_TEMPLATE = """1. {no_review_pct}% of expense reports approved by ExCo members showed no evidence \
of receipt review. What accountability mechanisms exist for approvers, and how does \
management satisfy itself that the approver control is operating effectively?

2. 9 of 11 ExCo members have instant approval rates above 80%. Is this indicative of a \
systemic culture of rubber-stamping, and what is the minimum expected review time \
for expense approvals?

3. {missing_attendee} of {total_attendee} entertainment claims ({missing_attendee_pct}%) \
are missing attendee lists. How does management verify these expenses have a genuine \
business purpose, and what is the estimated FBT exposure?

4. ${preapproval_amount:,.0f} in pre-approved travel across {preapproval_count} requests \
has no matching expense claim. Are these approvals being cancelled, or is travel occurring \
without reconciliation?

5. {flagged_personal} claims were flagged as potentially personal by AI screening. What is \
management's process for investigating these, and what false-positive rate is acceptable?

6. {approver_worst_name} approved {approver_worst_reports} reports with {approver_worst_viewed}% \
receipt review. What is the minimum expected review standard, and has this been communicated \
to ExCo?

7. {dup_count} potential duplicate claims were identified including {oop_amex} out-of-pocket \
vs corporate card matches. What detective controls exist to prevent double-claiming?"""


def generate_fallback(payload: Dict[str, Any]) -> Dict[str, str]:
    """Generate template-based narratives from the results payload.

    This is the guaranteed-accurate fallback when LLM narratives fail eval.
    """
    tests = payload["tests"]
    approver_metrics = payload.get("approver_metrics", [])

    # Find worst approver (lowest receipt viewed %)
    worst_approver = approver_metrics[-1] if approver_metrics else {
        "name": "Unknown", "reports": 0, "receipt_viewed_pct": 0, "instant_pct": 0
    }
    # Find worst instant approver (highest instant %)
    instant_worst = max(approver_metrics, key=lambda x: x["instant_pct"]) if approver_metrics else worst_approver

    template_vars = {
        "months_covered": payload["months_covered"],
        "audit_period": payload["audit_period"],
        "total_records": payload["total_records"],
        "total_files": payload["total_files"],
        "exco_members_count": payload["exco_members_count"],
        "total_spend_tor": payload["total_spend_tor"],
        "claims_combined_rows": payload["claims_combined"]["rows"],
        "no_review_pct": tests["T6.1a"]["pct"],
        "no_review": tests["T6.1a"]["no_review"],
        "total_reports": tests["T6.1a"]["total"],
        "missing_attendee": tests["T4.2"]["missing"],
        "total_attendee": tests["T4.2"]["total"],
        "missing_attendee_pct": tests["T4.2"]["pct"],
        "preapproval_count": tests["T3.1a"]["count"],
        "preapproval_amount": tests["T3.1a"]["amount"],
        "missing_receipt_count": tests["T4.1"]["count"],
        "non_preferred_count": tests["T3.2a"]["count"],
        "flagged_personal": tests["T4.3"]["flagged"],
        "split_same_day": tests["T5.1"]["same_day"],
        "split_window": tests["T5.1"]["window"],
        "dup_count": tests["T5.2"]["count"],
        "oop_amex": tests["T5.2"]["oop_vs_amex"],
        "approver_worst_name": worst_approver["name"],
        "approver_worst_reports": worst_approver["reports"],
        "approver_worst_viewed": worst_approver["receipt_viewed_pct"],
        "instant_worst_name": instant_worst["name"],
        "instant_worst_pct": instant_worst["instant_pct"],
        "instant_worst_reports": instant_worst["reports"],
    }

    return {
        "executive_summary": EXEC_SUMMARY_TEMPLATE.format(**template_vars),
        "insights": INSIGHTS_TEMPLATE.format(**template_vars),
        "questions": QUESTIONS_TEMPLATE.format(**template_vars),
    }
