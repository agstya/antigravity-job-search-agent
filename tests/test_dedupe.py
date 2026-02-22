"""Tests for deduplication logic (URL-based, fuzzy, and database)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.models.job import JobModel, RemoteType, EmploymentType
from src.storage.database import JobRepository


def _make_job(
    title: str = "ML Engineer",
    company: str = "Acme Corp",
    url: str = "https://example.com/job/1",
    **kwargs,
) -> JobModel:
    """Helper to create a test job."""
    return JobModel(
        title=title,
        company=company,
        url=url,
        source="test",
        description="Test job description",
        **kwargs,
    )


class TestJobModel:
    """Test suite for JobModel computed fields."""

    def test_job_id_is_stable(self) -> None:
        """Same inputs produce the same job_id."""
        job1 = _make_job()
        job2 = _make_job()
        assert job1.job_id == job2.job_id

    def test_job_id_differs_for_different_urls(self) -> None:
        """Different URLs produce different job_ids."""
        job1 = _make_job(url="https://example.com/job/1")
        job2 = _make_job(url="https://example.com/job/2")
        assert job1.job_id != job2.job_id

    def test_dedupe_key_normalized(self) -> None:
        """Dedupe key strips non-alphanumeric characters."""
        job1 = _make_job(title="ML Engineer", company="Acme Corp")
        job2 = _make_job(title="ml engineer", company="acme corp")
        assert job1.dedupe_key == job2.dedupe_key

    def test_dedupe_key_ignores_special_chars(self) -> None:
        """Dedupe key ignores special characters."""
        job1 = _make_job(title="ML Engineer (Remote)", company="Acme Corp, Inc.")
        job2 = _make_job(title="ML Engineer Remote", company="Acme Corp Inc")
        assert job1.dedupe_key == job2.dedupe_key


class TestDatabaseDedupe:
    """Test suite for SQLite-based deduplication."""

    def _get_repo(self, tmp_path: Path) -> JobRepository:
        db_path = str(tmp_path / "test_jobs.db")
        return JobRepository(db_path)

    def test_insert_and_detect_url_duplicate(self, tmp_path: Path) -> None:
        """Inserting same URL twice should detect duplicate."""
        repo = self._get_repo(tmp_path)

        job = _make_job(url="https://example.com/unique-job")
        assert repo.insert_job(job, "2025-01-01") is True
        assert repo.insert_job(job, "2025-01-01") is False

        repo.close()

    def test_insert_and_detect_fuzzy_duplicate(self, tmp_path: Path) -> None:
        """Inserting same company+title with different URL should detect fuzzy duplicate."""
        repo = self._get_repo(tmp_path)

        job1 = _make_job(
            title="ML Engineer",
            company="Acme Corp",
            url="https://example.com/job/v1",
        )
        job2 = _make_job(
            title="ML Engineer",
            company="Acme Corp",
            url="https://example.com/job/v2",
        )

        assert repo.insert_job(job1, "2025-01-01") is True
        assert repo.is_duplicate(job2) is True

        repo.close()

    def test_different_jobs_not_duplicate(self, tmp_path: Path) -> None:
        """Different jobs should not be flagged as duplicates."""
        repo = self._get_repo(tmp_path)

        job1 = _make_job(
            title="ML Engineer",
            company="Acme Corp",
            url="https://example.com/job/1",
        )
        job2 = _make_job(
            title="Data Scientist",
            company="Other Inc",
            url="https://example.com/job/2",
        )

        assert repo.insert_job(job1, "2025-01-01") is True
        assert repo.is_duplicate(job2) is False

        repo.close()

    def test_get_all_job_urls(self, tmp_path: Path) -> None:
        """Test retrieval of all stored URLs."""
        repo = self._get_repo(tmp_path)

        urls = {"https://example.com/1", "https://example.com/2", "https://example.com/3"}
        for i, url in enumerate(urls):
            repo.insert_job(_make_job(url=url, title=f"Job {i}", company=f"Company {i}"), "2025-01-01")

        stored_urls = repo.get_all_job_urls()
        assert stored_urls == urls

        repo.close()

    def test_run_logging(self, tmp_path: Path) -> None:
        """Test that run metadata is logged."""
        repo = self._get_repo(tmp_path)

        repo.log_run(
            run_date="2025-01-01",
            mode="daily",
            total_fetched=100,
            total_filtered=50,
            total_matched=10,
            total_emailed=8,
            errors=["test error"],
            duration_secs=45.2,
        )

        # Verify run was logged (no crash)
        repo.close()
