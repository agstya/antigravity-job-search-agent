"""Job source fetchers — RemoteOK API, RSS feeds, Greenhouse, Lever."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta

import feedparser
import httpx
import yaml

from src.models.job import JobModel, RemoteType, EmploymentType
from src.tools.html_cleaner import clean_html

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
USER_AGENT = "JobSearchAgent/1.0 (https://github.com/local; bot)"


# =============================================================================
# Source config loading
# =============================================================================


def load_sources(filepath: str = "sources.yaml") -> list[dict]:
    """Load and return enabled sources from sources.yaml."""
    with open(filepath) as f:
        data = yaml.safe_load(f)
    sources = data.get("sources", [])
    enabled = [s for s in sources if s.get("enabled", False)]
    logger.info("Loaded %d enabled sources out of %d total", len(enabled), len(sources))
    return enabled


# =============================================================================
# RemoteOK API
# =============================================================================


def fetch_remoteok(url: str = "https://remoteok.com/api") -> list[JobModel]:
    """Fetch jobs from RemoteOK JSON API."""
    jobs: list[JobModel] = []
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()

        # First element is metadata, skip it
        for item in data[1:] if isinstance(data, list) and len(data) > 1 else []:
            try:
                title = item.get("position", "") or item.get("title", "")
                company = item.get("company", "Unknown")
                job_url = item.get("url", "")
                if not job_url and item.get("slug"):
                    job_url = f"https://remoteok.com/remote-jobs/{item['slug']}"
                if not job_url or not title:
                    continue

                description_html = item.get("description", "")
                description = clean_html(description_html)

                # Parse date
                posted_date = None
                date_str = item.get("date")
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        posted_date = dt.isoformat()
                    except (ValueError, TypeError):
                        posted_date = None

                # Parse salary
                salary_text = None
                salary_min = None
                salary_max = None
                if item.get("salary_min"):
                    salary_min = _parse_int(item["salary_min"])
                if item.get("salary_max"):
                    salary_max = _parse_int(item["salary_max"])
                if salary_min or salary_max:
                    salary_text = f"${salary_min or '?'}–${salary_max or '?'}"

                # Tags for keyword matching
                tags = item.get("tags", [])
                if tags and isinstance(tags, list):
                    description += " " + " ".join(tags)

                location = item.get("location", "Remote")

                jobs.append(
                    JobModel(
                        title=title,
                        company=company,
                        url=job_url,
                        source="RemoteOK",
                        posted_date=posted_date,
                        employment_type=EmploymentType.FULL_TIME,
                        remote_type=RemoteType.REMOTE,
                        salary_text=salary_text,
                        salary_min=salary_min,
                        salary_max=salary_max,
                        location=location,
                        description=description,
                        raw_description_html=description_html[:5000] if description_html else None,
                    )
                )
            except Exception as e:
                logger.warning("Failed to parse RemoteOK item: %s", e)
                continue

        logger.info("Fetched %d jobs from RemoteOK", len(jobs))
    except Exception as e:
        logger.error("Failed to fetch RemoteOK: %s", e)

    return jobs


# =============================================================================
# Generic RSS Feed
# =============================================================================


def fetch_rss(url: str, source_name: str = "RSS") -> list[JobModel]:
    """Fetch jobs from a generic RSS feed using feedparser."""
    jobs: list[JobModel] = []
    try:
        # feedparser can handle URLs directly but we fetch ourselves for timeout control
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
        feed = feedparser.parse(response.text)

        for entry in feed.entries:
            try:
                title = entry.get("title", "")
                link = entry.get("link", "")
                if not title or not link:
                    continue

                # Try to extract company from title (common pattern: "Title at Company")
                company = "Unknown"
                title_clean = title
                for sep in [" at ", " @ ", " - ", " — ", " | "]:
                    if sep in title:
                        parts = title.split(sep, 1)
                        title_clean = parts[0].strip()
                        company = parts[1].strip()
                        break

                # Description
                desc_html = entry.get("summary", "") or entry.get("description", "")
                description = clean_html(desc_html)

                # Date
                posted_date = None
                if entry.get("published_parsed"):
                    try:
                        from time import mktime
                        dt = datetime.fromtimestamp(
                            mktime(entry.published_parsed), tz=timezone.utc
                        )
                        posted_date = dt.isoformat()
                    except Exception:
                        pass
                elif entry.get("published"):
                    posted_date = entry["published"]

                # Infer remote type from content
                remote_type = _infer_remote_type(title + " " + description)
                employment_type = _infer_employment_type(title + " " + description)

                # Parse salary from description
                salary_text, salary_min, salary_max = _extract_salary(
                    title + " " + description
                )

                jobs.append(
                    JobModel(
                        title=title_clean,
                        company=company,
                        url=link,
                        source=source_name,
                        posted_date=posted_date,
                        employment_type=employment_type,
                        remote_type=remote_type,
                        salary_text=salary_text,
                        salary_min=salary_min,
                        salary_max=salary_max,
                        description=description,
                        raw_description_html=desc_html[:5000] if desc_html else None,
                    )
                )
            except Exception as e:
                logger.warning("Failed to parse RSS entry from %s: %s", source_name, e)
                continue

        logger.info("Fetched %d jobs from %s", len(jobs), source_name)
    except Exception as e:
        logger.error("Failed to fetch RSS from %s (%s): %s", source_name, url, e)

    return jobs


# =============================================================================
# Greenhouse RSS
# =============================================================================


def fetch_greenhouse(company_slug: str) -> list[JobModel]:
    """Fetch jobs from a Greenhouse company job board RSS feed."""
    url = f"https://boards.greenhouse.io/{company_slug}.rss"
    source_name = f"Greenhouse ({company_slug})"

    jobs: list[JobModel] = []
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
        feed = feedparser.parse(response.text)

        for entry in feed.entries:
            try:
                title = entry.get("title", "")
                link = entry.get("link", "")
                if not title or not link:
                    continue

                desc_html = entry.get("summary", "") or entry.get("description", "")
                description = clean_html(desc_html)

                # Location from content or title
                location = entry.get("location", None)

                # Date
                posted_date = None
                if entry.get("published_parsed"):
                    try:
                        from time import mktime
                        dt = datetime.fromtimestamp(
                            mktime(entry.published_parsed), tz=timezone.utc
                        )
                        posted_date = dt.isoformat()
                    except Exception:
                        pass

                remote_type = _infer_remote_type(title + " " + description)
                employment_type = _infer_employment_type(title + " " + description)
                salary_text, salary_min, salary_max = _extract_salary(description)

                jobs.append(
                    JobModel(
                        title=title,
                        company=company_slug.replace("-", " ").title(),
                        url=link,
                        source=source_name,
                        posted_date=posted_date,
                        employment_type=employment_type,
                        remote_type=remote_type,
                        salary_text=salary_text,
                        salary_min=salary_min,
                        salary_max=salary_max,
                        location=location,
                        description=description,
                        raw_description_html=desc_html[:5000] if desc_html else None,
                    )
                )
            except Exception as e:
                logger.warning("Failed to parse Greenhouse entry (%s): %s", company_slug, e)
                continue

        logger.info("Fetched %d jobs from Greenhouse (%s)", len(jobs), company_slug)
    except Exception as e:
        logger.error("Failed to fetch Greenhouse (%s): %s", company_slug, e)

    return jobs


# =============================================================================
# Lever RSS
# =============================================================================


def fetch_lever(company_slug: str) -> list[JobModel]:
    """Fetch jobs from a Lever company job board RSS feed."""
    url = f"https://jobs.lever.co/{company_slug}?format=rss"
    source_name = f"Lever ({company_slug})"

    jobs: list[JobModel] = []
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
        feed = feedparser.parse(response.text)

        for entry in feed.entries:
            try:
                title = entry.get("title", "")
                link = entry.get("link", "")
                if not title or not link:
                    continue

                desc_html = entry.get("summary", "") or entry.get("description", "")
                description = clean_html(desc_html)

                # Try to extract location from title
                location = None
                title_clean = title
                if " – " in title:
                    parts = title.rsplit(" – ", 1)
                    title_clean = parts[0].strip()
                    location = parts[1].strip()

                # Date
                posted_date = None
                if entry.get("published_parsed"):
                    try:
                        from time import mktime
                        dt = datetime.fromtimestamp(
                            mktime(entry.published_parsed), tz=timezone.utc
                        )
                        posted_date = dt.isoformat()
                    except Exception:
                        pass

                remote_type = _infer_remote_type(
                    title + " " + description + " " + (location or "")
                )
                employment_type = _infer_employment_type(title + " " + description)
                salary_text, salary_min, salary_max = _extract_salary(description)

                jobs.append(
                    JobModel(
                        title=title_clean,
                        company=company_slug.replace("-", " ").title(),
                        url=link,
                        source=source_name,
                        posted_date=posted_date,
                        employment_type=employment_type,
                        remote_type=remote_type,
                        salary_text=salary_text,
                        salary_min=salary_min,
                        salary_max=salary_max,
                        location=location,
                        description=description,
                        raw_description_html=desc_html[:5000] if desc_html else None,
                    )
                )
            except Exception as e:
                logger.warning("Failed to parse Lever entry (%s): %s", company_slug, e)
                continue

        logger.info("Fetched %d jobs from Lever (%s)", len(jobs), company_slug)
    except Exception as e:
        logger.error("Failed to fetch Lever (%s): %s", company_slug, e)

    return jobs


# =============================================================================
# Orchestrator
# =============================================================================


def fetch_all_sources(sources_config: list[dict]) -> list[JobModel]:
    """Fetch jobs from all enabled sources. Handles errors per-source gracefully."""
    all_jobs: list[JobModel] = []

    for source in sources_config:
        source_type = source.get("type", "")
        name = source.get("name", source_type)

        try:
            if source_type == "remoteok_api":
                url = source.get("url", "https://remoteok.com/api")
                jobs = fetch_remoteok(url)

            elif source_type == "rss":
                url = source.get("url", "")
                if not url:
                    logger.warning("RSS source '%s' has no URL — skipping", name)
                    continue
                jobs = fetch_rss(url, source_name=name)

            elif source_type == "greenhouse":
                slug = source.get("company_slug", "")
                if not slug:
                    logger.warning("Greenhouse source '%s' has no company_slug — skipping", name)
                    continue
                jobs = fetch_greenhouse(slug)

            elif source_type == "lever":
                slug = source.get("company_slug", "")
                if not slug:
                    logger.warning("Lever source '%s' has no company_slug — skipping", name)
                    continue
                jobs = fetch_lever(slug)

            else:
                logger.warning("Unknown source type '%s' for '%s' — skipping", source_type, name)
                continue

            all_jobs.extend(jobs)

        except Exception as e:
            logger.error("Error fetching source '%s': %s — continuing", name, e)
            continue

    logger.info("Total jobs fetched from all sources: %d", len(all_jobs))
    return all_jobs


# =============================================================================
# Helper functions
# =============================================================================


def _parse_int(value) -> int | None:
    """Safely parse an integer from various formats."""
    if value is None:
        return None
    try:
        # Handle string numbers with commas, dollar signs, etc.
        cleaned = str(value).replace(",", "").replace("$", "").replace("k", "000").strip()
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def _infer_remote_type(text: str) -> RemoteType:
    """Infer remote work type from text content."""
    text_lower = text.lower()
    if any(term in text_lower for term in ["fully remote", "100% remote", "remote only", "anywhere"]):
        return RemoteType.REMOTE
    if "hybrid" in text_lower:
        return RemoteType.HYBRID
    if any(term in text_lower for term in ["on-site", "onsite", "in-office", "in office"]):
        return RemoteType.ONSITE
    if "remote" in text_lower:
        return RemoteType.REMOTE
    return RemoteType.UNKNOWN


def _infer_employment_type(text: str) -> EmploymentType:
    """Infer employment type from text content."""
    text_lower = text.lower()
    if any(term in text_lower for term in ["contract", "contractor", "1099", "freelance"]):
        return EmploymentType.CONTRACT
    if any(term in text_lower for term in ["part-time", "part time"]):
        return EmploymentType.PART_TIME
    if "hourly" in text_lower:
        return EmploymentType.HOURLY
    if any(term in text_lower for term in ["intern", "internship"]):
        return EmploymentType.INTERNSHIP
    if any(term in text_lower for term in ["full-time", "full time", "fte"]):
        return EmploymentType.FULL_TIME
    return EmploymentType.UNKNOWN


def _extract_salary(text: str) -> tuple[str | None, int | None, int | None]:
    """Extract salary information from text.

    Returns:
        (salary_text, salary_min, salary_max)
    """
    if not text:
        return None, None, None

    # Pattern: $XXX,XXX - $XXX,XXX  or  $XXXk - $XXXk
    patterns = [
        r"\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*[–\-—to]+\s*\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)",
        r"\$(\d+\.?\d*)[kK]\s*[–\-—to]+\s*\$(\d+\.?\d*)[kK]",
        r"(\d{1,3}(?:,\d{3})+)\s*[–\-—to]+\s*(\d{1,3}(?:,\d{3})+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            raw_min = match.group(1).replace(",", "")
            raw_max = match.group(2).replace(",", "")

            # Handle 'k' suffix
            if "k" in pattern.lower():
                salary_min = int(float(raw_min) * 1000)
                salary_max = int(float(raw_max) * 1000)
            else:
                salary_min = int(float(raw_min))
                salary_max = int(float(raw_max))

            salary_text = match.group(0)
            return salary_text, salary_min, salary_max

    # Single salary mention
    single = re.search(r"\$(\d{1,3}(?:,\d{3})*)", text)
    if single:
        val = int(single.group(1).replace(",", ""))
        if val > 10000:  # Likely annual salary
            return single.group(0), val, None

    return None, None, None
