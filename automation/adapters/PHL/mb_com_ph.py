"""Manila Bulletin category adapter with reusable browser clearance."""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from adapters.PHL.common import (
    AccessBlockedError,
    _close_browser,
    extract_article,
    fetch,
    normalize_url,
)


class Config:
    source_name = "mb.com.ph"
    country_code = "PHL"
    is_env = False


DENY = (
    "/commencement/", "/title-1", "/Entertainment/", "/Funpage/", "/coach/",
    "/wp-content/", "/amp/", "/lifestyle/", "/opinions-and-editorials/", "/sports/",
    "/challenge-platform/", "/entertainment/", "/opinions/", "/wp-json/",
)

CATEGORIES = (
    "world", "national", "metro-manila", "luzon", "visayas", "mindanao",
    "environment-and-sustainability", "embassies-foreign-affairs",
)
DATED_URL = re.compile(r"/((?:20\d{2})/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))/")
SEARCH = "https://mb.com.ph/search?query=&limit=12&page={page}"
ALLOWED_CATEGORY_LABELS = {
    "world", "national", "metro manila", "luzon", "visayas", "mindanao",
    "environment and sustainability", "embassies foreign affairs",
}


def _search_page(page: int) -> tuple[list[tuple[str, date]], int, str]:
    page_url = SEARCH.format(page=page)
    access_errors = []
    for attempt in range(2):
        try:
            response = fetch(page_url, timeout=45)
            break
        except AccessBlockedError as exc:
            access_errors.append(str(exc))
            _close_browser()
            if attempt:
                raise AccessBlockedError(
                    f"Manila Bulletin search page {page} remained blocked after "
                    f"one fresh-clearance retry: {'; '.join(access_errors)}"
                ) from exc
            time.sleep(3)
    soup = BeautifulSoup(response.content, "html.parser")
    rows = []
    seen = set()
    for anchor in soup.select(".search-page a[href]"):
        raw = anchor.get("href") or ""
        match = DATED_URL.search(raw)
        if not match:
            continue
        try:
            published = date.fromisoformat(
                "-".join(f"{int(value):02d}" if index else value
                         for index, value in enumerate(match.group(1).split("/")))
            )
        except ValueError:
            continue
        url = normalize_url(raw, Config.source_name, DENY)
        if url and url not in seen:
            seen.add(url)
            rows.append((url, published))
    if not rows:
        raise RuntimeError(f"Manila Bulletin search page {page} had no dated URLs")
    page_numbers = [page]
    for anchor in soup.select(".pagination a[href], nav.pagination a[href]"):
        query = parse_qs(urlparse(anchor.get("href") or "").query)
        try:
            page_numbers.append(int((query.get("page") or [0])[0]))
        except (TypeError, ValueError):
            pass
    return rows, max(page_numbers), getattr(response, "scrape_transport", "requests")


def _first_page_at_or_before(target: date, maximum: int, cache: dict) -> int:
    answer = maximum
    low, high = 1, maximum
    while low <= high:
        middle = (low + high) // 2
        if middle not in cache:
            cache[middle] = _search_page(middle)
        dates = [item[1] for item in cache[middle][0]]
        if min(dates) <= target:
            answer = middle
            high = middle - 1
        else:
            low = middle + 1
    return answer


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end:
        raise ValueError("start date follows end date")
    cache = {1: _search_page(1)}
    maximum = cache[1][1]
    first_page = _first_page_at_or_before(end, maximum, cache)
    last_page = _first_page_at_or_before(start - timedelta(days=1), maximum, cache)
    result, seen, pages = [], set(), []
    for page in range(first_page, last_page + 1):
        if page not in cache:
            cache[page] = _search_page(page)
        rows, _, transport = cache[page]
        selected = 0
        for url, published in rows:
            if start <= published <= end and url not in seen:
                seen.add(url)
                result.append(url)
                selected += 1
        dates = [item[1] for item in rows]
        pages.append({
            "page": page,
            "url": SEARCH.format(page=page),
            "dated_urls": len(rows),
            "selected": selected,
            "newest": max(dates).isoformat(),
            "oldest": min(dates).isoformat(),
            "transport": transport,
        })
    evidence.update({
        "strategy": "browser_cleared_global_search_binary_date_location",
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "included_categories": list(CATEGORIES),
        "search_maximum_page": maximum,
        "located_page_range": [first_page, last_page],
        "pages_checked": pages,
        "coverage_complete": bool(pages and min(date.fromisoformat(item["oldest"]) for item in pages) < start),
        "discovered_urls_after_filter": len(result),
        "blacklist_url_patterns": list(DENY),
    })
    return result


def scrape_url(url: str) -> dict:
    article = extract_article(
        url, source=Config.source_name,
        title_selectors=('meta[property="og:title"]', "article h1", "h1"),
        body_selectors=(".article-full-body", "article"),
        visible_date_selectors=(
            'meta[name="datePublished"]', 'meta[property="article:published_time"]',
            'meta[itemprop="datePublished"]',
        ),
        prefer_visible_date=False,
        evidence_body_selectors=(".article-full-body",),
    )
    categories = [
        re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
        for value in (article.get("field_evidence") or {}).get("page_categories", [])
    ]
    allowed = any(value in ALLOWED_CATEGORY_LABELS for value in categories)
    if categories and not allowed:
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_out_of_scope_section"
        article["exclusion_evidence"] = {
            "policy_type": "fully_parsed_out_of_scope_section",
            "publisher_url": url,
            "page_categories": categories,
            "allowed_categories": sorted(ALLOWED_CATEGORY_LABELS),
        }
    return article
