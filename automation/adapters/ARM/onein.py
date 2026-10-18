"""Pure 1in.am adapter. Textless video posts remain quarantined by validation."""
from __future__ import annotations

from datetime import date

from adapters.ARM.common import apply_youtube_only_exclusion, extract_article, wordpress_sitemap_window


class Config:
    source_name = "1in.am"
    country_code = "ARM"
    is_env = False


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    return wordpress_sitemap_window(
        index_url="https://www.1in.am/wp-sitemap.xml",
        domains=("1in.am",),
        start=start,
        end=end,
        evidence=evidence,
        deny=("/tag/", "/author/", "/category/", "/gallery/"),
    )


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        language="hy",
        title_selectors=("h1.single_post_title_standard", "article h1", "h1"),
        body_selectors=(".single-post-content", ".nra.prose", "article"),
        visible_date_selectors=(".post_item_date",),
        prefer_visible_date=True,
        evidence_body_selectors=(".single-post-content", ".nra.prose"),
    )
    apply_youtube_only_exclusion(article)
    return article
