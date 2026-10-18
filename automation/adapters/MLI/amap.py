"""Observation-only AMAP adapter using the live Yoast sitemap frontier."""
from __future__ import annotations

from datetime import date

from adapters.MLI.common import extract_article, wordpress_sitemap_window


class Config:
    source_name = "amap.ml"
    country_code = "MLI"
    is_env = False


INDEX = "https://amap.ml/sitemap_index.xml"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    return wordpress_sitemap_window(
        index_url=INDEX,
        domain=Config.source_name,
        start=start,
        end=end,
        evidence=evidence,
    )


def scrape_url(url: str) -> dict:
    article, _ = extract_article(
        url=url,
        domain=Config.source_name,
        title_selectors=("main h1", "h1"),
        body_selectors=("main#content .page-content", "main#content", ".entry-content"),
        parser="amap_visible_h1_published_meta_page_content",
    )
    return article

