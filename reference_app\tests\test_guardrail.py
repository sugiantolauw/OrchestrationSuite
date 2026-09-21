"""Unit tests for Layer 2: Guardrail (Number Verification)."""

import pytest

from src.guardrail import (
    flatten_payload_numbers,
    extract_dollar_amounts,
    extract_percentages,
    extract_counts,
    verify_narrative,
    build_correction_prompt,
)


@pytest.fixture
def sample_payload():
    """Minimal payload for testing."""
    return {
        "total_spend_tor": 1_057_101,
        "claims_combined": {"rows": 3_734, "amount": 1_057_101},
        "months_covered": 16,
        "total_records": 152_921,
        "tests": {
            "T6.1a": {"pct": 82.8, "no_review": 663, "total": 801},
            "T4.2": {"missing": 224, "total": 274, "pct": 82},
        },
        "approver_metrics": [
            {"name": "Shiner, Anthony", "reports": 156, "receipt_viewed_pct": 43.6, "instant_pct": 82.1},
        ],
    }


class TestFlattenPayload:
    def test_extracts_nested_numbers(self, sample_payload):
        numbers = flatten_payload_numbers(sample_payload)
        assert 1_057_101.0 in numbers
        assert 3_734.0 in numbers
        assert 82.8 in numbers
        assert 663.0 in numbers

    def test_handles_empty_payload(self):
        assert flatten_payload_numbers({}) == set()


class TestExtractDollarAmounts:
    def test_extracts_dollar_amounts(self):
        text = "Total spend was $1,057,101 across $590,037 in prepared claims."
        results = extract_dollar_amounts(text)
        assert len(results) == 2
        assert results[0] == ("$1,057,101", 1057101.0)
        assert results[1] == ("$590,037", 590037.0)

    def test_handles_decimals(self):
        text = "Amount of $5,000.50 was flagged."
        results = extract_dollar_amounts(text)
        assert results[0][1] == 5000.50


class TestExtractPercentages:
    def test_extracts_percentages(self):
        text = "Receipt review rate was 82.8% with 43.6% for Shiner."
        results = extract_percentages(text)
        assert len(results) == 2
        assert results[0] == ("82.8%", 82.8)
        assert results[1] == ("43.6%", 43.6)


class TestExtractCounts:
    def test_extracts_counts_with_keywords(self):
        text = "Across 663 reports and 3,734 claims, there were 11 members."
        results = extract_counts(text)
        assert ("663 reports", 663.0) in results
        assert ("3,734 claims", 3734.0) in results
        assert ("11 members", 11.0) in results


class TestVerifyNarrative:
    def test_passes_valid_narrative(self, sample_payload):
        text = (
            "Over the 16-month period, 152,921 records were analysed "
            "covering $1,057,101 in spend across 3,734 claims. "
            "82.8% of reports had no receipt review."
        )
        result = verify_narrative(text, sample_payload)
        assert result["passed"] is True
        assert len(result["failures"]) == 0

    def test_fails_invalid_number(self, sample_payload):
        text = (
            "Total spend was $999,999 across 3,734 claims."
        )
        result = verify_narrative(text, sample_payload)
        assert result["passed"] is False
        assert any("$999,999" in f["display"] for f in result["failures"])

    def test_returns_none_narrative_on_failure(self, sample_payload):
        text = "There were 50,000 reports reviewed."
        result = verify_narrative(text, sample_payload)
        assert result["narrative"] is None


class TestBuildCorrectionPrompt:
    def test_builds_prompt(self):
        failures = [
            {"type": "dollar", "display": "$999,999", "value": 999999.0},
            {"type": "count", "display": "50,000 reports", "value": 50000.0},
        ]
        prompt = build_correction_prompt(failures)
        assert "$999,999" in prompt
        assert "50,000 reports" in prompt
        assert "ONLY numbers from the provided payload" in prompt
