"""Observation-only EnvPK Pakistan-category adapter."""
from __future__ import annotations

from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.PAK.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "envpk.com"
    country_code = "PAK"
    is_env = True


BASE = "https://www.envpk.com/category/news/pakistan/"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    rows = []
    pages = []
    reached_cutoff = False
    for page in range(1, 31):
        url = BASE if page == 1 else f"{BASE}page/{page}/"
        response = fetch(url)
        soup = BeautifulSoup(response.content, "html.parser")
        page_rows = []
        for article in soup.select("article"):
            link = article.select_one("h2.entry-title a[href], h3.entry-title a[href]")
            stamp = article.select_one("time[datetime], time.entry-date, time")
            article_url = normalize_url(urljoin(url, link.get("href")) if link else "", Config.source_name)
            published = parse_datetime(stamp.get("datetime") or stamp.get_text(" ", strip=True)) if stamp else None
            if article_url:
                page_rows.append((article_url, published.date() if published else None))
        if not page_rows:
            break
        rows.extend(page_rows)
        dated = [day for _, day in page_rows if day]
        pages.append({"url": url, "rows": len(page_rows), "oldest": min(dated).isoformat() if dated else None, "newest": max(dated).isoformat() if dated else None})
        if dated and min(dated) < start:
            reached_cutoff = True
            break
    selected = sorted({url: day for url, day in rows if day is None or start <= day <= end}.items(), key=lambda item: item[1] or date.min)
    evidence.update({
        "strategy": "cutoff_driven_pakistan_category",
        "listing": BASE,
        "pages_checked": pages,
        "rows_seen": len(rows),
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": reached_cutoff,
        "publication_inactive_for_window": bool(
            not selected and reached_cutoff and pages
            and pages[0].get("newest") is not None
            and pages[0]["newest"] < start.isoformat()
        ),
    })
    return [url for url, _ in selected]


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1.entry-title", "article h1", "h1"),
        body_selectors=("article .entry-content", ".entry-content", "article"),
        visible_date_selectors=("article time[datetime]", "article time"),
        prefer_visible_date=True,
        evidence_body_selectors=("article .entry-content", "article"),
    )
