"""Environmental adapter for Glas Podrinja's agriculture category.

The publisher intermittently returns hosting-provider 403/429 pages. The
adapter retains the publisher's current named category pagination, records an
explicit blocked probe when access cannot be established, and parses Serbian
day-first dates explicitly to prevent the historic June 25 -> December 25
inversion.
"""
from __future__ import annotations

from datetime import date

from adapters.SRB.common import AccessBlockedError, discover_category_pages, extract_article


class Config:
    source_name = "glaspodrinja.rs"
    country_code = "SRB"
    is_env = True


BASE = "https://www.glaspodrinja.rs/vesti/rubrika/poljoprivreda/poljoprivreda/"
LEGACY_PROBE = "https://www.glaspodrinja.rs/vesti/rubrika/poljoprivreda/873"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    try:
        urls = discover_category_pages(
            base_url=BASE,
            page_url=lambda page: BASE if page == 1 else f"{BASE}{page}/",
            link_selector="h2.gfont a[href], h3 a[href]",
            domains=Config.source_name,
            start=start,
            end=end,
            evidence=evidence,
            card_selector="article, .article, .news-item, li",
            max_pages=250,
            fetch_timeout=15,
            fetch_attempts=1,
        )
    except AccessBlockedError as exc:
        evidence.update({
            "strategy": "access_probe",
            "access_status": "blocked_by_waf",
            "probes": [{"url": BASE, "result": "blocked", "error": str(exc)}],
            "coverage_complete": False,
            "legacy_probe": LEGACY_PROBE,
        })
        return []
    evidence.update({
        "environmental_scope": "publisher agriculture category",
        "legacy_discovery": "numeric archive routes 830-873 were used in June 2026; named pagination is now preferred",
        "date_fix": "explicit DMY parsing prevents 25 June from becoming 25 December",
    })
    return urls


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("h1", "h2.gfont"),
        body_selectors=(".article-content", ".vest-content", ".news-content", ".tekst", "main"),
        visible_date_text_selectors=("span.desk", "time", ".date", ".datum"),
        visible_date_pattern=r"\b\d{1,2}\.\s*(?:\d{1,2}\.|januar(?:a)?|februar(?:a)?|mart(?:a)?|april(?:a)?|maj(?:a)?|jun(?:a)?|jul(?:a)?|avgust(?:a)?|septembar(?:a)?|oktobar(?:a)?|novembar(?:a)?|decembar(?:a)?)\s*20\d{2}\.?(?:\s*(?:u)?\s*\d{1,2}:\d{2})?\b",
        environmental=True,
    )
