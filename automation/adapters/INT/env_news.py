"""Pure network observer for the Arabic environmental outlet env-news.com.

Discovery reads the bounded WordPress REST publication inventory twice, closes
the requested date boundaries, and verifies that every selected URL is present
in the publisher's six post sitemaps. Sitemap ``lastmod`` values are inventory
evidence only and are never used as publication dates.

This module has no database imports, credentials, cache mutation, or
production write path.
"""
from __future__ import annotations

import hashlib
import html
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

from adapters.INT.common import (
    clean,
    extract_article,
    normalize_url,
    parse_datetime,
    response_sha256,
    sitemap_rows,
    url_set_sha256,
)


class Config:
    source_name = "env-news.com"
    country_code = "INT"
    is_env = True
    access_block_circuit_threshold = 3


REST = "https://www.env-news.com/wp-json/wp/v2/posts"
REST_FIELDS = "id,link,date,date_gmt,modified,slug,status,type,title,content"
SITEMAPS = ["https://www.env-news.com/post-sitemap.xml"] + [
    f"https://www.env-news.com/post-sitemap{index}.xml" for index in range(2, 7)
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.7,en;q=0.3",
    "Referer": "https://www.google.com/",
}
_LOCAL = threading.local()
_DISCOVERY_ROWS: dict[str, dict] = {}


def _url_identity(raw: str):
    """Compare publisher URLs after decoding their equivalent UTF-8 paths."""
    normalized = normalize_url(raw, {Config.source_name})
    if not normalized:
        return None
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unicodedata.normalize("NFC", unquote(parsed.path)).rstrip("/")
    return host, path


def _exact_discovery_row(target: str) -> dict:
    """Load one publisher REST row for an exact targeted retry.

    Normal source observations populate ``_DISCOVERY_ROWS`` during bounded
    discovery.  The observer's ``--urls-file`` repair mode deliberately skips
    that source-wide step, so recover only the numeric WordPress post named by
    the reviewed URL and prove its publisher link has the same decoded path.
    """
    cached = _DISCOVERY_ROWS.get(target)
    if cached:
        return cached
    match = re.search(r"/(\d+)/[^/]+\.html$", urlparse(target).path, re.I)
    if not match:
        raise RuntimeError("env-news exact retry URL lacks a WordPress post id")
    _, rows = _decode_page({
        "include": int(match.group(1)), "per_page": 1, "page": 1,
        "status": "publish", "_fields": REST_FIELDS,
    })
    if len(rows) != 1 or _url_identity(rows[0]["url"]) != _url_identity(target):
        raise RuntimeError("env-news exact retry REST identity does not match the requested post")
    _DISCOVERY_ROWS[target] = rows[0]
    return rows[0]


def _session() -> requests.Session:
    session = getattr(_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _LOCAL.session = session
    return session


def _fetch(url: str) -> requests.Response:
    errors = []
    for attempt in range(3):
        try:
            response = _session().get(url, timeout=45)
            response.raise_for_status()
            if not response.content:
                raise ValueError("empty response")
            response.scrape_transport = "requests_browser_headers"
            return response
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"env-news fetch failed for {url}: {'; '.join(errors)}")


def _rest_url(params: dict) -> str:
    return f"{REST}?{urlencode(params)}"


