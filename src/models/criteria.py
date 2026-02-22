"""Pydantic model for job search criteria parsed from criteria.md."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CriteriaModel(BaseModel):
    """Structured representation of job search criteria."""

    # Work arrangement
    fully_remote: bool = True
    full_time_only: bool = True
    avoid_hourly: bool = True
    avoid_contract: bool = True

    # Compensation
    min_salary: int | None = None
    max_salary: int | None = None

    # Keywords & seniority
    keywords: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)

    # Search window
    posted_within_days: int = 1

    # Scoring & output
    min_llm_score: int = 7
    max_results_per_email: int = 30

    # Minimum keyword matches required in title/description
    min_keyword_matches: int = 1

    # Raw text for LLM prompts
    raw_text: str = ""
