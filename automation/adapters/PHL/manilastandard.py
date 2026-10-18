"""Manila Standard adapter using its browser-cleared first-party REST API."""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from adapters.PHL.common import (
    _close_browser, browser_json, clean, normalize_url, parse_datetime,
)


class Config:
    source_name = "manilastandard.net"
    country_code = "PHL"
    is_env = False


API = "https://manilastandard.net/wp-json/wp/v2/posts"
LANDING = "https://manilastandard.net/"
DENY = (
    "/sports/", "/showbitz/", "/lifestyle/", "/pop-life/", "/pets/", "/search",
    "/biyahero/", "/opinion/", "/gallery/", "/wheels-more-motoring-quarterly/",
    "/motoring", "/holiday-specials/", "/category/", "/archive.html", "/tech/",
)
_POSTS_BY_URL: dict[str, dict] = {}


def _browser_json_with_retry(url: str, *, timeout: int) -> tuple[object, dict]:
    """Retry transient REST denials with a fresh first-party browser session."""
    for attempt in range(5):
        try:
            return browser_json(url, landing_url=LANDING, timeout=timeout)
        except RuntimeError as exc:
            message = str(exc)
            retryable = any(
                f"status={status}" in message for status in (202, 403, 429, 503)
            )
            if not retryable or attempt == 4:
                raise
            _close_browser()
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Manila Standard REST retries exhausted: {url}")


def _api_url(**params: object) -> str:
    return f"{API}?{urlencode(params)}"


def _text_from_html(raw: object) -> str | None:
    soup = BeautifulSoup(str(raw or ""), "html.parser")
    for bad in soup.select(
        "script, style, iframe, form, nav, aside, footer, figure, figcaption, "
        ".ad, .ads, .advertisement, .related, .share, .social, .tags, .newsletter"
    ):
        bad.decompose()
    paragraphs = []
    for node in soup.select("p, h2, h3, li"):
        value = clean(node.get_text(" ", strip=True))
        if value and len(value) >= 20 and value not in paragraphs:
            paragraphs.append(value)
    if not paragraphs:
        value = clean(soup.get_text(" ", strip=True))
        return value if value and len(value) >= 20 else None
    return "\n\n".join(paragraphs)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    pages, result, seen = [], [], set()
    page = 1
    total_pages = None
    while total_pages is None or page <= total_pages:
        url = _api_url(
            after=f"{lower.isoformat()}T00:00:00",
            before=f"{upper.isoformat()}T23:59:59",
            per_page=100,
            page=page,
            orderby="date",
            order="desc",
            _fields="id,date,modified,link,title,content,status",
        )
        payload, headers = _browser_json_with_retry(url, timeout=55)
        if not isinstance(payload, list):
            raise RuntimeError("Manila Standard REST response is not a list")
        if total_pages is None:
            total_pages = int(headers.get("x-wp-totalpages") or 1)
            if total_pages > 120:
                raise RuntimeError(f"Manila Standard REST page count exceeds safety cap: {total_pages}")
        pages.append({"page": page, "rows": len(payload)})
        for row in payload:
            normalized = normalize_url(row.get("link") or "", Config.source_name, DENY)
            if not normalized or normalized in seen:
                continue
            published = parse_datetime(row.get("date"))
            if published and not (lower <= published.date() <= upper):
                continue
            seen.add(normalized)
            result.append(normalized)
            _POSTS_BY_URL[normalized] = row
        page += 1
    evidence.update({
        "strategy": "browser_cleared_wordpress_rest_publication_window",
        "endpoint": API,
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "candidate_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "pages_checked": pages,
        "api_total_pages": total_pages,
        "coverage_complete": True,
        "discovered_urls_after_filter": len(result),
        "blacklist_url_patterns": list(DENY),
    })
    return result


def _load_post(url: str) -> dict:
    row = _POSTS_BY_URL.get(url)
    if row is not None:
        return row
    match = re.search(r"/(\d{6,})/", url)
    if not match:
        raise RuntimeError(f"Manila Standard URL has no WordPress post id: {url}")
    payload, _ = _browser_json_with_retry(
        f"{API}/{match.group(1)}?_fields=id,date,modified,link,title,content,status",
        timeout=45,
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Manila Standard REST post response is invalid: {url}")
    _POSTS_BY_URL[url] = payload
    return payload


def scrape_url(url: str) -> dict:
    row = _load_post(url)
    title = _text_from_html((row.get("title") or {}).get("rendered"))
    published = parse_datetime(row.get("date"))
    modified = parse_datetime(row.get("modified"))
    content = row.get("content") or {}
    body = _text_from_html(content.get("rendered"))
    protected = bool(content.get("protected"))
    errors = []
    if not title:
        errors.append("title_missing")
    if not published:
        errors.append("date_publish_missing")
    if protected:
        errors.append("wordpress_content_protected")
    if not body:
        errors.append("maintext_missing")
    dates = []
    if row.get("date"):
        dates.append({"kind": "published", "source": "wordpress_rest.date", "raw": row["date"]})
    if row.get("modified"):
        dates.append({"kind": "modified", "source": "wordpress_rest.modified", "raw": row["modified"]})
    return {
        "url": url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": "en",
        "parser": "publisher_wordpress_rest_via_browser_clearance",
        "collection": None,
        "parser_error": ",".join(errors) or None,
        "field_sources": {
            "title": "wordpress_rest.title.rendered",
            "date_publish": "wordpress_rest.date",
            "maintext": "wordpress_rest.content.rendered",
        },
        "field_evidence": {
            "titles": ([{"kind": "api_title", "source": "wordpress_rest.title.rendered", "raw": title}]
                       if title else []),
            "dates": dates,
            "bodies": ([{"kind": "wordpress_rest.content.rendered", "text": body[:20000]}]
                       if body else []),
            "wordpress_post_id": row.get("id"),
            "wordpress_status": row.get("status"),
            "wordpress_content_protected": protected,
            "modified_at": modified.isoformat() if modified else None,
            "transport": "selenium_same_origin_fetch",
        },
    }
