"""Pure observer for Balkan Green Energy News.

Discovery uses the publisher's WordPress REST posts endpoint because its XML
``lastmod`` values are modification timestamps and cannot establish
publication dates.  The bounded REST inventory is read twice, must remain
stable, and is closed with one publisher-dated post on each side of the
requested interval.  Article pages remain authoritative for the retained
title, publication date, main text, and language.

This module has no database imports, credentials, cache mutation, or
production write path.
"""
from __future__ import annotations

import html
from datetime import date
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from adapters.INT.common import (
    clean,
    extract_article,
    fetch,
    normalize_url,
    parse_datetime,
    response_sha256,
    url_set_sha256,
)


class Config:
    source_name = "balkangreenenergynews.com"
    country_code = "INT"
    is_env = True
    access_block_circuit_threshold = 3


REST = "https://balkangreenenergynews.com/wp-json/wp/v2/posts"
REST_FIELDS = "id,link,date,modified,slug,status,type,title"
_DISCOVERY_ROWS: dict[str, dict] = {}


def _rest_url(params: dict) -> str:
    return "%s?%s" % (REST, urlencode(params))


def _decode_page(params: dict) -> tuple[object, list[dict]]:
    response = fetch(_rest_url(params))
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Balkan Green Energy News REST response is not JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Balkan Green Energy News REST response is not a post list")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("Balkan Green Energy News REST post is malformed")
        url = normalize_url(item.get("link"), {Config.source_name})
        published = parse_datetime(item.get("date"))
        title_payload = item.get("title")
        title = clean(html.unescape(title_payload.get("rendered"))) if isinstance(title_payload, dict) else None
        if not (
            url and published and title and item.get("status") == "publish"
            and item.get("type") == "post" and isinstance(item.get("id"), int)
        ):
            raise RuntimeError("Balkan Green Energy News REST post lacks identity, title, or publication date")
        rows.append({
            "id": item["id"],
            "url": url,
            "title": title,
            "date_raw": clean(item.get("date")),
            "date_publish": published,
            "modified_raw": clean(item.get("modified")),
            "slug": clean(item.get("slug")),
        })
    return response, rows


def _inventory(start: date, end: date) -> tuple[list[dict], list[dict]]:
    base = {
        "after": "%sT00:00:00" % start.isoformat(),
        "before": "%sT23:59:59" % end.isoformat(),
        "per_page": 100,
        "page": 1,
        "orderby": "date",
        "order": "asc",
        "status": "publish",
        "_fields": REST_FIELDS,
    }
    # WordPress ``after`` is exclusive; moving one second below midnight
    # retains every post published on the requested first day.
    from datetime import datetime, timedelta
    lower = datetime.combine(start, datetime.min.time()) - timedelta(seconds=1)
    base["after"] = lower.isoformat()

    records = []
    pages = []
    total = total_pages = None
    page = 1
    while True:
        params = dict(base, page=page)
        response, rows = _decode_page(params)
        header_total = int(response.headers.get("X-WP-Total", -1))
        header_pages = int(response.headers.get("X-WP-TotalPages", -1))
        if header_total < 0 or header_pages < 1 or header_pages > 20:
            raise RuntimeError("Balkan Green Energy News REST pagination headers are invalid")
        if total is None:
            total, total_pages = header_total, header_pages
        if (header_total, header_pages) != (total, total_pages):
            raise RuntimeError("Balkan Green Energy News REST pagination changed during discovery")
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
        raise RuntimeError("Balkan Green Energy News REST total does not match returned posts")
    if len({row["id"] for row in records}) != len(records) or len({row["url"] for row in records}) != len(records):
        raise RuntimeError("Balkan Green Energy News REST inventory contains duplicate posts")
    if any(records[index]["date_publish"] > records[index + 1]["date_publish"] for index in range(len(records) - 1)):
        raise RuntimeError("Balkan Green Energy News REST inventory is not publication-date ascending")
    if any(not (start <= row["date_publish"].date() <= end) for row in records):
        raise RuntimeError("Balkan Green Energy News REST returned a post outside the requested interval")
    return records, pages


def _boundary(*, start: date | None = None, end: date | None = None) -> tuple[dict, dict]:
    params = {
        "per_page": 1,
        "page": 1,
        "status": "publish",
        "_fields": REST_FIELDS,
    }
    if start is not None:
        params.update({"before": "%sT00:00:00" % start.isoformat(), "orderby": "date", "order": "desc"})
    elif end is not None:
        params.update({"after": "%sT23:59:59" % end.isoformat(), "orderby": "date", "order": "asc"})
    else:
        raise ValueError("one boundary is required")
    response, rows = _decode_page(params)
    if len(rows) != 1:
        raise RuntimeError("Balkan Green Energy News REST boundary proof is missing")
    return rows[0], {
        "request_url": response.url,
        "response_sha256": response_sha256(response),
        "response_bytes": len(response.content),
    }