def _rendered_text(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup.select("script, style, iframe, form, .sharedaddy, [class*='share'], [class*='related']"):
        node.decompose()
    parts = []
    for node in soup.select("p, h2, h3"):
        value = clean(node.get_text(" ", strip=True))
        if value and len(value) >= 20:
            parts.append(value)
    return "\n\n".join(parts) or None


def _decode_page(params: dict) -> tuple[requests.Response, list[dict]]:
    response = _fetch(_rest_url(params))
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("env-news REST response is not JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("env-news REST response is not a post list")
    rows = []
    for item in payload:
        title_payload = item.get("title")
        content_payload = item.get("content")
        title = clean(html.unescape(title_payload.get("rendered"))) if isinstance(title_payload, dict) else None
        body = _rendered_text(content_payload.get("rendered")) if isinstance(content_payload, dict) else None
        url = normalize_url(item.get("link"), {Config.source_name})
        published = parse_datetime(item.get("date"))
        if not (
            url and published and title and body and isinstance(item.get("id"), int)
            and item.get("status") == "publish" and item.get("type") == "post"
        ):
            raise RuntimeError("env-news REST post lacks identity, title, publication date, or body")
        rows.append({
            "id": item["id"], "url": url, "title": title, "date_publish": published,
            "date_raw": clean(item.get("date")), "date_gmt_raw": clean(item.get("date_gmt")),
            "modified_raw": clean(item.get("modified")), "slug": clean(item.get("slug")),
            "rest_body": body, "rest_body_chars": len(body),
            "rest_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        })
    return response, rows


def _inventory(start: date, end: date) -> tuple[list[dict], list[dict]]:
    lower = datetime.combine(start, datetime.min.time()) - timedelta(seconds=1)
    upper = datetime.combine(end + timedelta(days=1), datetime.min.time())
    base = {
        "after": lower.isoformat(), "before": upper.isoformat(), "per_page": 100,
        "page": 1, "orderby": "date", "order": "asc", "status": "publish",
        "_fields": REST_FIELDS,
    }
    records, pages = [], []
    total = total_pages = None
    page = 1
    while True:
        response, rows = _decode_page(dict(base, page=page))
        header_total = int(response.headers.get("X-WP-Total", -1))
        header_pages = int(response.headers.get("X-WP-TotalPages", -1))
        if header_total < 1 or header_pages < 1 or header_pages > 10:
            raise RuntimeError("env-news REST pagination headers are invalid")
        if total is None:
            total, total_pages = header_total, header_pages
        if (header_total, header_pages) != (total, total_pages):
            raise RuntimeError("env-news REST pagination changed during discovery")
        pages.append({
            "page": page, "url": response.url, "rows": len(rows),
            "response_sha256": response_sha256(response), "response_bytes": len(response.content),
        })
        records.extend(rows)
        if page >= total_pages:
            break
        page += 1
    if len(records) != total:
        raise RuntimeError("env-news REST total does not match returned posts")
    if len({row["id"] for row in records}) != len(records) or len({row["url"] for row in records}) != len(records):
        raise RuntimeError("env-news REST inventory contains duplicate posts")
    if any(not (start <= row["date_publish"].date() <= end) for row in records):
        raise RuntimeError("env-news REST returned a post outside the requested interval")
    return records, pages


def _boundary(*, start: date | None = None, end: date | None = None) -> tuple[dict, dict]:
    params = {"per_page": 1, "page": 1, "status": "publish", "_fields": REST_FIELDS}
    if start is not None:
        params.update({"before": datetime.combine(start, datetime.min.time()).isoformat(), "orderby": "date", "order": "desc"})
    elif end is not None:
        after = datetime.combine(end + timedelta(days=1), datetime.min.time()) - timedelta(seconds=1)
        params.update({"after": after.isoformat(), "orderby": "date", "order": "asc"})
    else:
        raise ValueError("one boundary is required")
    response, rows = _decode_page(params)
    if len(rows) != 1:
        raise RuntimeError("env-news REST boundary proof is missing")
    return rows[0], {"request_url": response.url, "response_sha256": response_sha256(response), "response_bytes": len(response.content)}


def _sitemap_inventory() -> tuple[set[str], list[dict]]:
    selected, evidence = set(), []
    for endpoint in SITEMAPS:
        response = _fetch(endpoint)
        soup = BeautifulSoup(response.content, "xml")
        if soup.find("urlset") is None:
            raise RuntimeError(f"env-news sitemap is not a URL set: {endpoint}")
        urls = []
        for row in sitemap_rows(soup):
            url = normalize_url(row.get("url"), {Config.source_name})
            if url:
                urls.append(url)
                selected.add(url)
        evidence.append({
            "endpoint": response.url, "urls": len(urls),
            "response_sha256": response_sha256(response), "response_bytes": len(response.content),
        })
    return selected, evidence


def _signature(rows: list[dict]) -> list[tuple]:
    return [(row["id"], row["url"], row["date_raw"], row["title"], row["rest_body_sha256"]) for row in rows]


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 240:
        raise ValueError("invalid env-news window (max 241 days)")
    first, first_pages = _inventory(start, end)
    repeat, repeat_pages = _inventory(start, end)
    if _signature(first) != _signature(repeat):
        raise RuntimeError("env-news REST inventory changed between reads")
    sitemap_urls, sitemap_evidence = _sitemap_inventory()
    selected = [row["url"] for row in first]
    missing = sorted(set(selected) - sitemap_urls)
    if missing:
        raise RuntimeError(f"env-news REST URLs missing from post sitemaps: {len(missing)}")
    prior, prior_response = _boundary(start=start)
    following, following_response = _boundary(end=end)
    if not (prior["date_publish"].date() < start and following["date_publish"].date() > end):
        raise RuntimeError("env-news REST inventory did not close both date boundaries")
    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in first})
    month_counts = {}
    for row in first:
        month = row["date_publish"].strftime("%Y-%m")
        month_counts[month] = month_counts.get(month, 0) + 1
    evidence.update({
        "method": "stable_bounded_wordpress_rest_with_sitemap_membership",
        "rest_endpoint": REST, "sitemap_endpoints": SITEMAPS,
        "scope": "all publisher WordPress posts in the requested publication-date interval",
        "rest_first_read_pages": first_pages, "rest_repeat_read_pages": repeat_pages,
        "rest_inventory_stable": True, "sitemap_inventory": sitemap_evidence,
        "selected_in_sitemaps": True, "selected": len(selected),
        "selected_month_counts": dict(sorted(month_counts.items())),
        "selected_oldest_publication": first[0]["date_publish"].isoformat(),
        "selected_newest_publication": first[-1]["date_publish"].isoformat(),
        "selected_url_set_sha256": url_set_sha256(selected),
        "full_sitemap_url_set_sha256": url_set_sha256(sorted(sitemap_urls)),
        "boundary_before_start": {"id": prior["id"], "url": prior["url"], "date_publish": prior["date_publish"].isoformat(), **prior_response},
        "boundary_after_end": {"id": following["id"], "url": following["url"], "date_publish": following["date_publish"].isoformat(), **following_response},
        "publication_date_source": "WordPress REST post.date confirmed by article metadata and visible time",
        "sitemap_lastmod_used_as_publication_date": False, "coverage_complete": True,
    })
    return selected


def _arabic_sanity(title: str, body: str) -> dict:
    sample = clean(f"{title} {body[:1600]}") or ""
    letters = [character for character in sample if character.isalpha()]
    arabic = sum(bool(re.match(r"[\u0600-\u06ff]", character)) for character in letters)
    ratio = arabic / len(letters) if letters else 0.0
    return {"method": "html_lang_plus_arabic_script_ratio", "sample_chars": len(sample), "arabic_letter_ratio": round(ratio, 6), "passed": len(sample) >= 200 and ratio >= 0.55}


def scrape_url(url: str) -> dict:
    target = normalize_url(url, {Config.source_name})
    if not target:
        raise RuntimeError("env-news article is outside the reconciled discovery inventory")
    discovered = _exact_discovery_row(target)
    response = _fetch(target)
    article, soup = extract_article(
        url=target, domain=Config.source_name, country_code=Config.country_code,
        title_selectors=("h1.entry-title", "article h1", "main h1"),
        body_selectors=(".td-post-content", "article .td-post-content"),
        remove_selectors=(".td-a-rec", ".sharedaddy", "[class*='share']", "[class*='related']"),
        parser="env_news_wordpress_page_rest_sitemap_reconciled", response=response,
    )
    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = normalize_url(canonical_node.get("href"), {Config.source_name}) if canonical_node else None
    if _url_identity(canonical or "") != _url_identity(target):
        raise RuntimeError("env-news canonical URL does not match the requested post")
    metadata_date = article.get("date_publish")
    if not metadata_date or abs((discovered["date_publish"] - metadata_date).total_seconds()) > 6 * 3600:
        raise RuntimeError("env-news REST and UTC article publication timestamps disagree")
    if clean(html.unescape(article.get("title"))) != discovered["title"]:
        raise RuntimeError("env-news REST and article titles disagree")
    visible_node = soup.select_one("article time.entry-date[datetime], time.entry-date[datetime]")
    visible_raw = clean(visible_node.get("datetime")) if visible_node else None
    visible_date = parse_datetime(visible_raw)
    if not visible_date or visible_date.date() != discovered["date_publish"].date():
        raise RuntimeError("env-news visible and REST publication dates disagree")
    # The publisher REST and visible time use UTC+03 while the Open Graph value
    # is UTC. Retain the publisher-local publication timestamp so midnight
    # crossings are assigned to the displayed publication day.
    article["date_publish"] = discovered["date_publish"]
    article["field_evidence"].setdefault("dates", []).extend([
        {"kind": "visible_published", "source": "time.entry-date", "raw": visible_raw},
        {"kind": "wordpress_rest_published", "source": "wp-json post.date", "raw": discovered["date_raw"]},
    ])
    maintext = article.get("maintext") or ""
    rest_prefix = clean(discovered["rest_body"].split("\n\n", 1)[0])
    if not rest_prefix or rest_prefix[:100] not in clean(maintext):
        raise RuntimeError("env-news visible body does not match REST content")
    html_language = clean((soup.html or {}).get("lang") if soup.html else None)
    language_qa = _arabic_sanity(article["title"], maintext)
    article["language"] = "ar"
    if not (html_language or "").lower().startswith("ar") or not language_qa["passed"]:
        article["parser_error"] = f"unexpected_or_unverified_language:{html_language or 'missing'}"
    article["field_evidence"].update({
        "canonical_url": canonical, "html_language": html_language,
        "visible_published_date": visible_raw, "language_qa": language_qa,
        "wordpress_rest_post": {
            "id": discovered["id"], "date": discovered["date_raw"], "date_gmt": discovered["date_gmt_raw"],
            "modified": discovered["modified_raw"], "title": discovered["title"],
            "body_chars": discovered["rest_body_chars"], "body_sha256": discovered["rest_body_sha256"],
        },
        "rest_body_prefix_confirmed_on_page": True,
        "sitemap_lastmod_used_as_publication_date": False,
    })
    article["field_sources"]["date_publish"] = "article metadata and visible time confirmed by WordPress REST post.date"
    article["field_sources"]["language"] = "html[lang] plus deterministic Arabic script ratio"
    return article
