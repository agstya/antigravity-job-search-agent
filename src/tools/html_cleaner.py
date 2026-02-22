"""HTML cleaning utility for job descriptions."""

from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)


def clean_html(raw_html: str | None) -> str:
    """Strip HTML tags and normalize whitespace from job descriptions.

    Uses BeautifulSoup if available, falls back to regex.
    """
    if not raw_html:
        return ""

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw_html, "html.parser")

        # Remove script and style elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()

        text = soup.get_text(separator=" ")
    except ImportError:
        # Fallback: regex-based tag removal
        text = re.sub(r"<[^>]+>", " ", raw_html)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Decode common HTML entities missed by parser
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")

    return text
