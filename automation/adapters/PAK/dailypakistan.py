"""Observation-only Daily Pakistan environment adapter with HTTP pagination."""
from __future__ import annotations

from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.PAK.common import SESSION, clean, extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "dailypakistan.com.pk"
    country_code = "PAK"
    is_env = True


LISTING = "https://dailypakistan.com.pk/environment"
AJAX = "https://dailypakistan.com.pk/ajax_post_pagination"


def _rows(soup: BeautifulSoup):
    rows = []
    for card in soup.select(".post-review-category-page"):
        link = card.select_one("div.post-title h3 a[href], div.prr-post-1-tt-div h3 a[href]")
        stamp = card.select_one("div.large-post-meta span")
        url = normalize_url(urljoin(LISTING, link.get("href")) if link else "", Config.source_name)
        published = parse_datetime(stamp.get_text(" ", strip=True)) if stamp else None
        if url:
            rows.append((url, published.date() if published else None))
    return rows


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    response = fetch(LISTING)
    soup = BeautifulSoup(response.content, "html.parser")
    rows = _rows(soup)
    pages = [{"kind": "initial", "url": LISTING, "rows": len(rows)}]
    offset = int((soup.select_one("#post_pagination_record") or {}).get("value", 36))
    reached_cutoff = bool([day for _, day in rows if day] and min(day for _, day in rows if day) < start)
    # The current initial page already spans multiple months. Continue only when
    # the requested cutoff is older than the oldest row exposed there.
    for _ in range(25):
        if reached_cutoff:
            break
        result = SESSION.post(AJAX, data={
            "post_per_page": "36",
            "post_listing_limit_offset": str(offset),
            "directory_name": "categories_pages",
            "template_name": "lazy_loading",
            "category_name": "environment",
        }, timeout=35)
        result.raise_for_status()
        if clean(result.text) == "no_more_news":
            break
        new_rows = _rows(BeautifulSoup(result.content, "html.parser"))
        pages.append({"kind": "ajax", "offset": offset, "rows": len(new_rows)})
        if not new_rows:
            break
        rows.extend(new_rows)
        dated = [day for _, day in new_rows if day]
        reached_cutoff = bool(dated and min(dated) < start)
        offset += 36
    selected = sorted({url: day for url, day in rows if day is None or start <= day <= end}.items(), key=lambda item: item[1] or date.min)
    evidence.update({
        "strategy": "cutoff_driven_first_party_ajax_listing",
        "listing": LISTING,
        "ajax_endpoint": AJAX,
        "pages_checked": pages,
        "rows_seen": len(rows),
        "oldest_visible_date": min((day for _, day in rows if day), default=None).isoformat() if any(day for _, day in rows) else None,
        "newest_visible_date": max((day for _, day in rows if day), default=None).isoformat() if any(day for _, day in rows) else None,
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": reached_cutoff,
    })
    return [url for url, _ in selected]


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1", ".news-detail h1", "h1"),
        body_selectors=("div.post-desc div.news-detail-content-class", ".news-detail-content-class", ".post-desc"),
        visible_date_selectors=("meta[property='article:published_time']", "div.large-post-meta span"),
        prefer_visible_date=False,
        evidence_body_selectors=(".news-detail-content-class", ".post-desc"),
        language="ur",
    )
