"""Company reputation checker using SearXNG (optional, heuristic-based)."""

from __future__ import annotations

import logging
import re

from src.tools.searx_tool import search_searxng

logger = logging.getLogger(__name__)

# Well-known tech companies (basic allowlist)
KNOWN_COMPANIES = {
    "google", "meta", "apple", "amazon", "microsoft", "netflix", "openai",
    "anthropic", "nvidia", "tesla", "stripe", "figma", "airbnb", "uber",
    "lyft", "coinbase", "databricks", "snowflake", "datadog", "cloudflare",
    "twilio", "atlassian", "shopify", "salesforce", "adobe", "oracle",
    "ibm", "intel", "amd", "qualcomm", "broadcom", "palantir", "spotify",
    "twitter", "x", "reddit", "discord", "slack", "zoom", "doordash",
    "instacart", "robinhood", "plaid", "square", "block", "paypal",
    "linkedin", "github", "gitlab", "hashicorp", "elastic", "mongodb",
    "vercel", "supabase", "hugging face", "huggingface", "cohere",
    "deepmind", "stability ai", "midjourney", "notion", "linear",
    "anyscale", "langchain", "mistral", "together ai",
}

# Signals that indicate funding / established company
FUNDING_SIGNALS = [
    "series a", "series b", "series c", "series d", "series e",
    "ipo", "publicly traded", "nasdaq", "nyse", "fortune 500",
    "raised", "funding", "valuation", "unicorn", "billion",
]


def check_reputation(
    company: str,
    searxng_url: str = "http://localhost:8888",
    enabled: bool = True,
) -> dict:
    """Check company reputation using allowlist and optional SearXNG search.

    Returns:
        Dict with 'reputation_score' (0-10) and 'evidence' (list of strings).
    """
    result = {"reputation_score": 5, "evidence": []}
    company_lower = company.lower().strip()

    # 1. Check allowlist
    if company_lower in KNOWN_COMPANIES:
        result["reputation_score"] = 9
        result["evidence"].append(f"{company} is a well-known tech company")
        return result

    # Partial match (e.g., "Google DeepMind" → "google")
    for known in KNOWN_COMPANIES:
        if known in company_lower or company_lower in known:
            result["reputation_score"] = 8
            result["evidence"].append(f"{company} appears related to known company '{known}'")
            return result

    if not enabled:
        result["evidence"].append("SearXNG disabled — using neutral score")
        return result

    # 2. SearXNG search for company signals
    try:
        queries = [
            f"{company} company funding",
            f"{company} careers hiring",
        ]

        all_evidence = []
        funding_hits = 0

        for query in queries:
            results = search_searxng(query, searxng_url=searxng_url, max_results=3)
            for r in results:
                snippet = r.get("content", "").lower()
                title = r.get("title", "").lower()
                combined = snippet + " " + title

                for signal in FUNDING_SIGNALS:
                    if signal in combined:
                        funding_hits += 1
                        all_evidence.append(
                            f"Signal '{signal}' found: {r.get('title', '')[:80]}"
                        )
                        break

        # Score based on funding signals found
        if funding_hits >= 3:
            result["reputation_score"] = 8
        elif funding_hits >= 1:
            result["reputation_score"] = 6
        else:
            result["reputation_score"] = 4
            all_evidence.append("No strong reputation signals found")

        result["evidence"] = all_evidence[:5]  # Limit evidence items

    except Exception as e:
        logger.warning("Reputation check failed for %s: %s", company, e)
        result["evidence"].append(f"Reputation check failed: {e}")

    return result
