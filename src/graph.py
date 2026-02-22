"""LangGraph workflow — 10-node job search pipeline."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, TypedDict

from langgraph.graph import StateGraph, END

from src.agents.criteria_parser import parse_criteria
from src.agents.reputation import check_reputation
from src.agents.scoring import score_jobs_batch
from src.models.criteria import CriteriaModel
from src.models.job import JobModel, RemoteType, EmploymentType
from src.report.email_sender import send_report_email
from src.report.renderer import render_html, render_markdown, save_report
from src.storage.database import JobRepository
from src.storage.vector_store import VectorStore
from src.tools.sources import fetch_all_sources, load_sources

logger = logging.getLogger(__name__)


# =============================================================================
# Pipeline State
# =============================================================================


class PipelineState(TypedDict, total=False):
    """State passed between nodes in the LangGraph pipeline."""

    # Config
    mode: str  # "daily" or "weekly"
    dry_run: bool
    no_email: bool
    criteria_path: str
    sources_path: str
    run_date: str

    # Data
    criteria: CriteriaModel | None
    sources_config: list[dict]
    raw_jobs: list[JobModel]
    filtered_jobs: list[JobModel]
    scored_jobs: list[JobModel]
    matched_jobs: list[JobModel]
    borderline_jobs: list[JobModel]
    new_jobs: list[JobModel]

    # Stats
    total_fetched: int
    total_filtered: int
    total_scored: int
    total_matched: int
    total_new: int
    errors: list[str]

    # Report
    report_md: str
    report_html: str
    email_sent: bool


# =============================================================================
# Node 1: Load Criteria
# =============================================================================


def load_criteria_node(state: PipelineState) -> dict:
    """Read criteria.md and parse into structured criteria model."""
    logger.info("=== Node 1: Loading Criteria ===")

    criteria_path = state.get("criteria_path", "criteria.md")
    mode = state.get("mode", "daily")

    criteria = parse_criteria(criteria_path)

    # Override posted_within_days based on mode
    if mode == "weekly":
        criteria.posted_within_days = 7
    elif mode == "daily":
        criteria.posted_within_days = 1

    logger.info("Criteria loaded: mode=%s, posted_within=%d days", mode, criteria.posted_within_days)
    return {"criteria": criteria}


# =============================================================================
# Node 2: Load Sources
# =============================================================================


def load_sources_node(state: PipelineState) -> dict:
    """Read sources.yaml and build list of enabled sources."""
    logger.info("=== Node 2: Loading Sources ===")

    sources_path = state.get("sources_path", "sources.yaml")
    sources_config = load_sources(sources_path)

    logger.info("Loaded %d enabled sources", len(sources_config))
    return {"sources_config": sources_config}


# =============================================================================
# Node 3: Fetch Jobs
# =============================================================================


def fetch_jobs_node(state: PipelineState) -> dict:
    """Fetch jobs from all enabled sources."""
    logger.info("=== Node 3: Fetching Jobs ===")

    sources_config = state.get("sources_config", [])
    errors = list(state.get("errors", []))

    try:
        raw_jobs = fetch_all_sources(sources_config)
    except Exception as e:
        logger.error("Fatal error fetching jobs: %s", e)
        errors.append(f"Fetch error: {e}")
        raw_jobs = []

    logger.info("Fetched %d raw jobs", len(raw_jobs))
    return {"raw_jobs": raw_jobs, "total_fetched": len(raw_jobs), "errors": errors}


# =============================================================================
# Node 4: Normalize and Parse Dates
# =============================================================================


def normalize_dates_node(state: PipelineState) -> dict:
    """Normalize date fields to ISO format and clean up job data."""
    logger.info("=== Node 4: Normalizing Dates ===")

    raw_jobs = state.get("raw_jobs", [])
    for job in raw_jobs:
        if job.posted_date:
            job.posted_date = _normalize_date(job.posted_date)

        # Add missing_salary flag
        if not job.salary_text and not job.salary_min:
            if "missing_salary" not in job.flags:
                job.flags.append("missing_salary")

    return {"raw_jobs": raw_jobs}


# =============================================================================
# Node 5: Hard Filter
# =============================================================================


def hard_filter_node(state: PipelineState) -> dict:
    """Apply deterministic filters based on criteria."""
    logger.info("=== Node 5: Hard Filter ===")

    raw_jobs = state.get("raw_jobs", [])
    criteria = state.get("criteria")
    if not criteria:
        logger.error("No criteria available — skipping hard filter")
        return {"filtered_jobs": raw_jobs, "total_filtered": len(raw_jobs)}

    filtered: list[JobModel] = []

    for job in raw_jobs:
        # 1. Remote check
        if criteria.fully_remote and job.remote_type not in (
            RemoteType.REMOTE, RemoteType.UNKNOWN
        ):
            continue

        # 2. Employment type check
        if criteria.avoid_contract and job.employment_type == EmploymentType.CONTRACT:
            continue
        if criteria.avoid_hourly and job.employment_type == EmploymentType.HOURLY:
            continue
        if criteria.full_time_only and job.employment_type in (
            EmploymentType.PART_TIME, EmploymentType.INTERNSHIP
        ):
            continue

        # 3. Date check
        if criteria.posted_within_days and job.posted_date:
            try:
                posted_dt = datetime.fromisoformat(job.posted_date.replace("Z", "+00:00"))
                cutoff = datetime.now(timezone.utc) - timedelta(days=criteria.posted_within_days)
                if posted_dt < cutoff:
                    continue
            except (ValueError, TypeError):
                pass  # Keep jobs with unparseable dates

        # 4. Salary check (if available)
        if job.salary_max and criteria.min_salary:
            if job.salary_max < criteria.min_salary:
                continue
        if job.salary_min and criteria.max_salary:
            if job.salary_min > criteria.max_salary:
                continue

        # 5. Exclude keywords
        if criteria.exclude_keywords:
            text_lower = (job.title + " " + job.description).lower()
            excluded = any(kw.lower() in text_lower for kw in criteria.exclude_keywords)
            if excluded:
                continue

        # 6. Keyword matching (at least min_keyword_matches)
        if criteria.keywords:
            text_lower = (job.title + " " + job.description).lower()
            matches = sum(
                1 for kw in criteria.keywords if kw.lower() in text_lower
            )
            if matches < criteria.min_keyword_matches:
                continue

        job.hard_filter_passed = True
        filtered.append(job)

    logger.info(
        "Hard filter: %d → %d jobs (%d removed)",
        len(raw_jobs), len(filtered), len(raw_jobs) - len(filtered),
    )
    return {"filtered_jobs": filtered, "total_filtered": len(filtered)}


# =============================================================================
# Node 6: Semantic Score (LLM)
# =============================================================================


def semantic_score_node(state: PipelineState) -> dict:
    """Score remaining jobs using local Ollama LLM."""
    logger.info("=== Node 6: Semantic Scoring (LLM) ===")

    filtered_jobs = state.get("filtered_jobs", [])
    criteria = state.get("criteria")
    dry_run = state.get("dry_run", False)

    if not criteria or not filtered_jobs:
        return {"scored_jobs": filtered_jobs, "total_scored": 0}

    if dry_run:
        logger.info("Dry run — skipping LLM scoring, assigning default score of 5")
        for job in filtered_jobs:
            job.llm_score = 5
            job.is_match = True
            job.llm_reasons = ["Dry run — no LLM scoring performed"]
            job.llm_confidence = "low"
        return {
            "scored_jobs": filtered_jobs,
            "matched_jobs": filtered_jobs,
            "borderline_jobs": [],
            "total_scored": len(filtered_jobs),
            "total_matched": len(filtered_jobs),
        }

    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("OLLAMA_MODEL", "llama3")

    scored_jobs = score_jobs_batch(filtered_jobs, criteria, ollama_url, model)

    # Split into matched and borderline
    min_score = criteria.min_llm_score
    matched = [j for j in scored_jobs if j.llm_score is not None and j.llm_score >= min_score]
    borderline = [
        j for j in scored_jobs
        if j.llm_score is not None and j.llm_score == min_score - 1
    ]

    logger.info(
        "Scoring complete: %d matched (score >= %d), %d borderline",
        len(matched), min_score, len(borderline),
    )

    return {
        "scored_jobs": scored_jobs,
        "matched_jobs": matched,
        "borderline_jobs": borderline,
        "total_scored": len(scored_jobs),
        "total_matched": len(matched),
    }


# =============================================================================
# Node 7: Company Reputation Check (Optional)
# =============================================================================


def reputation_check_node(state: PipelineState) -> dict:
    """Check company reputation for matched jobs."""
    logger.info("=== Node 7: Reputation Check ===")

    matched_jobs = state.get("matched_jobs", [])
    searxng_url = os.getenv("SEARXNG_URL", "http://localhost:8888")
    searxng_enabled = os.getenv("SEARXNG_ENABLED", "false").lower() == "true"

    for job in matched_jobs:
        rep = check_reputation(job.company, searxng_url, enabled=searxng_enabled)
        job.reputation_score = rep.get("reputation_score", 5)
        job.reputation_evidence = rep.get("evidence", [])

    return {"matched_jobs": matched_jobs}


# =============================================================================
# Node 8: Deduplicate and Persist
# =============================================================================


def deduplicate_persist_node(state: PipelineState) -> dict:
    """Deduplicate against prior runs and persist new jobs to SQLite + Chroma."""
    logger.info("=== Node 8: Deduplicate & Persist ===")

    matched_jobs = state.get("matched_jobs", [])
    run_date = state.get("run_date", datetime.now().strftime("%Y-%m-%d"))

    db_path = os.getenv("DB_PATH", "jobs.db")
    chroma_path = os.getenv("CHROMA_PATH", "./chroma_db")

    repo = JobRepository(db_path)
    vector_store = VectorStore(chroma_path)

    new_jobs: list[JobModel] = []

    for job in matched_jobs:
        # URL & fuzzy dedupe via SQLite
        if repo.is_duplicate(job):
            logger.debug("Duplicate (SQLite): %s at %s", job.title, job.company)
            continue

        # Semantic dedupe via Chroma
        if job.description and vector_store.is_semantic_duplicate(
            job.title + " " + job.company + " " + job.description[:500]
        ):
            logger.debug("Semantic duplicate (Chroma): %s at %s", job.title, job.company)
            continue

        # Insert new job
        repo.insert_job(job, run_date)
        vector_store.add_job(
            job.job_id,
            job.title + " " + job.company + " " + job.description[:500],
            {"company": job.company, "title": job.title},
        )
        new_jobs.append(job)

    repo.close()

    logger.info(
        "Dedup: %d matched → %d new jobs persisted",
        len(matched_jobs), len(new_jobs),
    )
    return {"new_jobs": new_jobs, "total_new": len(new_jobs)}


# =============================================================================
# Node 9: Generate Report
# =============================================================================


def generate_report_node(state: PipelineState) -> dict:
    """Generate markdown and HTML reports."""
    logger.info("=== Node 9: Generate Report ===")

    new_jobs = state.get("new_jobs", [])
    borderline_jobs = state.get("borderline_jobs", [])
    criteria = state.get("criteria")
    run_date = state.get("run_date", datetime.now().strftime("%Y-%m-%d"))

    # Sort: score desc, reputation desc, date desc
    new_jobs.sort(
        key=lambda j: (
            j.llm_score or 0,
            j.reputation_score or 0,
        ),
        reverse=True,
    )

    # Limit results
    max_results = criteria.max_results_per_email if criteria else 30
    display_jobs = new_jobs[:max_results]

    stats = {
        "run_date": run_date,
        "mode": state.get("mode", "daily"),
        "total_fetched": state.get("total_fetched", 0),
        "total_filtered": state.get("total_filtered", 0),
        "total_matched": state.get("total_matched", 0),
        "total_new": state.get("total_new", 0),
    }

    report_md = render_markdown(display_jobs, borderline_jobs, stats)
    report_html = render_html(display_jobs, borderline_jobs, stats)

    # Save reports
    save_report(report_md, report_html, run_date)

    return {"report_md": report_md, "report_html": report_html}


# =============================================================================
# Node 10: Send Email
# =============================================================================


def send_email_node(state: PipelineState) -> dict:
    """Send report via Gmail SMTP."""
    logger.info("=== Node 10: Send Email ===")

    no_email = state.get("no_email", False)
    dry_run = state.get("dry_run", False)

    if no_email:
        logger.info("Email sending disabled (--no-email)")
        return {"email_sent": False}

    if dry_run:
        logger.info("Dry run — skipping email send")
        return {"email_sent": False}

    report_html = state.get("report_html", "")
    report_md = state.get("report_md", "")
    run_date = state.get("run_date", datetime.now().strftime("%Y-%m-%d"))
    total_new = state.get("total_new", 0)

    if total_new == 0:
        logger.info("No new jobs found — skipping email")
        return {"email_sent": False}

    try:
        gmail_address = os.getenv("GMAIL_ADDRESS", "")
        gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
        recipient = os.getenv("RECIPIENT_EMAIL", gmail_address)

        if not gmail_address or not gmail_password:
            logger.warning("Gmail credentials not configured — skipping email")
            return {"email_sent": False}

        subject = f"Daily Agentic AI Job Matches — {run_date}"
        send_report_email(
            html_body=report_html,
            text_body=report_md,
            subject=subject,
            from_addr=gmail_address,
            to_addr=recipient,
            password=gmail_password,
        )
        logger.info("Email sent to %s", recipient)
        return {"email_sent": True}

    except Exception as e:
        logger.error("Failed to send email: %s", e)
        state_errors = list(state.get("errors", []))
        state_errors.append(f"Email send failed: {e}")
        return {"email_sent": False, "errors": state_errors}


# =============================================================================
# Build the Graph
# =============================================================================


def build_pipeline() -> StateGraph:
    """Build and compile the LangGraph pipeline."""

    graph = StateGraph(PipelineState)

    # Add nodes
    graph.add_node("load_criteria", load_criteria_node)
    graph.add_node("load_sources", load_sources_node)
    graph.add_node("fetch_jobs", fetch_jobs_node)
    graph.add_node("normalize_dates", normalize_dates_node)
    graph.add_node("hard_filter", hard_filter_node)
    graph.add_node("semantic_score", semantic_score_node)
    graph.add_node("reputation_check", reputation_check_node)
    graph.add_node("deduplicate_persist", deduplicate_persist_node)
    graph.add_node("generate_report", generate_report_node)
    graph.add_node("send_email", send_email_node)

    # Linear edges
    graph.set_entry_point("load_criteria")
    graph.add_edge("load_criteria", "load_sources")
    graph.add_edge("load_sources", "fetch_jobs")
    graph.add_edge("fetch_jobs", "normalize_dates")
    graph.add_edge("normalize_dates", "hard_filter")
    graph.add_edge("hard_filter", "semantic_score")
    graph.add_edge("semantic_score", "reputation_check")
    graph.add_edge("reputation_check", "deduplicate_persist")
    graph.add_edge("deduplicate_persist", "generate_report")
    graph.add_edge("generate_report", "send_email")
    graph.add_edge("send_email", END)

    return graph.compile()


# =============================================================================
# Date normalization helper
# =============================================================================


def _normalize_date(date_str: str) -> str | None:
    """Try to normalize a date string to ISO format."""
    if not date_str:
        return None

    # Already ISO format
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.isoformat()
    except (ValueError, TypeError):
        pass

    # Common feed date formats
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",  # RFC 822
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d %b %Y",
        "%B %d, %Y",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue

    logger.debug("Could not parse date: %s", date_str)
    return date_str  # Return original if unparseable
