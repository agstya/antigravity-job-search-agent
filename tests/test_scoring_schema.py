"""Tests for LLM scoring output schema validation."""

from __future__ import annotations

import pytest

from src.models.scoring import LLMScoringOutput


class TestScoringSchema:
    """Test suite for LLM scoring output Pydantic validation."""

    def test_valid_scoring_output(self) -> None:
        """Test that valid JSON parses correctly."""
        data = {
            "is_match": True,
            "score": 8,
            "reasons": [
                "Strong AI/ML focus",
                "Senior-level position",
                "Remote-friendly company",
            ],
            "flags": ["missing_salary"],
            "confidence": "high",
        }
        result = LLMScoringOutput(**data)
        assert result.is_match is True
        assert result.score == 8
        assert len(result.reasons) == 3
        assert result.confidence == "high"

    def test_invalid_score_too_high(self) -> None:
        """Test rejection of score > 10."""
        with pytest.raises(Exception):
            LLMScoringOutput(
                is_match=True,
                score=15,
                reasons=["test"],
                flags=[],
                confidence="high",
            )

    def test_invalid_score_too_low(self) -> None:
        """Test rejection of score < 1."""
        with pytest.raises(Exception):
            LLMScoringOutput(
                is_match=False,
                score=0,
                reasons=["test"],
                flags=[],
                confidence="low",
            )

    def test_invalid_confidence_value(self) -> None:
        """Test rejection of invalid confidence value."""
        with pytest.raises(Exception):
            LLMScoringOutput(
                is_match=True,
                score=7,
                reasons=["test"],
                flags=[],
                confidence="super_high",
            )

    def test_reasons_truncated_to_six(self) -> None:
        """Test that reasons are truncated to max 6 items."""
        data = {
            "is_match": True,
            "score": 7,
            "reasons": ["r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8"],
            "flags": [],
            "confidence": "medium",
        }
        result = LLMScoringOutput(**data)
        assert len(result.reasons) <= 6

    def test_minimal_valid_input(self) -> None:
        """Test with minimal required fields."""
        result = LLMScoringOutput(
            is_match=False,
            score=3,
        )
        assert result.is_match is False
        assert result.score == 3
        assert result.reasons == []
        assert result.flags == []
        assert result.confidence == "medium"  # default

    def test_from_json_string(self) -> None:
        """Test parsing from a JSON string (simulating LLM output)."""
        import json

        json_str = '{"is_match": true, "score": 9, "reasons": ["Perfect fit"], "flags": [], "confidence": "high"}'
        data = json.loads(json_str)
        result = LLMScoringOutput(**data)

        assert result.is_match is True
        assert result.score == 9
        assert result.reasons == ["Perfect fit"]

    def test_extra_fields_ignored(self) -> None:
        """Test that extra fields from LLM don't cause errors."""
        data = {
            "is_match": True,
            "score": 7,
            "reasons": ["test"],
            "flags": [],
            "confidence": "medium",
            "extra_field": "should be ignored",
            "another_one": 123,
        }
        # Pydantic v2 ignores extra fields by default
        result = LLMScoringOutput(**data)
        assert result.score == 7
