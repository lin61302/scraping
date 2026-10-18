"""Observation adapter for Insajder's newest-first news listing."""
from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from adapters.SRB.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "insajder.net"
    country_code = "SRB"
    is_env = False


LISTING = "https://insajder.net/vesti?page={}"
ROW_PATTERN = re.compile(
    r'\\"href\\":\\"(/vesti/[^\\"]+)\\"(?:(?!\\"href\\").){0,12000}?'
    r'\\"date\\":\\"([^\\"]+)',
    re.S,
)


def _rows(page_html: str) -> list[tuple[str, object]]:
    result = []
    seen = set()
    for raw, date_raw in ROW_PATTERN.findall(page_html):
        url = normalize_url(raw, Config.source_name)
        published = parse_datetime(date_raw)
        if url and url not in seen:
            seen.add(url)
            result.append((url, published.date() if published else None))
    # Preserve candidates if a frontend deployment changes the serialized
    # model while leaving visible cards intact. They will be date-gated after
    # article parsing by the observer.
    if not result:
        soup = BeautifulSoup(page_html, "html.parser")
        for link in soup.select('a.article-card[href^="/vesti/"]'):
            url = normalize_url(link.get("href", ""), Config.source_name)
            if url and url not in seen:
                seen.add(url)
                result.append((url, None))
    return result


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    selected = {}
    pages = []
    consecutive_old = 0
    first_rows = _rows(fetch(LISTING.format(1)).text)
    second_rows = _rows(fetch(LISTING.format(2)).text)
    repeated_sidebar = {url for url, _ in first_rows} & {url for url, _ in second_rows}
    for page in range(1, 7000):
        url = LISTING.format(page)
        rows = first_rows if page == 1 else second_rows if page == 2 else _rows(fetch(url).text)
        if not rows:
            break
        page_rows = rows if page == 1 else [(article_url, day) for article_url, day in rows if article_url not in repeated_sidebar]
        dated = [day for _, day in page_rows if day]
        pages.append({"url": url, "rows": len(page_rows), "sidebar_rows_removed": len(rows) - len(page_rows), "dated_rows": len(dated), "oldest": min(dated).isoformat() if dated else None, "newest": max(dated).isoformat() if dated else None})
        for article_url, day in rows:
            if day is None or start <= day <= end:
                selected[article_url] = day
        consecutive_old = consecutive_old + 1 if dated and max(dated) < start else 0
        if consecutive_old >= 2:
            break
    evidence.update({
        "strategy": "cutoff_driven_nextjs_listing",
        "repeated_sidebar_urls": len(repeated_sidebar),
        "pages_checked": pages,
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": consecutive_old >= 2,
    })
    return sorted(selected, key=lambda url: (selected[url] or date.min, url))


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1", "h1"),
        body_selectors=("article .pt-6", "article"),
        visible_date_selectors=('meta[property="article:published_time"]',),
        evidence_body_selectors=("article .pt-6",),
    )
