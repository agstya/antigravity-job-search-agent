"""SQLite storage for job history, deduplication, and run metadata."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.models.job import JobModel

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    dedupe_key    TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    source        TEXT,
    posted_date   TEXT,
    salary_text   TEXT,
    salary_min    INTEGER,
    salary_max    INTEGER,
    location      TEXT,
    remote_type   TEXT,
    employment_type TEXT,
    description   TEXT,
    llm_score     INTEGER,
    llm_reasons   TEXT,  -- JSON list
    llm_confidence TEXT,
    is_match      INTEGER DEFAULT 0,
    reputation_score INTEGER,
    flags         TEXT,  -- JSON list
    run_date      TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_run_date ON jobs(run_date);

CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date      TEXT NOT NULL,
    mode          TEXT NOT NULL,
    total_fetched INTEGER DEFAULT 0,
    total_filtered INTEGER DEFAULT 0,
    total_matched INTEGER DEFAULT 0,
    total_emailed INTEGER DEFAULT 0,
    errors        TEXT,  -- JSON list
    duration_secs REAL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class JobRepository:
    """SQLite-backed repository for job storage and deduplication."""

    def __init__(self, db_path: str = "jobs.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- Deduplication queries --------------------------------------------------

    def job_exists_by_url(self, url: str) -> bool:
        """Check if a job with this URL already exists."""
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def job_exists_by_dedupe_key(self, dedupe_key: str) -> bool:
        """Check if a job with this normalized company|title key exists."""
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return row is not None

    def is_duplicate(self, job: JobModel) -> bool:
        """Check both URL and fuzzy dedupe key."""
        return self.job_exists_by_url(job.url) or self.job_exists_by_dedupe_key(
            job.dedupe_key
        )

    # -- Insert -----------------------------------------------------------------

    def insert_job(self, job: JobModel, run_date: str) -> bool:
        """Insert a job into the database. Returns True if inserted, False if duplicate."""
        if self.is_duplicate(job):
            logger.debug("Skipping duplicate job: %s at %s", job.title, job.company)
            return False

        try:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    job_id, url, dedupe_key, title, company, source,
                    posted_date, salary_text, salary_min, salary_max,
                    location, remote_type, employment_type, description,
                    llm_score, llm_reasons, llm_confidence, is_match,
                    reputation_score, flags, run_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.url,
                    job.dedupe_key,
                    job.title,
                    job.company,
                    job.source,
                    job.posted_date,
                    job.salary_text,
                    job.salary_min,
                    job.salary_max,
                    job.location,
                    job.remote_type.value,
                    job.employment_type.value,
                    job.description[:5000] if job.description else None,
                    job.llm_score,
                    json.dumps(job.llm_reasons),
                    job.llm_confidence,
                    int(job.is_match),
                    job.reputation_score,
                    json.dumps(job.flags),
                    run_date,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            logger.debug("IntegrityError inserting job %s — already exists", job.job_id)
            return False

    def insert_jobs(self, jobs: list[JobModel], run_date: str) -> int:
        """Insert multiple jobs. Returns count of newly inserted jobs."""
        inserted = 0
        for job in jobs:
            if self.insert_job(job, run_date):
                inserted += 1
        return inserted

    # -- Run logging ------------------------------------------------------------

    def log_run(
        self,
        run_date: str,
        mode: str,
        total_fetched: int = 0,
        total_filtered: int = 0,
        total_matched: int = 0,
        total_emailed: int = 0,
        errors: list[str] | None = None,
        duration_secs: float | None = None,
    ) -> None:
        """Log a pipeline run."""
        self._conn.execute(
            """
            INSERT INTO runs (run_date, mode, total_fetched, total_filtered,
                              total_matched, total_emailed, errors, duration_secs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_date,
                mode,
                total_fetched,
                total_filtered,
                total_matched,
                total_emailed,
                json.dumps(errors or []),
                duration_secs,
            ),
        )
        self._conn.commit()

    # -- Queries ----------------------------------------------------------------

    def get_all_job_urls(self) -> set[str]:
        """Get all stored job URLs for fast dedup lookup."""
        rows = self._conn.execute("SELECT url FROM jobs").fetchall()
        return {row["url"] for row in rows}

    def get_jobs_by_run_date(self, run_date: str) -> list[dict]:
        """Get all jobs from a specific run."""
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE run_date = ? ORDER BY llm_score DESC",
            (run_date,),
        ).fetchall()
        return [dict(row) for row in rows]
