"""Publication-resumption observation adapter for a dormant environment site."""
from __future__ import annotations

from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.PAK.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "pakistanenvironmentalnews.org"
    country_code = "PAK"
    is_env = True


LISTING = "https://pakistanenvironmentalnews.org/"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    soup = BeautifulSoup(fetch(LISTING).content, "html.parser")
    rows = []
    for article in soup.select("article"):
        link = article.select_one("h2.entry-title a[href], h3.entry-title a[href]")
        url = normalize_url(urljoin(LISTING, link.get("href")) if link else "", Config.source_name)
        published = None
        if url:
            page = BeautifulSoup(fetch(url).content, "html.parser")
            stamp = page.select_one("meta[property='article:published_time'], time[datetime], time.entry-date, time")
            published = parse_datetime(stamp.get("content") or stamp.get("datetime") or stamp.get_text(" ", strip=True)) if stamp else None
        if url:
            rows.append((url, published.date() if published else None))
    selected = sorted({url: day for url, day in rows if day is None or start <= day <= end}.items(), key=lambda item: item[1] or date.min)
    newest = max((day for _, day in rows if day), default=None)
    evidence.update({
        "strategy": "publication_resumption_probe",
        "listing": LISTING,
        "listing_rows": len(rows),
        "newest_visible_publication_date": newest.isoformat() if newest else None,
        "publication_inactive_for_window": bool(newest and newest < start),
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": bool(rows and newest),
    })
    return [url for url, _ in selected]


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1.entry-title", "article h1", "h1"),
        body_selectors=("article .entry-content", ".entry-content", "article"),
        visible_date_selectors=("article time[datetime]", "article time"),
        visible_date_text_selectors=(".entry-meta",),
        prefer_visible_date=False,
        evidence_body_selectors=("article .entry-content", "article"),
    )
