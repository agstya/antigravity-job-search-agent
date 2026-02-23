"""LangGraph workflow — 10-node job search pipeline."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, TypedDict, cast

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

    sources_config = cast(list, state.get("sources_config", []))
    errors = [*cast(list, state.get("errors") or [])]

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

    raw_jobs = cast("list[JobModel]", state.get("raw_jobs", []))
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
    """Pass all jobs through — no hard filtering.

    Keyword match counts are still computed and stored for relevance sorting,
    but no jobs are removed. This maximizes the number of jobs sent to the
    LLM scoring step.
    """
    logger.info("=== Node 5: Soft Annotation (no hard filter) ===")

    raw_jobs = cast(list[JobModel], state.get("raw_jobs", []))
    criteria = cast(CriteriaModel, state.get("criteria"))

    for job in raw_jobs:
        # Compute keyword match count for relevance sorting
        if criteria and criteria.keywords:
            text_lower = (job.title + " " + job.description).lower()
            matches = sum(
                1 for kw in criteria.keywords if kw.lower() in text_lower
            )
            # Store match count in flags for downstream sorting
            job.flags.append(f"keyword_matches:{matches}")

        job.hard_filter_passed = True

    logger.info(
        "Soft annotation: all %d jobs passed through (no filtering)",
        len(raw_jobs),
    )
    return {"filtered_jobs": raw_jobs, "total_filtered": len(raw_jobs)}


# =============================================================================
# Node 6: Semantic Score (LLM)
# =============================================================================


def semantic_score_node(state: PipelineState) -> dict:
    """Score remaining jobs using local Ollama LLM."""
    logger.info("=== Node 6: Semantic Scoring (LLM) ===")

    filtered_jobs = cast(list[JobModel], state.get("filtered_jobs", []))
    criteria = cast(CriteriaModel, state.get("criteria"))
    dry_run = bool(state.get("dry_run", False))

    if not criteria or not filtered_jobs:
        return {"scored_jobs": filtered_jobs, "total_scored": 0}

    if dry_run:
        logger.info("Dry run — skipping LLM scoring, assigning default score of 5")

        # Sort by keyword match count (highest first) to pick the most relevant
        def _kw_count(job: JobModel) -> int:
            for flag in job.flags:
                if flag.startswith("keyword_matches:"):
                    try:
                        return int(flag.split(":")[1])
                    except (ValueError, IndexError):
                        pass
            return 0

        cast(list, filtered_jobs).sort(key=_kw_count, reverse=True)

        # Limit to top 100 most relevant
        top_jobs = cast(list, filtered_jobs)[:100]
        logger.info("Dry run — keeping top %d of %d jobs by keyword relevance", len(top_jobs), len(filtered_jobs))

        for job in top_jobs:
            job.llm_score = 5
            job.is_match = True
            job.llm_reasons = ["Dry run — no LLM scoring performed"]
            job.llm_confidence = "low"
        return {
            "scored_jobs": top_jobs,
            "matched_jobs": top_jobs,
            "borderline_jobs": [],
            "total_scored": len(top_jobs),
            "total_matched": len(top_jobs),
        }

    # Pre-filter to top 100 by keyword relevance before expensive LLM scoring
    def _kw_count_full(job: JobModel) -> int:
        for flag in job.flags:
            if flag.startswith("keyword_matches:"):
                try:
                    return int(flag.split(":")[1])
                except (ValueError, IndexError):
                    pass
        return 0

    cast(list, filtered_jobs).sort(key=_kw_count_full, reverse=True)
    top_candidates = cast(list, filtered_jobs)[:100]
    logger.info(
        "Pre-filtered to top %d of %d jobs by keyword relevance for LLM scoring",
        len(top_candidates), len(filtered_jobs),
    )

    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("OLLAMA_MODEL", "llama3")

    scored_jobs = score_jobs_batch(top_candidates, criteria, ollama_url, model)

    # Split into matched and borderline
    min_score: int = criteria.min_llm_score if criteria else 7
    matched = [j for j in scored_jobs if j.llm_score is not None and j.llm_score >= min_score]
    borderline = [
        j for j in scored_jobs
        if j.llm_score is not None and j.llm_score == min_score - 1
    ]

    # Fallback: if LLM scoring failed for all jobs (or many), treat top candidates as matched
    if not matched and top_candidates:
        logger.warning(
            "LLM scoring produced 0 matches — falling back to top %d keyword-matched jobs",
            len(top_candidates),
        )
        for job in top_candidates:
            # If scoring failed (llm_score is 0 or None) or no match found
            if not job.llm_score or job.llm_score <= 0:
                job.llm_score = 5
                job.is_match = True
                job.llm_reasons = ["LLM scoring unavailable — matched by keyword relevance"]
                job.llm_confidence = "low"
        matched = top_candidates

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

    matched_jobs = cast(list[JobModel], state.get("matched_jobs", []))
    run_date = str(state.get("run_date", datetime.now().strftime("%Y-%m-%d")))

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
    """Generate markdown and HTML reports.

    Always shows top matched jobs — even if they've all been seen in prior runs.
    The 'new' count is tracked in stats but does NOT control what is displayed.
    """
    logger.info("=== Node 9: Generate Report ===")

    # Use ALL matched jobs for the report, not just new ones
    matched_jobs = cast(list[JobModel], state.get("matched_jobs", []))
    scored_jobs = cast(list[JobModel], state.get("scored_jobs", []))
    filtered_jobs = cast(list[JobModel], state.get("filtered_jobs", []))
    new_jobs = cast(list[JobModel], state.get("new_jobs", []))
    borderline_jobs = cast(list[JobModel], state.get("borderline_jobs", []))
    criteria = cast(CriteriaModel, state.get("criteria"))
    run_date = str(state.get("run_date", datetime.now().strftime("%Y-%m-%d")))

    # Cascade fallback: matched → scored → filtered → new
    # Ensures the report NEVER shows zero results if jobs were fetched
    if matched_jobs:
        display_source = cast(list, matched_jobs)
    elif scored_jobs:
        display_source = cast(list, scored_jobs)
    elif filtered_jobs:
        display_source = cast(list, filtered_jobs)[:100]
    else:
        display_source = cast(list, new_jobs)

    # Sort all jobs by date posted (newest first)
    def _date_sort_key(j: JobModel) -> str:
        return j.posted_date or ""

    display_source.sort(key=_date_sort_key, reverse=True)

    # Show all 100 results
    display_jobs = cast(list, display_source)[:100]

    # Split into Remote and Non-Remote sections
    from src.models.job import RemoteType
    remote_jobs = [j for j in display_jobs if j.remote_type == RemoteType.REMOTE]
    non_remote_jobs = [j for j in display_jobs if j.remote_type != RemoteType.REMOTE]

    stats = {
        "run_date": run_date,
        "mode": state.get("mode", "daily"),
        "total_fetched": state.get("total_fetched", 0),
        "total_filtered": state.get("total_filtered", 0),
        "total_matched": state.get("total_matched", 0),
        "total_new": state.get("total_new", 0),
        "total_showing": len(display_jobs),
        "total_remote": len(remote_jobs),
        "total_non_remote": len(non_remote_jobs),
    }

    report_md = render_markdown(remote_jobs, non_remote_jobs, stats)
    report_html = render_html(remote_jobs, non_remote_jobs, stats)

    # Save reports
    save_report(report_md, report_html, run_date)

    return {"report_md": report_md, "report_html": report_html}


# =============================================================================
# Node 10: Send Email
# =============================================================================


def send_email_node(state: PipelineState) -> dict:
    """Send report via Gmail SMTP — always sends, even if no new jobs found."""
    logger.info("=== Node 10: Send Email ===")

    no_email = state.get("no_email", False)

    if no_email:
        logger.info("Email sending disabled (--no-email)")
        return {"email_sent": False}

    report_html = str(state.get("report_html") or "")
    report_md = str(state.get("report_md") or "")
    run_date = str(state.get("run_date") or datetime.now().strftime("%Y-%m-%d"))
    total_new = state.get("total_new")
    if not isinstance(total_new, int): total_new = 0
    total_fetched = state.get("total_fetched")
    if not isinstance(total_fetched, int): total_fetched = 0

    try:
        gmail_address = os.getenv("GMAIL_ADDRESS", "")
        gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
        recipient = os.getenv("RECIPIENT_EMAIL", gmail_address)

        if not gmail_address or not gmail_password:
            logger.warning("Gmail credentials not configured — skipping email")
            return {"email_sent": False}

        total_matched = state.get("total_matched")
        if not isinstance(total_matched, int): total_matched = 0

        if total_new > 0:
            subject = f"🔍 {total_new} New Job Matches — {run_date}"
        elif total_matched > 0:
            subject = f"📋 Daily Report ({total_matched} matches, 0 new) — {run_date}"
        else:
            subject = f"📋 Job Search Report (0 matches) — {run_date}"

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
        state_errors = [*cast(list, state.get("errors") or [])]
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
