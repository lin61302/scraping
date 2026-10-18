"""Pure The Star adapter using the publisher's date-sorted search index.

The public ``/news/latest`` view is capped at roughly 25 pages and cannot cover
a prior monthly window.  The site's own search widget is backed by Queryly and
exposes the same publisher index with stable arbitrary offsets.  Discovery
queries that index only; every selected article is still parsed from its
canonical The Star page and all production writes remain outside this module.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from datetime import date, timedelta
from urllib.parse import urlencode

from adapters.MYS.common import extract_article, normalize_url, parse_datetime


class Config:
    source_name = "thestar.com.my"
    country_code = "MYS"
    is_env = False


ROUTES = ("/news/nation/", "/news/world/", "/metro/")
QUERYLY_API = "https://api.queryly.com/json.aspx"
QUERYLY_KEY = "6ddd278bf17648ac"
BATCH_SIZE = 100
BOUNDARY_OVERLAP = 200
SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; MLP-MLEED-observation/1.0)"})
QUERYLY_PROXY_PREFIX = "https://r.jina.ai/http://"
QUERYLY_MIN_INTERVAL_SECONDS = 0.8
_QUERYLY_DIRECT_BLOCKED = False
_QUERYLY_LAST_REQUEST = 0.0


def _queryly_page(index: int, batch_size: int = BATCH_SIZE) -> tuple[list[dict], int, str, str]:
    global _QUERYLY_DIRECT_BLOCKED, _QUERYLY_LAST_REQUEST
    params = {
        "queryly_key": QUERYLY_KEY,
        "query": "*",
        "endindex": max(0, index),
        "batchsize": max(1, min(batch_size, BATCH_SIZE)),
        "callback": "thestarAdapterCallback",
        "showfaceted": "false",
        "timezoneoffset": 0,
        "sort": "date",
        # Queryly's unfiltered wildcard is capped at 30 days.  This is the
        # one-year option rendered by The Star's own search interface.
        "facetedkey": "pubDate|",
        "facetedvalue": "8760|",
    }
    request_url = QUERYLY_API + "?" + urlencode(params)
    raw = None
    transport = None
    transport_url = request_url
    if not _QUERYLY_DIRECT_BLOCKED:
        delay = QUERYLY_MIN_INTERVAL_SECONDS - (time.monotonic() - _QUERYLY_LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
        try:
            response = SESSION.get(
                request_url,
                timeout=45,
                verify=str(SYSTEM_CA_BUNDLE) if SYSTEM_CA_BUNDLE.exists() else True,
            )
            _QUERYLY_LAST_REQUEST = time.monotonic()
            if response.status_code in {403, 429}:
                _QUERYLY_DIRECT_BLOCKED = True
            else:
                response.raise_for_status()
                if not response.content:
                    raise ValueError("empty response")
                raw = response.text
                transport = "publisher_embedded_queryly_api"
        except (requests.RequestException, ValueError):
            _QUERYLY_DIRECT_BLOCKED = True
    if raw is None:
        transport_url = QUERYLY_PROXY_PREFIX + request_url.split("://", 1)[1]
        errors = []
        for attempt in range(3):
            try:
                response = requests.get(transport_url, timeout=70, headers=SESSION.headers)
                response.raise_for_status()
                if not response.content:
                    raise ValueError("empty response")
                raw = response.text
                transport = "jina_render_of_publisher_embedded_queryly_api"
                break
            except (requests.RequestException, ValueError) as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                if attempt < 2:
                    time.sleep(2 + attempt)
        else:
            raise RuntimeError(f"The Star search API and render fallback failed: {'; '.join(errors)}")

    marker = "thestarAdapterCallback("
    left = raw.find(marker)
    right = raw.find(");", left + len(marker))
    if left < 0 or right <= left:
        raise RuntimeError("The Star search API returned invalid JSONP")
    try:
        payload = json.loads(raw[left + len(marker):right])
    except ValueError as exc:
        raise RuntimeError("The Star search API returned invalid JSON") from exc
    items = payload.get("items")
    metadata = payload.get("metadata")
    if not isinstance(items, list) or not isinstance(metadata, dict):
        raise RuntimeError("The Star search API omitted items or metadata")
    try:
        total = int(metadata["total"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("The Star search API omitted its total count") from exc
    return items, total, transport_url, transport or "unknown"


def _item_date(item: dict) -> date:
    parsed = parse_datetime(item.get("pubdate"))
    if parsed is None:
        raise RuntimeError(f"The Star search item has no parseable publication date: {item!r}")
    return parsed.date()


def _date_at(index: int, calls: list[dict]) -> tuple[date, int]:
    items, total, request_url, transport = _queryly_page(index, 1)
    if not items:
        raise RuntimeError(f"The Star search index returned no item at offset {index} of {total}")
    day = _item_date(items[0])
    calls.append({"offset": index, "rows": 1, "first_date": day.isoformat(), "url": request_url, "transport": transport})
    return day, total


def _first_offset_on_or_before(target: date, total: int, calls: list[dict]) -> int:
    """Return the first descending-index offset whose date is <= target."""
    low, high = 0, total
    while low < high:
        middle = (low + high) // 2
        day, observed_total = _date_at(middle, calls)
        # New posts can enter at the head while discovery runs.  A small page
        # overlap below absorbs that drift; a material count jump is unsafe.
        if abs(observed_total - total) > BOUNDARY_OVERLAP:
            raise RuntimeError(
                f"The Star search index changed materially during discovery: {total} -> {observed_total}"
            )
        if day <= target:
            high = middle
        else:
            low = middle + 1
    return low


def _first_offset_before(target: date, total: int, calls: list[dict]) -> int:
    """Return the first descending-index offset whose date is < target."""
    low, high = 0, total
    while low < high:
        middle = (low + high) // 2
        day, observed_total = _date_at(middle, calls)
        if abs(observed_total - total) > BOUNDARY_OVERLAP:
            raise RuntimeError(
                f"The Star search index changed materially during discovery: {total} -> {observed_total}"
            )
        if day < target:
            high = middle
        else:
            low = middle + 1
    return low


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end:
        raise ValueError("start date follows end date")
    # Queryly records The Star's local midnight as the preceding UTC date for
    # many articles.  A two-day discovery envelope prevents a timezone edge
    # from omitting a requested article; the observer validates page dates.
    lower, upper = start - timedelta(days=2), end + timedelta(days=2)
    initial, total, initial_url, initial_transport = _queryly_page(0, 1)
    if not initial or total <= 0:
        raise RuntimeError("The Star search index is empty")
    newest = _item_date(initial[0])
    if lower < newest - timedelta(days=366):
        raise RuntimeError("The Star search index exposes one year; requested start is outside that window")

    calls: list[dict] = [{
        "offset": 0,
        "rows": 1,
        "first_date": newest.isoformat(),
        "url": initial_url,
        "transport": initial_transport,
    }]
    first = _first_offset_on_or_before(upper, total, calls)
    after = _first_offset_before(lower, total, calls)
    if first >= total or after <= first:
        evidence.update({
            "strategy": "publisher_queryly_date_sorted_index",
            "search_api": QUERYLY_API,
            "publisher_index_total": total,
            "query_date_envelope": {"start": lower.isoformat(), "end": upper.isoformat()},
            "search_calls": calls,
            "coverage_complete": True,
            "discovered_urls_after_filter": 0,
        })
        return []

    page_start = max(0, ((first - BOUNDARY_OVERLAP) // BATCH_SIZE) * BATCH_SIZE)
    page_stop = min(total, after + BOUNDARY_OVERLAP)
    found: dict[str, date] = {}
    scanned = 0
    for offset in range(page_start, page_stop, BATCH_SIZE):
        items, observed_total, request_url, transport = _queryly_page(offset)
        if abs(observed_total - total) > BOUNDARY_OVERLAP:
            raise RuntimeError(
                f"The Star search index changed materially during discovery: {total} -> {observed_total}"
            )
        days = [_item_date(item) for item in items]
        calls.append({
            "offset": offset,
            "rows": len(items),
            "newest_date": max(days).isoformat() if days else None,
            "oldest_date": min(days).isoformat() if days else None,
            "url": request_url,
            "transport": transport,
        })
        scanned += len(items)
        for item, day in zip(items, days):
            if not lower <= day <= upper:
                continue
            url = normalize_url(item.get("link") or "", Config.source_name)
            if url and any(route in url.casefold() for route in ROUTES):
                found[url] = day

    # Prove both chronological boundaries independently of the page envelope.
    first_day, _ = _date_at(first, calls)
    after_day = None
    if after < total:
        after_day, _ = _date_at(after, calls)
    coverage_complete = first_day <= upper and (after_day is None or after_day < lower)
    if not coverage_complete:
        raise RuntimeError("The Star search index boundary reconciliation failed")
    urls = [url for url, _ in sorted(found.items(), key=lambda item: (item[1], item[0]))]
    evidence.update({
        "strategy": "publisher_queryly_date_sorted_index",
        "search_api": QUERYLY_API,
        "publisher_index_total": total,
        "publisher_newest_date": newest.isoformat(),
        "query_date_envelope": {"start": lower.isoformat(), "end": upper.isoformat()},
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "boundary_offsets": {"first_on_or_before_upper": first, "first_before_lower": after},
        "boundary_overlap_items": BOUNDARY_OVERLAP,
        "index_rows_scanned": scanned,
        "search_calls": calls,
        "interested_routes": list(ROUTES),
        "discovered_urls_after_filter": len(urls),
        "coverage_complete": True,
    })
    return urls


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        language="en",
        title_selectors=("h1.primary-heading", "article h1", "main h1", "h1"),
        body_selectors=(".story-content", ".article-content", "article"),
        visible_date_selectors=("[itemprop='datePublished']", "article time", "time"),
        evidence_body_selectors=(".story-content", "article", "main"),
    )
