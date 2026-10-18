"""Observation-only environmental adapter for spikedmedia.co.zw."""
from __future__ import annotations

from datetime import date

from adapters.ZWE.common import wordpress_rest_article, wordpress_rest_discovery


class Config:
    source_name = "spikedmedia.co.zw"
    country_code = "ZWE"
    is_env = True


AGRICULTURE_CATEGORY_ID = 8


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence["legacy_category"] = "agriculture"
    return wordpress_rest_discovery(
        Config.source_name, start, end, evidence,
        categories=[AGRICULTURE_CATEGORY_ID],
    )


def scrape_url(url: str) -> dict:
    article, _ = wordpress_rest_article(url, source=Config.source_name)
    article["environmental"] = True
    return article
