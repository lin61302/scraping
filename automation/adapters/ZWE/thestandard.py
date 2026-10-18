"""Observation-only adapter for The Standard's migrated WordPress site."""
from __future__ import annotations

from datetime import date

from adapters.ZWE.common import wordpress_rest_article, wordpress_rest_discovery


class Config:
    source_name = "thestandard.co.zw"
    country_code = "ZWE"
    is_env = False


# The old category IDs (12, 196, 535, 299) no longer exist. The migration
# created these news, investigation, and citizen reporting categories.
CATEGORY_IDS = [24075, 24083, 24079]


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence["legacy_sections"] = [
        "local-news", "politics", "environment", "africa",
    ]
    evidence["site_migration"] = (
        "Legacy category IDs and /w_m load-more endpoint now return 404. "
        "The live WordPress categories news/investigations/citizen replace them."
    )
    return wordpress_rest_discovery(
        Config.source_name, start, end, evidence,
        categories=CATEGORY_IDS,
    )


def scrape_url(url: str) -> dict:
    article, _ = wordpress_rest_article(url, source=Config.source_name)
    if article.get("content_protected") or article.get("content_truncated"):
        article["maintext"] = None
        article["field_evidence"]["bodies"] = []
        article["parser_error"] = "publisher_login_or_truncated_body"
    return article
