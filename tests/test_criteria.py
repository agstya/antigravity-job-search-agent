"""Tests for criteria.md parsing into CriteriaModel."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.agents.criteria_parser import parse_criteria
from src.models.criteria import CriteriaModel


SAMPLE_CRITERIA = """# Job Search Criteria

## Work Arrangement
- Fully remote: yes
- Full-time only: yes
- Avoid hourly: yes
- Avoid contract/1099: yes

## Compensation
- Minimum salary: 150000
- Maximum salary: 300000

## Roles & Keywords
- Keywords: agentic AI, AI agent, LLM, machine learning engineer, ML engineer
- Seniority: senior, staff, principal, lead

## Exclusions
- Exclude keywords: intern, internship, junior, entry level, part-time

## Search Window
- Posted within days: 1

## Scoring
- Minimum LLM score: 7
- Max results per email: 30
"""


class TestCriteriaParsing:
    """Test suite for criteria parsing."""

    def test_parse_full_criteria(self, tmp_path: Path) -> None:
        """Test parsing a complete criteria file."""
        filepath = tmp_path / "criteria.md"
        filepath.write_text(SAMPLE_CRITERIA)

        result = parse_criteria(str(filepath))

        assert isinstance(result, CriteriaModel)
        assert result.fully_remote is True
        assert result.full_time_only is True
        assert result.avoid_hourly is True
        assert result.avoid_contract is True
        assert result.min_salary == 150000
        assert result.max_salary == 300000
        assert len(result.keywords) > 0
        assert "agentic AI" in result.keywords
        assert len(result.seniority) > 0
        assert "senior" in result.seniority
        assert len(result.exclude_keywords) > 0
        assert result.posted_within_days == 1
        assert result.min_llm_score == 7
        assert result.max_results_per_email == 30

    def test_parse_missing_file_returns_defaults(self) -> None:
        """Test that a missing file returns default criteria."""
        result = parse_criteria("/nonexistent/path/criteria.md")

        assert isinstance(result, CriteriaModel)
        assert result.fully_remote is True
        assert result.min_salary is None
        assert result.keywords == []

    def test_parse_minimal_criteria(self, tmp_path: Path) -> None:
        """Test parsing a minimal criteria file."""
        filepath = tmp_path / "minimal.md"
        filepath.write_text("# Criteria\n\nFully remote: yes\nMinimum salary: 100000\n")

        result = parse_criteria(str(filepath))

        assert result.fully_remote is True
        assert result.min_salary == 100000

    def test_raw_text_preserved(self, tmp_path: Path) -> None:
        """Test that raw text is preserved for LLM prompts."""
        filepath = tmp_path / "criteria.md"
        filepath.write_text(SAMPLE_CRITERIA)

        result = parse_criteria(str(filepath))

        assert result.raw_text == SAMPLE_CRITERIA
        assert len(result.raw_text) > 0

    def test_salary_with_commas(self, tmp_path: Path) -> None:
        """Test salary parsing with comma-formatted numbers."""
        filepath = tmp_path / "criteria.md"
        filepath.write_text("Minimum salary: 150,000\nMaximum salary: 300,000\n")

        result = parse_criteria(str(filepath))

        assert result.min_salary == 150000
        assert result.max_salary == 300000

    def test_boolean_no_values(self, tmp_path: Path) -> None:
        """Test parsing boolean 'no' values."""
        filepath = tmp_path / "criteria.md"
        filepath.write_text("Fully remote: no\nFull-time only: no\n")

        result = parse_criteria(str(filepath))

        assert result.fully_remote is False
        assert result.full_time_only is False
