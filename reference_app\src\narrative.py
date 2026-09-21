"""LLM Findings Generation via Databricks Model Serving.

Sends the full computed evidence payload to the LLM and receives
structured JSON findings back. The LLM analyses the metrics and
decides what findings to produce — we don't prescribe the count
or categories.
"""

import json
import logging
import os
from typing import Any, Dict

try:
    import mlflow.deployments as mlflow_deployments
except Exception:
    mlflow_deployments = None

logger = logging.getLogger(__name__)

LLM_MODEL = os.environ.get("LLM_MODEL_NAME", "databricks-claude-sonnet-4-5")

SYSTEM_PROMPT = """You are an internal audit analyst for Optus.
You receive a structured evidence payload of pre-computed metrics from Travel & Entertainment audit data.
You MUST only use the numbers provided in the evidence payload.
Do NOT calculate, estimate, or infer any numbers.
Every number you cite must appear in the payload — if it is not there, do not use it.

Your tone is professional, objective, and evidence-based.
Use Australian English spelling (e.g., "analysed", "organised", "behaviour").
Do not speculate beyond what the data supports.
Refer to individuals by surname only after first full mention."""

FINDINGS_PROMPT = """Analyse the evidence payload below and produce structured audit findings.

INSTRUCTIONS:
1. Identify ALL audit findings supported by the data — there may be many (10-20+).
2. Each finding must have a test_id matching the T&E audit test framework (T3.x, T4.x, T5.x, T6.x).
3. Assign risk: "High" (red — systemic control failure or material exposure), "Medium" (amber — significant exceptions), "Low" (green — minor or control operating effectively).
4. A finding may appear at MULTIPLE risk levels if the prepared vs approved populations show different severity.
5. Every number in the observation text MUST come directly from the evidence payload.
6. For each metric you cite, include its metric_id in the metrics_cited list.
7. Include 2-4 management questions per finding.
8. Include a recommendation for each finding.

TEST ID FRAMEWORK:
- T3.1a: Pre-approvals not linked to claims
- T3.1b: Travel booked without prior approval
- T3.2a: Preferred supplier compliance (airline, car, accommodation)
- T3.3a: Late/urgent travel bookings (<7 days before departure)
- T3.3b: Entertainment spend per head thresholds
- T4.1: Missing receipts
- T4.2: Missing attendee lists
- T4.3: Personal expense in claims (AI-assessed)
- T4.4: High-value reimbursements (>$5K)
- T5.1: Potential split claims
- T5.2: Duplicate claims (same-report and OOP vs AMEX)
- T6.1a: Approver review sufficiency (receipt viewing, instant approvals)
- T6.1c: Attendee hierarchy validity
- T6.1d: Daily spend limit exceedances

Return ONLY valid JSON matching this schema — no markdown, no commentary:
{{
  "findings": [
    {{
      "test_id": "T6.1a",
      "title": "Approver Review Sufficiency",
      "risk": "High",
      "observation": "83% of expense reports...",
      "recommendation": "Implement mandatory receipt-viewing...",
      "metrics_cited": ["approver_no_receipt_pct", "approver_instant_pct"],
      "management_questions": [
        "How does management satisfy itself that approvals...",
        "What is the escalation path..."
      ]
    }}
  ]
}}

EVIDENCE PAYLOAD:
{payload_json}
"""


def get_llm_client():
    """Get MLflow deployments client for Databricks Model Serving."""
    if mlflow_deployments is None:
        raise RuntimeError("mlflow.deployments is not available in this runtime")
    return mlflow_deployments.get_deploy_client("databricks")


def call_llm(prompt: str, system_prompt: str = SYSTEM_PROMPT, max_tokens: int = 8000) -> str:
    """Call the LLM via mlflow.deployments."""
    client = get_llm_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    try:
        response = client.predict(
            endpoint=LLM_MODEL,
            inputs={
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
        )
        return response["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        raise


def generate_findings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send evidence payload to LLM, get structured findings JSON back."""
    payload_json = json.dumps(payload, indent=2, default=str)
    prompt = FINDINGS_PROMPT.format(payload_json=payload_json)
    raw = call_llm(prompt)

    # Strip markdown fences if the model wraps output
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    result = json.loads(raw)
    if "findings" not in result:
        raise ValueError("LLM response missing 'findings' key")
    logger.info("LLM returned %d findings", len(result["findings"]))
    return result


# ── Narrative generation (for the AI narrative section) ──────────────────

NARRATIVE_PROMPT = """Based on the audit findings and evidence payload below, write:

1. **Executive Summary** (3-4 paragraphs): scope, overall assessment, top findings, positives.
2. **Actionable Insights** grouped into ### High Risk, ### Medium Risk, ### Low Risk bullet points.
3. **Questions for Management** (5-8 pointed, data-driven questions).

RULES:
- Every number must come from the payload or findings — do NOT calculate.
- Use Australian English. Professional audit tone.
- Reference specific ExCo members where the data names them.

Return as JSON:
{{"executive_summary": "...", "insights": "...", "questions": "..."}}

EVIDENCE PAYLOAD:
{payload_json}

FINDINGS:
{findings_json}
"""


def generate_narrative(payload: Dict[str, Any], findings: list) -> Dict[str, str]:
    """Generate executive narrative sections from payload + findings."""
    payload_json = json.dumps(payload, indent=2, default=str)
    findings_json = json.dumps(findings, indent=2, default=str)
    prompt = NARRATIVE_PROMPT.format(payload_json=payload_json, findings_json=findings_json)
    raw = call_llm(prompt, max_tokens=4000)

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    return json.loads(raw)
