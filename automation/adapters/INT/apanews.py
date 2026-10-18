"""Pure inventory-only adapter for the APANews French environment category.

The historical updater used a browser to expand the publisher environment
listing and then mixed extraction with direct database writes.  The current
publisher page exposes a server-rendered JSON-LD ``CollectionPage`` with ten
category members per page.  Publication-looking timestamps are rendered in
article cards, but the JSON-LD list and the cards can be offset by one page.
This adapter therefore joins category membership to timestamps across adjacent
pages and fails closed unless both date boundaries are proven.

Listing timestamps are inventory evidence only.  A later article observer must
confirm title, ``date_publish``, main text, canonical URL, and language from the
article page.  This module has no database, credential, cache, subprocess, or
file-write path, and ``scrape_url`` is deliberately disabled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from html import unescape
from typing import Iterable
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


class Config:
    source_name = "apanews.net"
    country_code = "INT"
    is_env = True
    inventory_only = True
    access_block_circuit_threshold = 1


FIRST_PASS_START = date(2026, 6, 2)
FIRST_PASS_END = date(2026, 9, 30)
CATEGORY_URL = "https://fr.apanews.net/category/environnement/"
MAX_WINDOW_DAYS = 184
MAX_PAGES = 32
MAX_RESPONSE_BYTES = 1_500_000
MIN_REQUEST_INTERVAL_SECONDS = 6.0
USER_AGENT = "MLP-MLEED source inventory/1.0 (bounded research QA)"
INVENTORY_ONLY = True
_PAGE_RE = re.compile(r"^/category/environnement/page/(\d+)/$")
_LOCK = threading.Lock()
_LAST_REQUEST_STARTED = 0.0


class AccessLimitedError(RuntimeError):
    """The publisher asked this bounded inventory to stop or slow down."""


def _request(url: str, *, timeout: int = 30) -> requests.Response:
    global _LAST_REQUEST_STARTED
    with _LOCK:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_STARTED)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_STARTED = time.monotonic()
    response = requests.get(
        url,
        timeout=timeout,
        allow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.1"},
    )
    if response.status_code in {403, 429}:
        raise AccessLimitedError(
            "APANews returned HTTP %s; stop immediately and retry only in a later bounded run"
            % response.status_code
        )
    response.raise_for_status()
    if not response.content:
        raise RuntimeError("APANews returned an empty category page")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("APANews category page exceeds the 1.5 MB safety cap")
    return response


def _page_url(page: int) -> str:
    if page < 1:
        raise ValueError("APANews page number must be positive")
    return CATEGORY_URL if page == 1 else "%spage/%d/" % (CATEGORY_URL, page)


def _canonicalize_article_url(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = urlparse(unescape(raw.strip()))
    except Exception:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    path = re.sub(r"/{2,}", "/", parsed.path or "")
    forbidden = (
        "/category/", "/tag/", "/author/", "/wp-admin/", "/wp-login", "/search/",
    )
    if (
        parsed.scheme not in {"http", "https"}
        or host != "fr.apanews.net"
        or parsed.username
        or parsed.password
        or not path
        or path == "/"
        or path.startswith(forbidden)
    ):
        return None
    return urlunparse(("https", host, path.rstrip("/") + "/", "", "", ""))


def _walk_json(value: object):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(child for child in item.values() if isinstance(child, (dict, list)))
        elif isinstance(item, list):
            stack.extend(item)


def _collection_items(soup: BeautifulSoup, requested_url: str) -> list[dict]:
    collections = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (TypeError, ValueError):
            continue
        for item in _walk_json(payload):
            if item.get("@type") != "CollectionPage":
                continue
            main = item.get("mainEntity")
            if isinstance(main, dict) and main.get("@type") == "ItemList":
                collections.append(main.get("itemListElement"))
    if len(collections) != 1 or not isinstance(collections[0], list):
        raise RuntimeError("APANews category page lacks one unambiguous JSON-LD ItemList")
    rows = []
    for raw in collections[0]:
        if not isinstance(raw, dict) or raw.get("@type") != "ListItem":
            raise RuntimeError("APANews category ItemList contains a malformed member")
        url = _canonicalize_article_url(raw.get("url"))
        position = raw.get("position")
        try:
            position = int(position)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("APANews category ItemList position is invalid") from exc
        if not url:
            raise RuntimeError("APANews category ItemList contains a non-article URL")
        rows.append({"position": position, "url": url})
    rows.sort(key=lambda row: row["position"])
    if not rows or len(rows) > 20 or [row["position"] for row in rows] != list(range(1, len(rows) + 1)):
        raise RuntimeError("APANews category ItemList positions are incomplete or excessive")
    if len({row["url"] for row in rows}) != len(rows):
        raise RuntimeError("APANews category ItemList repeats an article URL")
    return rows


def _article_card_times(soup: BeautifulSoup) -> dict[str, set[str]]:
    observations: dict[str, set[str]] = defaultdict(set)
    for article in soup.find_all("article"):
        stamp = article.select_one("time[datetime]")
        if not stamp:
            continue
        raw = stamp.get("datetime")
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            continue
        normalized = parsed.isoformat()
        for anchor in article.select("a[href]"):
            url = _canonicalize_article_url(anchor.get("href"))
            if url:
                observations[url].add(normalized)
    return observations


def _next_page(soup: BeautifulSoup, current_page: int) -> str | None:
    expected = current_page + 1
    candidates = set()
    for anchor in soup.select('a[href*="/category/environnement/page/"]'):
        parsed = urlparse(anchor.get("href"))
        if (parsed.hostname or "").lower() != "fr.apanews.net":
            continue
        match = _PAGE_RE.match(parsed.path)
        if match and int(match.group(1)) == expected:
            candidates.add(_page_url(expected))
    if len(candidates) > 1:
        raise RuntimeError("APANews category page has ambiguous pagination")
    return next(iter(candidates), None)


def _read_page(page: int) -> tuple[list[dict], dict[str, set[str]], dict, str | None]:
    url = _page_url(page)
    response = _request(url)
    soup = BeautifulSoup(response.content, "html.parser")
    rows = _collection_items(soup, url)
    observations = _article_card_times(soup)
    return rows, observations, {
        "page": page,
        "url": url,
        "http_status": response.status_code,
        "response_bytes": len(response.content),
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
        "itemlist_rows": len(rows),
        "dated_card_urls": len(observations),
    }, _next_page(soup, page)


def _url_set_sha256(urls: Iterable[str]) -> str:
    payload = "".join("%s\n" % url for url in sorted(set(urls)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    """Return a date-bounded category inventory or fail closed.

    One look-ahead page is retained because current JSON-LD membership and
    rendered timestamp cards can be offset across adjacent pages.
    """
    if start > end or (end - start).days >= MAX_WINDOW_DAYS:
        raise ValueError("APANews inventory must span 1 to 184 days")

    member_pages: dict[int, list[dict]] = {}
    time_observations: dict[str, set[str]] = defaultdict(set)
    page_receipts = []
    boundary_page = None
    next_url = CATEGORY_URL

    for page in range(1, MAX_PAGES + 1):
        if next_url != _page_url(page):
            raise RuntimeError("APANews pagination sequence changed")
        rows, times, receipt, next_url = _read_page(page)
        member_pages[page] = rows
        page_receipts.append(receipt)
        for url, values in times.items():
            time_observations[url].update(values)

        previous = member_pages.get(page - 1)
        if previous:
            previous_dates = []
            for row in previous:
                values = time_observations.get(row["url"], set())
                if len(values) != 1:
                    break
                previous_dates.append(datetime.fromisoformat(next(iter(values))).date())
            else:
                if previous_dates and max(previous_dates) < start:
                    boundary_page = page - 1
                    break
        if next_url is None:
            break

    all_members = [dict(row, page=page) for page, rows in member_pages.items() for row in rows]
    if len({row["url"] for row in all_members}) != len(all_members):
        raise RuntimeError("APANews category pagination repeats ItemList URLs")

    joined = []
    for row in all_members:
        values = sorted(time_observations.get(row["url"], set()))
        joined.append(dict(row, listing_datetimes=values, listing_datetime=values[0] if len(values) == 1 else None))

    unresolved = [row for row in joined if row["listing_datetime"] is None]
    selected = [
        row for row in joined
        if row["listing_datetime"]
        and start <= datetime.fromisoformat(row["listing_datetime"]).date() <= end
    ]
    after = [
        row for row in joined
        if row["listing_datetime"] and datetime.fromisoformat(row["listing_datetime"]).date() > end
    ]
    before = [
        row for row in joined
        if row["listing_datetime"] and datetime.fromisoformat(row["listing_datetime"]).date() < start
    ]
    complete = bool(boundary_page and after and before and not unresolved)
    evidence.update({
        "mode": "inventory_only_no_article_pages_no_production_writes",
        "method": "publisher_environment_category_jsonld_with_cross_page_card_time_join",
        "category_url": CATEGORY_URL,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "pages_fetched": len(page_receipts),
        "page_receipts": page_receipts,
        "itemlist_rows": len(all_members),
        "dated_rows": len(joined) - len(unresolved),
        "unresolved_rows": unresolved,
        "selected_rows": selected,
        "selected": len(selected),
        "selected_month_counts": dict(sorted(Counter(row["listing_datetime"][:7] for row in selected).items())),
        "selected_url_set_sha256": _url_set_sha256(row["url"] for row in selected),
        "boundary_page": boundary_page,
        "boundary_after_end": min(after, key=lambda row: row["listing_datetime"]) if after else None,
        "boundary_before_start": max(before, key=lambda row: row["listing_datetime"]) if before else None,
        "inventory_complete": complete,
        "coverage_complete": False,
        "first_pass_verified_through": None,
        "strict_verified_through": None,
        "listing_datetime_authoritative": False,
        "authoritative_publication_date_rule": "publisher article datePublished in a later approved article observation",
        "article_pages_fetched": 0,
        "legacy_database_writer_executed": False,
        "production_writes": False,
        "minimum_request_interval_seconds": MIN_REQUEST_INTERVAL_SECONDS,
    })
    if not complete:
        raise RuntimeError(
            "APANews inventory did not close both dates with every category member timestamp-resolved"
        )
    return [row["url"] for row in selected]


def scrape_url(url: str) -> dict:
    raise RuntimeError("APANews is inventory-only; article scraping is disabled")


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date, default=FIRST_PASS_START)
    parser.add_argument("--end", type=_parse_date, default=FIRST_PASS_END)
    parser.add_argument("--include-urls", action="store_true")
    args = parser.parse_args(argv)
    evidence: dict = {}
    try:
        urls = discover_urls(args.start, args.end, evidence)
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": "%s: %s" % (type(exc).__name__, exc), "evidence": evidence}, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    payload = {"status": "inventory_complete", "evidence": evidence}
    if args.include_urls:
        payload["urls"] = urls
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
