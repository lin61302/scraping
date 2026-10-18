"""Pure observer for The Watchers.

Discovery reconciles the publisher's bounded WordPress REST inventory exactly
to the dated URLs in its first-party sitemap.  WordPress ``post.date`` and the
article page's publication metadata are used for ``date_publish``; sitemap
timestamps are never treated as publication dates.  The adapter validates the
article title, canonical URL, visible body, and deterministic English signals.

This module has no database imports, credentials, cache mutation, or
production write path.
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from adapters.INT.common import (
    article_jsonld,
    clean,
    extract_article,
    fetch,
    normalize_url,
    parse_datetime,
    response_sha256,
    sitemap_rows,
    url_set_sha256,
    xml_response,
)


class Config:
    source_name = "watchers.news"
    country_code = "INT"
    is_env = True
    access_block_circuit_threshold = 3


REST = "https://watchers.news/wp-json/wp/v2/posts"
SITEMAP = "https://watchers.news/sitemap_index.xml"
REST_FIELDS = "id,link,date,date_gmt,modified,slug,status,type,title,content"
PATH_DATE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/")
ENGLISH_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for",
    "from", "has", "have", "in", "is", "it", "of", "on", "that",
    "the", "this", "to", "was", "were", "will", "with",
})
_DISCOVERY_ROWS: dict[str, dict] = {}


def _exact_discovery_row(target: str) -> dict:
    """Load one REST identity when the observer runs an exact URL retry."""
    cached = _DISCOVERY_ROWS.get(target)
    if cached:
        return cached
    slug = urlparse(target).path.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        raise RuntimeError("The Watchers exact retry URL lacks a post slug")
    _, rows = _decode_page({
        "slug": slug, "per_page": 1, "page": 1,
        "status": "publish", "_fields": REST_FIELDS,
    })
    if len(rows) != 1 or rows[0]["url"] != target:
        raise RuntimeError("The Watchers exact retry REST identity does not match the requested post")
    _DISCOVERY_ROWS[target] = rows[0]
    return rows[0]


def _rest_url(params: dict) -> str:
    return "%s?%s" % (REST, urlencode(params))


def _rendered_text(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    soup = BeautifulSoup(raw, "html.parser")
    for selector in ("script", "style", "iframe", "form", ".m-a-box", "[class*='share']"):
        for node in soup.select(selector):
            node.decompose()
    parts = []
    for node in soup.select("p, h2, h3"):
        value = clean(node.get_text(" ", strip=True))
        if value and len(value) >= 20:
            parts.append(value)
    return "\n\n".join(parts) or None


def _decode_page(params: dict) -> tuple[object, list[dict]]:
    response = fetch(_rest_url(params))
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("The Watchers REST response is not JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("The Watchers REST response is not a post list")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("The Watchers REST post is malformed")
        url = normalize_url(item.get("link"), {Config.source_name})
        published = parse_datetime(item.get("date"))
        title_payload = item.get("title")
        title = clean(html.unescape(title_payload.get("rendered"))) if isinstance(title_payload, dict) else None
        content_payload = item.get("content")
        rendered = content_payload.get("rendered") if isinstance(content_payload, dict) else None
        body = _rendered_text(rendered)
        if not (
            url and published and title and body and item.get("status") == "publish"
            and item.get("type") == "post" and isinstance(item.get("id"), int)
        ):
            raise RuntimeError("The Watchers REST post lacks identity, title, publication date, or body")
        match = PATH_DATE.search(urlparse(url).path)
        try:
            path_date = date(*(int(value) for value in match.groups())) if match else None
        except ValueError as exc:
            raise RuntimeError("The Watchers URL contains an invalid publication date") from exc
        if path_date != published.date():
            raise RuntimeError("The Watchers URL date and REST publication date disagree")
        rows.append({
            "id": item["id"],
            "url": url,
            "title": title,
            "date_raw": clean(item.get("date")),
            "date_gmt_raw": clean(item.get("date_gmt")),
            "date_publish": published,
            "modified_raw": clean(item.get("modified")),
            "slug": clean(item.get("slug")),
            "rest_body": body,
            "rest_body_chars": len(body),
            "rest_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        })
    return response, rows


def _inventory(start: date, end: date) -> tuple[list[dict], list[dict]]:
    lower = datetime.combine(start, datetime.min.time()) - timedelta(seconds=1)
    upper = datetime.combine(end + timedelta(days=1), datetime.min.time())
    base = {
        "after": lower.isoformat(),
        "before": upper.isoformat(),
        "per_page": 100,
        "page": 1,
        "orderby": "date",
        "order": "asc",
        "status": "publish",
        "_fields": REST_FIELDS,
    }
    records = []
    pages = []
    total = total_pages = None
    page = 1
    while True:
        params = dict(base, page=page)
        response, rows = _decode_page(params)
        header_total = int(response.headers.get("X-WP-Total", -1))
        header_pages = int(response.headers.get("X-WP-TotalPages", -1))
        if header_total < 1 or header_pages < 1 or header_pages > 20:
            raise RuntimeError("The Watchers REST pagination headers are invalid")
        if total is None:
            total, total_pages = header_total, header_pages
        if (header_total, header_pages) != (total, total_pages):
            raise RuntimeError("The Watchers REST pagination changed during discovery")
        pages.append({
            "page": page,
            "url": response.url,
            "rows": len(rows),
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
        })
        records.extend(rows)
        if page >= total_pages:
            break
        page += 1
    if len(records) != total:
        raise RuntimeError("The Watchers REST total does not match returned posts")
    if len({row["id"] for row in records}) != len(records) or len({row["url"] for row in records}) != len(records):
        raise RuntimeError("The Watchers REST inventory contains duplicate posts")
    if any(records[index]["date_publish"] > records[index + 1]["date_publish"] for index in range(len(records) - 1)):
        raise RuntimeError("The Watchers REST inventory is not publication-date ascending")
    if any(not (start <= row["date_publish"].date() <= end) for row in records):
        raise RuntimeError("The Watchers REST returned a post outside the requested interval")
    return records, pages


def _sitemap_inventory(start: date, end: date) -> tuple[list[str], dict]:
    response, soup = xml_response(SITEMAP)
    if soup.find("urlset") is None:
        raise RuntimeError("The Watchers sitemap endpoint is not a URL set")
    selected = []
    for row in sitemap_rows(soup):
        url = normalize_url(row.get("url"), {Config.source_name})
        if not url:
            continue
        match = PATH_DATE.search(urlparse(url).path)
        if not match:
            continue
        try:
            path_date = date(*(int(value) for value in match.groups()))
        except ValueError as exc:
            raise RuntimeError("The Watchers sitemap contains an invalid URL date") from exc
        if start <= path_date <= end:
            selected.append(url)
    if not selected or len(selected) != len(set(selected)):
        raise RuntimeError("The Watchers bounded sitemap inventory is empty or duplicated")
    return selected, {
        "endpoint": response.url,
        "response_sha256": response_sha256(response),
        "response_bytes": len(response.content),
        "bounded_urls": len(selected),
        "bounded_url_set_sha256": url_set_sha256(selected),
    }


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
        raise RuntimeError("The Watchers REST boundary proof is missing")
    return rows[0], {
        "request_url": response.url,
        "response_sha256": response_sha256(response),
        "response_bytes": len(response.content),
    }


def _signature(rows: list[dict]) -> list[tuple[int, str, str, str, str]]:
    return [
        (row["id"], row["url"], row["date_raw"], row["title"], row["rest_body_sha256"])
        for row in rows
    ]


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 180:
        raise ValueError("invalid The Watchers window (max 181 days)")
    sitemap_first, sitemap_first_evidence = _sitemap_inventory(start, end)
    first, first_pages = _inventory(start, end)
    repeat, repeat_pages = _inventory(start, end)
    sitemap_repeat, sitemap_repeat_evidence = _sitemap_inventory(start, end)
    if _signature(first) != _signature(repeat):
        raise RuntimeError("The Watchers REST inventory changed between reads")
    if set(sitemap_first) != set(sitemap_repeat):
        raise RuntimeError("The Watchers bounded sitemap inventory changed between reads")
    rest_urls = [row["url"] for row in first]
    rest_set, sitemap_set = set(rest_urls), set(sitemap_first)
    if rest_set != sitemap_set:
        raise RuntimeError(
            "The Watchers REST and sitemap inventories disagree: rest_only=%d sitemap_only=%d"
            % (len(rest_set - sitemap_set), len(sitemap_set - rest_set))
        )
    prior, prior_response = _boundary(start=start)
    following, following_response = _boundary(end=end)
    if not (prior["date_publish"].date() < start and following["date_publish"].date() > end):
        raise RuntimeError("The Watchers REST inventory did not close both date boundaries")

    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in first})
    month_counts = {}
    for row in first:
        month = row["date_publish"].strftime("%Y-%m")
        month_counts[month] = month_counts.get(month, 0) + 1
    evidence.update({
        "method": "stable_bounded_wordpress_rest_exact_sitemap_reconciliation",
        "rest_endpoint": REST,
        "sitemap_endpoint": SITEMAP,
        "scope": "all publisher WordPress posts in the requested publication-date interval",
        "rest_first_read_pages": first_pages,
        "rest_repeat_read_pages": repeat_pages,
        "rest_inventory_stable": True,
        "sitemap_first_read": sitemap_first_evidence,
        "sitemap_repeat_read": sitemap_repeat_evidence,
        "sitemap_inventory_stable": True,
        "rest_sitemap_exact_match": True,
        "selected": len(rest_urls),
        "selected_month_counts": dict(sorted(month_counts.items())),
        "selected_oldest_publication": first[0]["date_publish"].isoformat(),
        "selected_newest_publication": first[-1]["date_publish"].isoformat(),
        "selected_url_set_sha256": url_set_sha256(rest_urls),
        "boundary_before_start": {
            "id": prior["id"], "url": prior["url"],
            "date_publish": prior["date_publish"].isoformat(), **prior_response,
        },
        "boundary_after_end": {
            "id": following["id"], "url": following["url"],
            "date_publish": following["date_publish"].isoformat(), **following_response,
        },
        "publication_date_source": "WordPress REST post.date, confirmed by article metadata and visible time",
        "sitemap_timestamp_used_as_publication_date": False,
        "coverage_complete": True,
    })
    return rest_urls


def _english_sanity(title: str, body: str) -> dict:
    sample = clean("%s %s" % (title, body[:1200])) or ""
    tokens = re.findall(r"[a-z]+", sample.lower())
    common = sorted({token for token in tokens if token in ENGLISH_WORDS})
    alphabetic = [character for character in sample if character.isalpha()]
    ascii_letters = sum("a" <= character.lower() <= "z" for character in alphabetic)
    ascii_ratio = ascii_letters / len(alphabetic) if alphabetic else 0.0
    return {
        "method": "deterministic_html_lang_plus_ascii_and_common_word_check",
        "sample_chars": len(sample),
        "token_count": len(tokens),
        "common_words": common,
        "common_word_count": len(common),
        "ascii_letter_ratio": round(ascii_ratio, 6),
        "minimum_ascii_letter_ratio": 0.80,
        "mixed_script_quotation_tolerated": 0.80 <= ascii_ratio < 0.90,
        "passed": len(tokens) >= 40 and len(common) >= 5 and ascii_ratio >= 0.80,
    }


def scrape_url(url: str) -> dict:
    target = normalize_url(url, {Config.source_name})
    if not target or not PATH_DATE.search(urlparse(target).path):
        raise RuntimeError("The Watchers article URL is invalid")
    discovered = _exact_discovery_row(target)
    response = fetch(target)
    article, soup = extract_article(
        url=target,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("article h1.entry-title", "h1.entry-title", "main h1"),
        body_selectors=("article .entry-content.single-content", ".entry-content.single-content", ".entry-content"),
        remove_selectors=(
            ".m-a-box", ".molongui-clearfix", ".kt_simple_share_container",
            ".entry-tags", ".post-footer", ".author-box", ".wp-block-buttons",
            "[class*='share']", "[class*='related']", "[class*='newsletter']",
        ),
        parser="watchers_wordpress_page_with_rest_and_sitemap_reconciliation",
        response=response,
    )
    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = normalize_url(canonical_node.get("href"), {Config.source_name}) if canonical_node else None
    if canonical != target:
        raise RuntimeError("The Watchers canonical URL does not match the requested post")
    # A small number of otherwise normal articles omit the itemprop attribute
    # while retaining the same publisher entry-date timestamp.  Require that
    # fallback to agree exactly with Open Graph metadata and WordPress REST.
    visible_node = soup.select_one(
        'time[itemprop="datePublished"][datetime], time.entry-date[datetime]'
    )
    visible_raw = clean(visible_node.get("datetime")) if visible_node else None
    visible_date = parse_datetime(visible_raw)
    if not article.get("date_publish") or not visible_date:
        raise RuntimeError("The Watchers page lacks publication metadata or a visible publication time")
    if not (
        article["date_publish"] == visible_date == discovered["date_publish"]
    ):
        raise RuntimeError("The Watchers REST, metadata, and visible publication dates disagree")
    if clean(html.unescape(article.get("title"))) != discovered["title"]:
        raise RuntimeError("The Watchers REST and article titles disagree")
    maintext = article.get("maintext") or ""
    rest_prefix = clean(discovered["rest_body"].split("\n\n", 1)[0])
    if not rest_prefix or rest_prefix[:140] not in clean(maintext):
        raise RuntimeError("The Watchers visible article body does not match the REST article body")

    html_language = clean((soup.html or {}).get("lang") if soup.html else None)
    jsonld = article_jsonld(soup) or {}
    jsonld_language = clean(jsonld.get("inLanguage"))
    language = (html_language or jsonld_language or "").split("-", 1)[0].lower()
    language_qa = _english_sanity(article["title"], maintext)
    if language != "en" or (jsonld_language and not jsonld_language.lower().startswith("en")) or not language_qa["passed"]:
        article["parser_error"] = "unexpected_or_unverified_language:%s" % (language or "missing")
    article["language"] = language
    article["field_evidence"].update({
        "canonical_url": canonical,
        "html_language": html_language,
        "jsonld_language": jsonld_language,
        "visible_published_date": visible_raw,
        "language_qa": language_qa,
        "wordpress_rest_post": {
            "id": discovered["id"],
            "date": discovered["date_raw"],
            "date_gmt": discovered["date_gmt_raw"],
            "modified": discovered["modified_raw"],
            "title": discovered["title"],
            "body_chars": discovered["rest_body_chars"],
            "body_sha256": discovered["rest_body_sha256"],
        },
        "rest_body_prefix_confirmed_on_page": True,
        "sitemap_timestamp_used_as_publication_date": False,
    })
    article["field_sources"]["date_publish"] = (
        "article metadata and visible time confirmed exactly by WordPress REST post.date"
    )
    article["field_sources"]["language"] = (
        "html[lang], optional JSON-LD inLanguage, and deterministic text sanity check"
    )
    return article