def _signature(rows: list[dict]) -> list[tuple[int, str, str, str]]:
    return [(row["id"], row["url"], row["date_raw"], row["title"]) for row in rows]


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 180:
        raise ValueError("invalid Balkan Green Energy News window (max 181 days)")

    first, first_pages = _inventory(start, end)
    repeat, repeat_pages = _inventory(start, end)
    stable = _signature(first) == _signature(repeat)
    if not stable:
        raise RuntimeError("Balkan Green Energy News REST inventory changed between reads")
    prior, prior_response = _boundary(start=start)
    following, following_response = _boundary(end=end)
    if not (prior["date_publish"].date() < start and following["date_publish"].date() > end):
        raise RuntimeError("Balkan Green Energy News REST inventory did not close both date boundaries")
    if not first:
        raise RuntimeError("Balkan Green Energy News interval contains no publisher-dated posts")

    selected = [row["url"] for row in first]
    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in first})
    month_counts = {}
    for row in first:
        month = row["date_publish"].strftime("%Y-%m")
        month_counts[month] = month_counts.get(month, 0) + 1
    evidence.update({
        "method": "stable_bounded_wordpress_rest_publication_inventory",
        "endpoint": REST,
        "scope": "all publisher posts, matching the current legacy post-sitemap scope",
        "first_read_pages": first_pages,
        "repeat_read_pages": repeat_pages,
        "inventory_stable": stable,
        "inventory_order": "WordPress post.date ascending",
        "selected": len(selected),
        "selected_month_counts": dict(sorted(month_counts.items())),
        "selected_oldest_publication": first[0]["date_publish"].date().isoformat(),
        "selected_newest_publication": first[-1]["date_publish"].date().isoformat(),
        "selected_url_set_sha256": url_set_sha256(selected),
        "boundary_before_start": {
            "id": prior["id"], "url": prior["url"],
            "date_publish": prior["date_publish"].date().isoformat(), **prior_response,
        },
        "boundary_after_end": {
            "id": following["id"], "url": following["url"],
            "date_publish": following["date_publish"].date().isoformat(), **following_response,
        },
        "publication_date_source": "WordPress REST post.date, confirmed on article pages",
        "sitemap_lastmod_used_as_publication_date": False,
        "coverage_complete": True,
    })
    return selected


def _visible_published_dates(soup: BeautifulSoup) -> list[tuple[object, str]]:
    values = []
    for item in soup.select(".single-news-sidebar-item"):
        label = item.select_one(".title")
        raw_node = item.select_one(".text")
        label_text = clean(label.get_text(" ", strip=True)) if label else None
        if label_text != "Published":
            continue
        raw = clean(raw_node.get_text(" ", strip=True)) if raw_node else None
        parsed = parse_datetime(raw)
        if parsed and raw and all(existing[1] != raw for existing in values):
            values.append((parsed, raw))
    return values


def scrape_url(url: str) -> dict:
    target = normalize_url(url, {Config.source_name})
    if not target or urlparse(target).path in {"", "/", "/latest/"}:
        raise RuntimeError("Balkan Green Energy News article URL is invalid")
    response = fetch(target)
    article, soup = extract_article(
        url=target,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("main .single-news h1", ".single-news h1", "main h1"),
        body_selectors=("main .single-news-content", ".single-news-content"),
        remove_selectors=(
            ".related-news", ".related", ".newsletter-box", ".banner",
            ".wp-block-buttons", "[class*='related']", "[class*='newsletter']",
        ),
        parser="bgen_jsonld_published_visible_article_body",
        response=response,
    )
    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = normalize_url(canonical_node.get("href"), {Config.source_name}) if canonical_node else None
    if canonical != target:
        raise RuntimeError("Balkan Green Energy News canonical URL does not match the requested post")

    visible_dates = _visible_published_dates(soup)
    if len(visible_dates) != 1 or not article.get("date_publish"):
        raise RuntimeError("Balkan Green Energy News page lacks one visible publication date")
    if visible_dates[0][0].date() != article["date_publish"].date():
        raise RuntimeError("Balkan Green Energy News visible and JSON-LD publication dates disagree")
    discovered = _DISCOVERY_ROWS.get(target)
    if discovered and discovered["date_publish"].date() != article["date_publish"].date():
        raise RuntimeError("Balkan Green Energy News REST and page publication dates disagree")

    language = clean((soup.html or {}).get("lang") if soup.html else None)
    article["language"] = (language or "").split("-", 1)[0].lower()
    if article["language"] != "en":
        article["parser_error"] = "unexpected_language:%s" % article["language"]
    article["field_evidence"].update({
        "canonical_url": canonical,
        "html_language": language,
        "visible_published_date": visible_dates[0][1],
        "wordpress_rest_post": ({
            "id": discovered["id"],
            "date": discovered["date_raw"],
            "modified": discovered["modified_raw"],
            "title": discovered["title"],
        } if discovered else None),
        "sitemap_lastmod_used_as_publication_date": False,
    })
    article["field_sources"]["date_publish"] = "NewsArticle JSON-LD datePublished confirmed by visible Published label and WordPress REST post.date"
    article["field_sources"]["language"] = "html[lang]"
    return article
