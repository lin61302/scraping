"""Environmental observation adapter for Sremske Novine agriculture."""
from __future__ import annotations

from datetime import date

from adapters.SRB.common import discover_category_pages, extract_article


class Config:
    source_name = "sremskenovine.co.rs"
    country_code = "SRB"
    is_env = True


BASE = "https://www.sremskenovine.co.rs/category/ekonomija/poljoprivreda/"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    urls = discover_category_pages(
        base_url=BASE,
        page_url=lambda page: BASE if page == 1 else f"{BASE}page/{page}/",
        link_selector="h3 a[href], h4 a[href]",
        domains=Config.source_name,
        start=start,
        end=end,
        evidence=evidence,
        card_selector=".big-news-category, .small-news-element, article, .blog-post",
        max_pages=150,
    )
    evidence["environmental_scope"] = "publisher agriculture category"
    return urls


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("h1.single-post-title", "h1"),
        body_selectors=(".single-post-content", ".entry-content"),
        visible_date_selectors=('meta[property="article:published_time"]', "time[datetime]"),
        evidence_body_selectors=(".single-post-content",),
        environmental=True,
    )
