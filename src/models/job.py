"""Pydantic model for normalized job listings."""

from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, Field, computed_field


class RemoteType(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    HOURLY = "hourly"
    INTERNSHIP = "internship"
    UNKNOWN = "unknown"


class JobModel(BaseModel):
    """Normalized job listing."""

    title: str
    company: str
    url: str
    source: str
    posted_date: str | None = None  # ISO format or None
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    remote_type: RemoteType = RemoteType.UNKNOWN
    salary_text: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    location: str | None = None
    description: str = ""
    raw_description_html: str | None = None
    flags: list[str] = Field(default_factory=list)
    hard_filter_passed: bool = False

    # Scoring fields (populated after LLM scoring)
    llm_score: int | None = None
    llm_reasons: list[str] = Field(default_factory=list)
    llm_confidence: str | None = None
    is_match: bool = False

    # Reputation fields (populated after reputation check)
    reputation_score: int | None = None
    reputation_evidence: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def job_id(self) -> str:
        """Stable identifier computed from URL + normalized title/company."""
        normalized = f"{self.url}|{self.company.lower().strip()}|{self.title.lower().strip()}"
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dedupe_key(self) -> str:
        """Fuzzy dedupe key from normalized company + title."""
        company = "".join(c for c in self.company.lower() if c.isalnum())
        title = "".join(c for c in self.title.lower() if c.isalnum())
        return f"{company}|{title}"
