"""Environmental adapter for Novosti sustainable-development and agriculture."""
from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

from adapters.SRB.common import discover_category_pages, extract_article


class Config:
    source_name = "novosti.rs"
    country_code = "SRB"
    is_env = True


CATEGORIES = (
    "https://www.novosti.rs/odrzivi-razvoj",
    "https://www.novosti.rs/ekonomija/poljoprivreda",
)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    selected = set()
    category_runs = []
    for base in CATEGORIES:
        category_evidence = {}
        selected.update(discover_category_pages(
            base_url=base,
            page_url=lambda page, base=base: base if page == 1 else f"{base}/page/{page}",
            link_selector="h3.news-item-title a[href]",
            domains=Config.source_name,
            start=start,
            end=end,
            evidence=category_evidence,
            card_selector="article.news-item",
            max_pages=100,
            accept_url=lambda url, base=base: urlparse(url).path.startswith(urlparse(base).path.rstrip("/") + "/"),
        ))
        category_runs.append(category_evidence)
    evidence.update({
        "strategy": "two_cutoff_driven_environment_category_feeds",
        "categories": list(CATEGORIES),
        "category_runs": category_runs,
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": all(run.get("coverage_complete") for run in category_runs),
    })
    return sorted(selected)


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("h1.news-title", "main h1", "h1"),
        body_selectors=("section.single-news-content", ".single-news-content", "main"),
        visible_date_pattern=r"\b\d{1,2}\.\s*\d{1,2}\.\s*20\d{2}\.\s*u\s*\d{1,2}:\d{2}\b",
        evidence_body_selectors=("section.single-news-content",),
        environmental=True,
    )
