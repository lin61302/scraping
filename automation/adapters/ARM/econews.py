"""Pure EcoNews adapter with explicit inactivity boundary evidence."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.ARM.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "econews.am"
    country_code = "ARM"
    is_env = True


BASE = "https://econews.am"
CATEGORIES = (1, 2, 3, 31)


def _published(url: str) -> date | None:
    soup = BeautifulSoup(fetch(url).content, "html.parser")
    node = soup.select_one("article.news time, time")
    parsed = parse_datetime(node.get_text(" ", strip=True)) if node else None
    return parsed.date() if parsed else None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    result, seen, categories = [], set(), []
    for category in CATEGORIES:
        pages, boundary = [], False
        for page in range(1, 151):
            listing_url = f"{BASE}/?cat={category}&paged={page}&l=am"
            soup = BeautifulSoup(fetch(listing_url).content, "html.parser")
            urls = []
            for anchor in soup.select("a.clearfix[href]"):
                normalized = normalize_url(urljoin(BASE, anchor.get("href", "")), Config.source_name)
                if normalized and "p=" in normalized and normalized not in urls:
                    urls.append(normalized)
            if not urls:
                pages.append({"page": page, "url": listing_url, "rows": 0})
                boundary = True
                break
            first_date = _published(urls[0])
            # Category pages are newest-first.  The first page's first article
            # is sufficient to prove a true zero when the source is inactive.
            if first_date and first_date < lower:
                pages.append({
                    "page": page, "url": listing_url, "rows": len(urls),
                    "newest_article_date": first_date.isoformat(),
                    "boundary_reason": "newest_article_older_than_start",
                })
                boundary = True
                break
            dated = []
            for url in urls:
                published = _published(url)
                dated.append((url, published))
                if published and lower <= published <= upper and url not in seen:
                    seen.add(url)
                    result.append(url)
            dates = [published for _, published in dated if published]
            pages.append({
                "page": page, "url": listing_url, "rows": len(urls),
                "newest_article_date": max(dates).isoformat() if dates else None,
                "oldest_article_date": min(dates).isoformat() if dates else None,
            })
            if dates and max(dates) < lower:
                boundary = True
                break
        if not boundary:
            raise RuntimeError(f"EcoNews category {category} did not reach requested start")
        categories.append({"category": category, "pages_checked": pages, "boundary_reached": boundary})
    complete = all(item["boundary_reached"] for item in categories)
    newest_visible = max(
        (
            page.get("newest_article_date")
            for item in categories
            for page in item["pages_checked"]
            if page.get("newest_article_date")
        ),
        default=None,
    )
    evidence.update({
        "strategy": "category_pagination_with_article_page_dates",
        "categories": categories,
        "candidate_date_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "coverage_complete": complete,
        "verified_inactive": len(result) == 0,
        "publication_inactive_for_window": complete and len(result) == 0,
        "newest_visible_publication_date": newest_visible,
        "discovered_urls_after_filter": len(result),
    })
    return result


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        language="hy",
        title_selectors=("article.news h1", "h1"),
        body_selectors=("article.news .desc", ".desc"),
        visible_date_selectors=("article.news time", "time"),
        prefer_visible_date=True,
        evidence_body_selectors=("article.news .desc", ".desc"),
    )
