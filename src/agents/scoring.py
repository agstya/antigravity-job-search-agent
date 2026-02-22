"""LLM-based semantic job scoring using Ollama (local)."""

from __future__ import annotations

import json
import logging

from src.models.criteria import CriteriaModel
from src.models.job import JobModel
from src.models.scoring import LLMScoringOutput

logger = logging.getLogger(__name__)

SCORING_PROMPT = """You are a job relevance evaluator. Given the candidate's criteria and a job listing, evaluate how well the job matches.

## Candidate Criteria:
{criteria_text}

## Job Listing:
- **Title**: {title}
- **Company**: {company}
- **Location**: {location}
- **Remote**: {remote_type}
- **Salary**: {salary_text}
- **Posted**: {posted_date}
- **Description**: {description}

## Instructions:
Evaluate the job against the candidate's criteria. Consider:
1. Role relevance (title, keywords, seniority match)
2. Company quality (is it a reputed, established company?)
3. Technical fit (does the description match agentic AI / ML / LLM work?)
4. Compensation alignment (if salary is available)
5. Red flags (contract, hourly, junior-level, etc.)

## Required Output:
Respond ONLY with valid JSON matching this exact schema:
{{
    "is_match": true/false,
    "score": <integer 1-10>,
    "reasons": ["reason1", "reason2", ...],
    "flags": ["flag1", ...],
    "confidence": "low" | "medium" | "high"
}}

Rules:
- score 1-3: poor match
- score 4-6: partial match
- score 7-10: good match
- reasons: max 6 short bullet points explaining why
- flags: note issues like "missing_salary", "unknown_company", "seniority_mismatch"
- confidence: your confidence in the assessment

Respond ONLY with the JSON object. No other text.
"""

REPAIR_PROMPT = """Your previous response was not valid JSON. Please respond with ONLY a valid JSON object matching this schema:
{{
    "is_match": true/false,
    "score": <integer 1-10>,
    "reasons": ["reason1", "reason2", ...],
    "flags": ["flag1", ...],
    "confidence": "low" | "medium" | "high"
}}

Previous response:
{previous_response}

Please fix the JSON and respond with ONLY the corrected JSON object.
"""


def score_job(
    job: JobModel,
    criteria: CriteriaModel,
    ollama_base_url: str = "http://localhost:11434",
    model: str = "llama3",
) -> LLMScoringOutput | None:
    """Score a job using local Ollama LLM.

    Sends a structured prompt, parses JSON output, validates with Pydantic.
    Retries once with a repair prompt on failure.

    Returns:
        LLMScoringOutput if successful, None if all attempts fail.
    """
    prompt = SCORING_PROMPT.format(
        criteria_text=criteria.raw_text or _format_criteria(criteria),
        title=job.title,
        company=job.company,
        location=job.location or "Not specified",
        remote_type=job.remote_type.value,
        salary_text=job.salary_text or "Not specified",
        posted_date=job.posted_date or "Unknown",
        description=job.description[:2000] if job.description else "No description available",
    )

    # First attempt
    response_text = _call_ollama(prompt, ollama_base_url, model)
    if response_text:
        result = _parse_scoring_output(response_text)
        if result:
            return result

        # Repair attempt
        logger.info("First scoring attempt failed for '%s' — retrying with repair prompt", job.title)
        repair_prompt = REPAIR_PROMPT.format(previous_response=response_text[:500])
        repair_response = _call_ollama(repair_prompt, ollama_base_url, model)
        if repair_response:
            result = _parse_scoring_output(repair_response)
            if result:
                return result

    logger.warning("All scoring attempts failed for job: %s at %s", job.title, job.company)
    return None


def score_jobs_batch(
    jobs: list[JobModel],
    criteria: CriteriaModel,
    ollama_base_url: str = "http://localhost:11434",
    model: str = "llama3",
) -> list[JobModel]:
    """Score a batch of jobs, updating each job's scoring fields in place.

    Jobs that fail scoring are marked with 'scoring_failed' flag.
    """
    scored = 0
    failed = 0

    for i, job in enumerate(jobs):
        logger.info(
            "Scoring job %d/%d: '%s' at %s",
            i + 1, len(jobs), job.title, job.company,
        )

        result = score_job(job, criteria, ollama_base_url, model)
        if result:
            job.llm_score = result.score
            job.llm_reasons = result.reasons
            job.llm_confidence = result.confidence
            job.is_match = result.is_match
            job.flags.extend(result.flags)
            scored += 1
        else:
            job.flags.append("scoring_failed")
            job.llm_score = 0
            job.is_match = False
            job.llm_confidence = "low"
            failed += 1

    logger.info("Scoring complete: %d scored, %d failed out of %d total", scored, failed, len(jobs))
    return jobs


# =============================================================================
# Internal helpers
# =============================================================================


def _call_ollama(prompt: str, base_url: str, model: str) -> str | None:
    """Call Ollama API and return raw text response."""
    import httpx

    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 512,
                },
            },
            timeout=120.0,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("response", "")
    except Exception as e:
        logger.error("Ollama API call failed: %s", e)
        return None


def _parse_scoring_output(raw: str) -> LLMScoringOutput | None:
    """Parse and validate LLM response as LLMScoringOutput."""
    try:
        # Try to extract JSON from the response (LLM might add surrounding text)
        json_str = _extract_json(raw)
        if not json_str:
            logger.warning("No JSON found in LLM response")
            return None

        data = json.loads(json_str)
        return LLMScoringOutput(**data)
    except json.JSONDecodeError as e:
        logger.warning("JSON parse error: %s", e)
        return None
    except Exception as e:
        logger.warning("Pydantic validation error: %s", e)
        return None


def _extract_json(text: str) -> str | None:
    """Extract a JSON object from text that may contain surrounding content."""
    # Try the full text first
    text = text.strip()
    if text.startswith("{"):
        # Find the matching closing brace
        depth = 0
        for i, ch in enumerate(text):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[: i + 1]

    # Look for JSON block in markdown code fences
    import re
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return match.group(1)

    # Look for any JSON object
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        return match.group(0)

    return None


def _format_criteria(criteria: CriteriaModel) -> str:
    """Format criteria as readable text for the LLM prompt."""
    parts = []
    if criteria.fully_remote:
        parts.append("Must be fully remote")
    if criteria.full_time_only:
        parts.append("Must be full-time")
    if criteria.avoid_contract:
        parts.append("No contract/1099 roles")
    if criteria.avoid_hourly:
        parts.append("No hourly roles")
    if criteria.min_salary or criteria.max_salary:
        parts.append(f"Salary range: ${criteria.min_salary or '?'}–${criteria.max_salary or '?'}")
    if criteria.keywords:
        parts.append(f"Target keywords: {', '.join(criteria.keywords)}")
    if criteria.seniority:
        parts.append(f"Seniority levels: {', '.join(criteria.seniority)}")
    if criteria.exclude_keywords:
        parts.append(f"Exclude: {', '.join(criteria.exclude_keywords)}")
    return "\n".join(f"- {p}" for p in parts)
