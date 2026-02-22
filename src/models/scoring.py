"""Pydantic model for LLM scoring output — strict JSON schema."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class LLMScoringOutput(BaseModel):
    """Strict schema for LLM-generated job relevance scoring."""

    is_match: bool
    score: int = Field(ge=1, le=10)
    reasons: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "medium"

    @field_validator("reasons")
    @classmethod
    def limit_reasons(cls, v: list[str]) -> list[str]:
        return v[:6]
