"""Job source fetchers — RemoteOK, Remotive, Greenhouse JSON, Lever, Jobicy, Himalayas, RSS."""

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
# Greenhouse JSON API (replaces broken RSS)
# =============================================================================


def fetch_greenhouse(company_slug: str) -> list[JobModel]:
    """Fetch jobs from Greenhouse public JSON API.

    Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{company}/jobs
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"
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
        data = response.json()

        for item in data.get("jobs", []):
            try:
                title = item.get("title", "")
                job_url = item.get("absolute_url", "")
                if not title or not job_url:
                    continue

                # Description
                desc_html = item.get("content", "")
                description = clean_html(desc_html)

                # Location
                location = None
                loc_data = item.get("location", {})
                if isinstance(loc_data, dict):
                    location = loc_data.get("name", None)

                # Date
                posted_date = None
                updated_at = item.get("updated_at") or item.get("created_at")
                if updated_at:
                    try:
                        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                        posted_date = dt.isoformat()
                    except (ValueError, TypeError):
                        pass

                remote_type = _infer_remote_type(
                    title + " " + description + " " + (location or "")
                )
                employment_type = _infer_employment_type(title + " " + description)
                salary_text, salary_min, salary_max = _extract_salary(description)

                jobs.append(
                    JobModel(
                        title=title,
                        company=company_slug.replace("-", " ").title(),
                        url=job_url,
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
                logger.warning("Failed to parse Greenhouse job (%s): %s", company_slug, e)
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
# Remotive API
# =============================================================================


def fetch_remotive(url: str = "https://remotive.com/api/remote-jobs") -> list[JobModel]:
    """Fetch jobs from Remotive public API."""
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

        for item in data.get("jobs", []):
            try:
                title = item.get("title", "")
                job_url = item.get("url", "")
                company = item.get("company_name", "Unknown")
                if not title or not job_url:
                    continue

                desc_html = item.get("description", "")
                description = clean_html(desc_html)

                # Date
                posted_date = None
                pub_date = item.get("publication_date")
                if pub_date:
                    try:
                        dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                        posted_date = dt.isoformat()
                    except (ValueError, TypeError):
                        pass

                # Salary
                salary_text = item.get("salary", None)
                salary_min, salary_max = None, None
                if salary_text:
                    _, salary_min, salary_max = _extract_salary(salary_text)

                location = item.get("candidate_required_location", "Remote")
                job_type = item.get("job_type", "")
                employment_type = _infer_employment_type(
                    job_type + " " + title
                ) if job_type else _infer_employment_type(title)

                # Tags
                tags = item.get("tags", [])
                if tags and isinstance(tags, list):
                    description += " " + " ".join(tags)

                jobs.append(
                    JobModel(
                        title=title,
                        company=company,
                        url=job_url,
                        source="Remotive",
                        posted_date=posted_date,
                        employment_type=employment_type,
                        remote_type=RemoteType.REMOTE,
                        salary_text=salary_text,
                        salary_min=salary_min,
                        salary_max=salary_max,
                        location=location,
                        description=description,
                        raw_description_html=desc_html[:5000] if desc_html else None,
                    )
                )
            except Exception as e:
                logger.warning("Failed to parse Remotive item: %s", e)
                continue

        logger.info("Fetched %d jobs from Remotive", len(jobs))
    except Exception as e:
        logger.error("Failed to fetch Remotive: %s", e)

    return jobs


# =============================================================================
# Jobicy API
# =============================================================================


def fetch_jobicy(url: str = "https://jobicy.com/api/v2/remote-jobs") -> list[JobModel]:
    """Fetch jobs from Jobicy public remote jobs API."""
    jobs: list[JobModel] = []
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            params={"count": 50},
        )
        response.raise_for_status()
        data = response.json()

        for item in data.get("jobs", []):
            try:
                title = item.get("jobTitle", "")
                job_url = item.get("url", "")
                company = item.get("companyName", "Unknown")
                if not title or not job_url:
                    continue

                description = item.get("jobDescription", "")
                if "<" in description:
                    description = clean_html(description)

                posted_date = None
                pub_date = item.get("pubDate")
                if pub_date:
                    try:
                        dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                        posted_date = dt.isoformat()
                    except (ValueError, TypeError):
                        pass

                location = item.get("jobGeo", "Remote")
                job_type = item.get("jobType", "")

                # Salary
                salary_min = _parse_int(item.get("annualSalaryMin"))
                salary_max = _parse_int(item.get("annualSalaryMax"))
                salary_text = None
                if salary_min or salary_max:
                    currency = item.get("salaryCurrency", "USD")
                    salary_text = f"{currency} {salary_min or '?'}–{salary_max or '?'}"

                jobs.append(
                    JobModel(
                        title=title,
                        company=company,
                        url=job_url,
                        source="Jobicy",
                        posted_date=posted_date,
                        employment_type=_infer_employment_type(job_type + " " + title),
                        remote_type=RemoteType.REMOTE,
                        salary_text=salary_text,
                        salary_min=salary_min,
                        salary_max=salary_max,
                        location=location,
                        description=description,
                    )
                )
            except Exception as e:
                logger.warning("Failed to parse Jobicy item: %s", e)
                continue

        logger.info("Fetched %d jobs from Jobicy", len(jobs))
    except Exception as e:
        logger.error("Failed to fetch Jobicy: %s", e)

    return jobs


# =============================================================================
# Himalayas API
# =============================================================================


def fetch_himalayas(url: str = "https://himalayas.app/jobs/api") -> list[JobModel]:
    """Fetch jobs from Himalayas remote jobs API."""
    jobs: list[JobModel] = []
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            params={"limit": 50},
        )
        response.raise_for_status()
        data = response.json()

        for item in data.get("jobs", []):
            try:
                title = item.get("title", "")
                job_url = item.get("applicationUrl") or item.get("url", "")
                company = item.get("companyName", "Unknown")
                if not title or not job_url:
                    continue

                description = item.get("description", "")
                if "<" in description:
                    description = clean_html(description)

                posted_date = None
                pub_date = item.get("pubDate") or item.get("publishedAt")
                if pub_date:
                    try:
                        dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                        posted_date = dt.isoformat()
                    except (ValueError, TypeError):
                        pass

                location = item.get("location", "Remote")

                # Salary
                salary_min = _parse_int(item.get("minSalary"))
                salary_max = _parse_int(item.get("maxSalary"))
                salary_text = None
                if salary_min or salary_max:
                    salary_text = f"${salary_min or '?'}–${salary_max or '?'}"

                # Tags / categories
                categories = item.get("categories", [])
                if categories and isinstance(categories, list):
                    description += " " + " ".join(categories)

                jobs.append(
                    JobModel(
                        title=title,
                        company=company,
                        url=job_url,
                        source="Himalayas",
                        posted_date=posted_date,
                        employment_type=_infer_employment_type(title + " " + description),
                        remote_type=RemoteType.REMOTE,
                        salary_text=salary_text,
                        salary_min=salary_min,
                        salary_max=salary_max,
                        location=location,
                        description=description,
                    )
                )
            except Exception as e:
                logger.warning("Failed to parse Himalayas item: %s", e)
                continue

        logger.info("Fetched %d jobs from Himalayas", len(jobs))
    except Exception as e:
        logger.error("Failed to fetch Himalayas: %s", e)

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

            elif source_type == "remotive":
                url = source.get("url", "https://remotive.com/api/remote-jobs")
                jobs = fetch_remotive(url)

            elif source_type == "jobicy":
                url = source.get("url", "https://jobicy.com/api/v2/remote-jobs")
                jobs = fetch_jobicy(url)

            elif source_type == "himalayas":
                url = source.get("url", "https://himalayas.app/jobs/api")
                jobs = fetch_himalayas(url)

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
