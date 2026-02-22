"""Criteria parser — reads criteria.md and produces a structured CriteriaModel."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from src.models.criteria import CriteriaModel

logger = logging.getLogger(__name__)


def parse_criteria(filepath: str = "criteria.md") -> CriteriaModel:
    """Parse a human-written criteria.md file into a structured CriteriaModel.

    The parser looks for specific headings and extracts key-value pairs
    from bullet points beneath them. It is intentionally lenient.
    """
    path = Path(filepath)
    if not path.exists():
        logger.warning("Criteria file not found at %s — using defaults", filepath)
        return CriteriaModel()

    raw_text = path.read_text(encoding="utf-8")
    logger.info("Loaded criteria from %s (%d chars)", filepath, len(raw_text))

    # Initialize with defaults
    data: dict = {"raw_text": raw_text}

    # Parse boolean flags
    data["fully_remote"] = _parse_bool(raw_text, r"fully\s+remote", default=True)
    data["full_time_only"] = _parse_bool(raw_text, r"full[- ]time\s+only", default=True)
    data["avoid_hourly"] = _parse_bool(raw_text, r"avoid\s+hourly", default=True)
    data["avoid_contract"] = _parse_bool(raw_text, r"avoid\s+contract", default=True)

    # Parse salary
    data["min_salary"] = _parse_number(raw_text, r"[Mm]inimum\s+salary[:\s]+(\d[\d,]*)")
    data["max_salary"] = _parse_number(raw_text, r"[Mm]aximum\s+salary[:\s]+(\d[\d,]*)")

    # Parse keyword lists
    data["keywords"] = _parse_list(raw_text, r"-\s*[Kk]eywords?\s*:\s*(.*)")
    data["seniority"] = _parse_list(raw_text, r"-\s*[Ss]eniority\s*:\s*(.*)")
    data["exclude_keywords"] = _parse_list(
        raw_text, r"-\s*[Ee]xclu(?:de|sion)\s+keywords?\s*:\s*(.*)"
    )

    # Parse numeric fields
    posted = _parse_number(raw_text, r"[Pp]osted\s+within\s+days?[:\s]+(\d+)")
    if posted is not None:
        data["posted_within_days"] = posted

    min_score = _parse_number(raw_text, r"[Mm]inimum\s+LLM\s+score[:\s]+(\d+)")
    if min_score is not None:
        data["min_llm_score"] = min_score

    max_results = _parse_number(
        raw_text, r"[Mm]ax\s+results?\s+per\s+email[:\s]+(\d+)"
    )
    if max_results is not None:
        data["max_results_per_email"] = max_results

    criteria = CriteriaModel(**data)
    logger.info(
        "Parsed criteria: %d keywords, %d seniority levels, salary %s–%s",
        len(criteria.keywords),
        len(criteria.seniority),
        criteria.min_salary,
        criteria.max_salary,
    )
    return criteria


# =============================================================================
# Parsing helpers
# =============================================================================


def _parse_bool(text: str, pattern: str, default: bool = True) -> bool:
    """Look for a pattern followed by yes/no/true/false."""
    match = re.search(
        pattern + r"[:\s]+(yes|no|true|false)", text, re.IGNORECASE
    )
    if match:
        return match.group(1).lower() in ("yes", "true")
    # If the pattern appears at all, assume True
    if re.search(pattern, text, re.IGNORECASE):
        return True
    return default


def _parse_number(text: str, pattern: str) -> int | None:
    """Extract a number following a pattern."""
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _parse_list(text: str, pattern: str) -> list[str]:
    """Extract a comma-separated list following a pattern."""
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return []

    raw = match.group(1).strip()
    # Split on commas, strip whitespace and bullet markers
    items = [item.strip().strip("-").strip() for item in raw.split(",")]
    # Filter empty strings
    return [item for item in items if item]
