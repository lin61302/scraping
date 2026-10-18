"""Pure observer for Mongabay's sequential post-sitemap inventory."""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from adapters.INT.common import (
    extract_article,
    fetch,
    normalize_url,
    published_datetime,
    response_sha256,
    sitemap_rows,
    url_set_sha256,
    xml_response,
)


class Config:
    source_name = "news.mongabay.com"
    country_code = "INT"
    is_env = True


BASE = "https://news.mongabay.com"
PATH_MONTH = re.compile(r"^/(20\d{2})/(\d{2})/")
MAX_SITEMAPS = 60
_responses = {}
_response_lock = threading.Lock()


def _month(url: str):
    match = PATH_MONTH.match(urlparse(url).path)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return None


def _sitemap_url(number: int) -> str:
    return "%s/%s" % (BASE, "post-sitemap.xml" if number == 1 else "post-sitemap%d.xml" % number)


def _prefetch_published(url: str):
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    published, _, _ = published_datetime(soup)
    if not published:
        raise RuntimeError("Mongabay boundary candidate lacks publisher publication date: %s" % url)
    with _response_lock:
        _responses[url] = response
    return url, published.date()


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 180:
        raise ValueError("invalid Mongabay window (max 181 days)")
    _responses.clear()
    checked = []
    inventory = []
    empty_after_frontier = []
    last_nonempty_rows = None
    for number in range(1, MAX_SITEMAPS + 1):
        endpoint = _sitemap_url(number)
        response, soup = xml_response(endpoint)
        rows = sitemap_rows(soup)
        checked.append({
            "sequence": number,
            "url": endpoint,
            "entries": len(rows),
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
        })
        if rows:
            if empty_after_frontier:
                raise RuntimeError("Mongabay nonempty sitemap follows an empty sitemap")
            inventory.extend(rows)
            last_nonempty_rows = [row["url"] for row in rows]
        else:
            empty_after_frontier.append(number)
            if len(empty_after_frontier) == 2:
                break
    if last_nonempty_rows is None or len(empty_after_frontier) < 2:
        raise RuntimeError("Mongabay sitemap frontier was not bounded by two empty successors")
    repeat_response, repeat_soup = xml_response(_sitemap_url(empty_after_frontier[0] - 1))
    repeat_rows = [row["url"] for row in sitemap_rows(repeat_soup)]
    frontier_stable = repeat_rows == last_nonempty_rows
    start_month = start.replace(day=1)
    end_month = end.replace(day=1)
    candidates = []
    path_months = []
    for row in inventory:
        normalized = normalize_url(row["url"], {Config.source_name})
        month = _month(normalized) if normalized else None
        if month:
            path_months.append(month)
            if start_month <= month <= end_month:
                candidates.append(normalized)
    candidates = sorted(set(candidates), key=lambda item: (_month(item), item))
    boundary_urls = [item for item in candidates if _month(item) in {start_month, end_month}]
    exact_dates = {}
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="mongabay-boundary") as pool:
        for url, published in pool.map(_prefetch_published, boundary_urls):
            exact_dates[url] = published
    selected = []
    boundary_excluded = []
    for url in candidates:
        month = _month(url)
        published = exact_dates.get(url)
        if month in {start_month, end_month}:
            if start <= published <= end:
                selected.append(url)
            else:
                boundary_excluded.append({"url": url, "page_publication_date": published.isoformat()})
        else:
            selected.append(url)
    boundary_before = bool(path_months and min(path_months) < start_month)
    boundary_after = bool(path_months and max(path_months) > end_month)
    coverage = bool(frontier_stable and boundary_before and boundary_after)
    evidence.update({
        "method": "sequential_post_sitemaps_to_two_empty_successors_plus_boundary_page_dates",
        "sitemaps": checked,
        "last_nonempty_sequence": empty_after_frontier[0] - 1,
        "empty_successor_sequences": empty_after_frontier,
        "frontier_url_set_stable": frontier_stable,
        "repeat_frontier_sha256": response_sha256(repeat_response),
        "path_month_is_candidate_only_article_page_date_is_authoritative": True,
        "candidate_path_month_urls": len(candidates),
        "boundary_page_dates_checked": len(boundary_urls),
        "boundary_candidates_excluded": boundary_excluded,
        "boundary_before_start_month": boundary_before,
        "boundary_after_end_month": boundary_after,
        "selected_url_set_sha256": url_set_sha256(selected),
        "coverage_complete": coverage,
    })
    if not coverage:
        raise RuntimeError("Mongabay sitemap inventory did not close the requested window")
    return selected


def scrape_url(url: str) -> dict:
    with _response_lock:
        response = _responses.pop(url, None)
    article, _ = extract_article(
        url=url,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("article.post h1", "main article h1", "main h1"),
        body_selectors=("article.post",),
        remove_selectors=("dialog", ".single-article-meta", ".related-articles", ".article-end", "blockquote"),
        parser="mongabay_visible_h1_jsonld_date_article_body",
        response=response,
    )
    return article
