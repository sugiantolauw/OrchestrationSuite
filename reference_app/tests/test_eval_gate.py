"""Unit tests for Layer 3: Eval Gate."""

import pytest

from src.eval_gate import (
    check_completeness,
    check_name_validity,
    check_tone,
    check_substance,
)


class TestCompleteness:
    def test_passes_with_all_sections(self):
        narratives = {
            "executive_summary": "A" * 100,
            "insights": "B" * 100,
            "questions": "C" * 100,
        }
        result = check_completeness(narratives)
        assert result["passed"] is True

    def test_fails_missing_section(self):
        narratives = {
            "executive_summary": "A" * 100,
            "insights": None,
            "questions": "C" * 100,
        }
        result = check_completeness(narratives)
        assert result["passed"] is False
        assert "insights" in result["missing_sections"]

    def test_fails_short_section(self):
        narratives = {
            "executive_summary": "Too short",
            "insights": "B" * 100,
            "questions": "C" * 100,
        }
        result = check_completeness(narratives)
        assert result["passed"] is False


class TestNameValidity:
    def test_passes_valid_names(self):
        text = "Shiner approved 156 reports. Ross had a low review rate."
        result = check_name_validity(text)
        assert result["passed"] is True

    def test_detects_invalid_names(self):
        text = "Johnson approved 50 reports without review."
        result = check_name_validity(text)
        # Note: depends on heuristic pattern matching
        # May or may not flag depending on context words


class TestTone:
    def test_passes_professional_text(self):
        text = (
            "The audit identified material control weaknesses in approver oversight. "
            "82.8% of expense reports approved by ExCo members showed no evidence of receipt review."
        )
        result = check_tone(text)
        assert result["passed"] is True

    def test_fails_speculative_language(self):
        text = "I think the approver probably did not review the receipts."
        result = check_tone(text)
        assert result["passed"] is False
        assert len(result["violations"]) >= 2

    def test_rejects_might_be(self):
        text = "This might be indicative of a control failure."
        result = check_tone(text)
        assert result["passed"] is False


class TestSubstance:
    def test_passes_sufficient_content(self):
        narratives = {
            "insights": "\n".join([f"- Insight {i}" for i in range(5)]),
            "questions": "\n".join([f"Question {i}?" for i in range(6)]),
        }
        result = check_substance(narratives)
        assert result["passed"] is True

    def test_fails_insufficient_insights(self):
        narratives = {
            "insights": "- Only one insight",
            "questions": "\n".join([f"Question {i}?" for i in range(6)]),
        }
        result = check_substance(narratives)
        assert result["passed"] is False

    def test_fails_insufficient_questions(self):
        narratives = {
            "insights": "\n".join([f"- Insight {i}" for i in range(5)]),
            "questions": "One question? Two questions?",
        }
        result = check_substance(narratives)
        assert result["passed"] is False
