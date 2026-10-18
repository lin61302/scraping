"""Observation-only environmental adapter for zela.org."""
from __future__ import annotations

from datetime import date

from adapters.ZWE.common import wordpress_rest_article, wordpress_rest_discovery


class Config:
    source_name = "zela.org"
    country_code = "ZWE"
    is_env = True


LATEST_NEWS_CATEGORY_ID = 51


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence["legacy_category"] = "latest-news"
    return wordpress_rest_discovery(
        Config.source_name, start, end, evidence,
        categories=[LATEST_NEWS_CATEGORY_ID],
    )


def scrape_url(url: str) -> dict:
    article, _ = wordpress_rest_article(url, source=Config.source_name)
    article["environmental"] = True
    return article
