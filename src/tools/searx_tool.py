"""SearXNG local metasearch wrapper for company reputation checks."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0


def search_searxng(
    query: str,
    searxng_url: str = "http://localhost:8888",
    categories: str = "general",
    max_results: int = 5,
) -> list[dict]:
    """Query local SearXNG instance and return search results.

    Args:
        query: Search query string.
        searxng_url: Base URL of the SearXNG instance.
        categories: Comma-separated search categories.
        max_results: Maximum number of results to return.

    Returns:
        List of result dicts with 'title', 'url', 'content' keys.
    """
    try:
        response = httpx.get(
            f"{searxng_url.rstrip('/')}/search",
            params={
                "q": query,
                "format": "json",
                "categories": categories,
            },
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        results = []
        for result in data.get("results", [])[:max_results]:
            results.append(
                {
                    "title": result.get("title", ""),
                    "url": result.get("url", ""),
                    "content": result.get("content", ""),
                }
            )
        return results

    except httpx.TimeoutException:
        logger.warning("SearXNG request timed out for query: %s", query)
        return []
    except httpx.HTTPStatusError as e:
        logger.warning("SearXNG HTTP error %d for query: %s", e.response.status_code, query)
        return []
    except Exception as e:
        logger.warning("SearXNG search failed for query '%s': %s", query, e)
        return []
