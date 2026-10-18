"""Pure bounded observer for the Spanish outlet noticiasdelatierra.com.

The publisher reorganized its category taxonomy in 2026.  Discovery therefore
uses the WordPress REST post inventory and proves that every selected post
resolves to one or more of the four current high-level environmental sections.
This avoids the heavy duplication produced by walking overlapping category
archives.  WordPress ``post.date`` and the visible Spanish publication date
establish the local publication day; modification times are evidence only.

This module has no database imports, credentials, cache mutation, or
production write path.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urlencode, urlparse

import dateparser
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
    source_name = "noticiasdelatierra.com"
    country_code = "INT"
    is_env = True
    access_block_circuit_threshold = 3


REST_ROOT = "https://noticiasdelatierra.com/wp-json/wp/v2"
POSTS_REST = f"{REST_ROOT}/posts"
CATEGORIES_REST = f"{REST_ROOT}/categories"
POST_FIELDS = "id,link,date,date_gmt,modified,slug,status,type,title,categories"
CATEGORY_FIELDS = "id,slug,name,parent,count,link"
EXPECTED_ROOT_SLUGS = {
    "ciencia-y-tierra",
    "clima-y-planeta",
    "naturaleza-y-biodiversidad",
    "politica-y-sociedad",
}
SPANISH_COMMON_WORDS = {
    "al", "como", "con", "de", "del", "el", "en", "es", "esta", "este",
    "ha", "la", "las", "los", "más", "para", "por", "que", "se", "su",
    "sus", "un", "una", "y",
}
_DISCOVERY_ROWS: dict[str, dict] = {}
_TAXONOMY: dict | None = None


def _rest_url(endpoint: str, params: dict) -> str:
    return "%s?%s" % (endpoint, urlencode(params))


def _url_identity(raw: str):
    normalized = normalize_url(raw, {Config.source_name})
    if not normalized:
        return None
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, unquote(parsed.path).rstrip("/")


def _fetch_json(endpoint: str, params: dict) -> tuple[object, object]:
    response = fetch(_rest_url(endpoint, params))
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Noticias de la Tierra REST response is not JSON") from exc
    return response, payload


def _load_taxonomy(*, refresh: bool = False) -> dict:
    global _TAXONOMY
    if _TAXONOMY is not None and not refresh:
        return _TAXONOMY

    rows, pages = [], []
    page = 1
    expected_total = expected_pages = None
    while True:
        response, payload = _fetch_json(CATEGORIES_REST, {
            "per_page": 100,
            "page": page,
            "orderby": "id",
            "order": "asc",
            "hide_empty": "false",
            "_fields": CATEGORY_FIELDS,
        })
        if not isinstance(payload, list):
            raise RuntimeError("Noticias de la Tierra category response is not a list")
        total = int(response.headers.get("X-WP-Total", -1))
        total_pages = int(response.headers.get("X-WP-TotalPages", -1))
        if total < 1 or total_pages < 1 or total_pages > 5:
            raise RuntimeError("Noticias de la Tierra category pagination is invalid")
        if expected_total is None:
            expected_total, expected_pages = total, total_pages
        if (total, total_pages) != (expected_total, expected_pages):
            raise RuntimeError("Noticias de la Tierra category pagination changed during discovery")
        pages.append({
            "page": page,
            "rows": len(payload),
            "request_url": response.url,
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
        })
        rows.extend(payload)
        if page >= total_pages:
            break
        page += 1

    if len(rows) != expected_total:
        raise RuntimeError("Noticias de la Tierra category total does not match returned rows")
    by_id = {}
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            raise RuntimeError("Noticias de la Tierra category row is malformed")
        row = {
            "id": item["id"],
            "slug": clean(item.get("slug")),
            "name": clean(html.unescape(item.get("name") or "")),
            "parent": item.get("parent"),
            "count": item.get("count"),
            "link": clean(item.get("link")),
        }
        if not row["slug"] or not isinstance(row["parent"], int) or row["id"] in by_id:
            raise RuntimeError("Noticias de la Tierra category identity is invalid")
        by_id[row["id"]] = row

    def root_id(category_id: int) -> int:
        current = category_id
        seen = set()
        while True:
            if current in seen or current not in by_id:
                raise RuntimeError("Noticias de la Tierra category ancestry is cyclic or incomplete")
            seen.add(current)
            parent = by_id[current]["parent"]
            if parent == 0:
                return current
            current = parent

    root_by_id = {category_id: root_id(category_id) for category_id in by_id}
    expected_root_ids = {
        category_id for category_id, item in by_id.items()
        if item["parent"] == 0 and item["slug"] in EXPECTED_ROOT_SLUGS
    }
    if {by_id[item]["slug"] for item in expected_root_ids} != EXPECTED_ROOT_SLUGS:
        raise RuntimeError("Noticias de la Tierra expected environmental root taxonomy is incomplete")
    unexpected_roots = [
        item for item in by_id.values()
        if item["parent"] == 0 and item["slug"] not in EXPECTED_ROOT_SLUGS | {"uncategorized"}
    ]
    if unexpected_roots:
        raise RuntimeError("Noticias de la Tierra has an unreviewed top-level category")

    taxonomy_payload = [
        {key: item[key] for key in ("id", "slug", "name", "parent", "count")}
        for item in sorted(by_id.values(), key=lambda row: row["id"])
    ]
    _TAXONOMY = {
        "by_id": by_id,
        "root_by_id": root_by_id,
        "expected_root_ids": expected_root_ids,
        "pages": pages,
        "category_count": len(by_id),
        "taxonomy_sha256": hashlib.sha256(
            json.dumps(taxonomy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    return _TAXONOMY


def _decode_posts(params: dict, taxonomy: dict) -> tuple[object, list[dict]]:
    response, payload = _fetch_json(POSTS_REST, params)
    if not isinstance(payload, list):
        raise RuntimeError("Noticias de la Tierra post response is not a list")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("Noticias de la Tierra REST post is malformed")
        url = normalize_url(item.get("link"), {Config.source_name})
        published = parse_datetime(item.get("date"))
        title_payload = item.get("title")
        title = clean(html.unescape(title_payload.get("rendered"))) if isinstance(title_payload, dict) else None
        categories = item.get("categories")
        if not isinstance(categories, list) or not categories or any(not isinstance(value, int) for value in categories):
            raise RuntimeError("Noticias de la Tierra REST post lacks category identity")
        unknown = sorted(set(categories) - set(taxonomy["by_id"]))
        if unknown:
            raise RuntimeError("Noticias de la Tierra REST post uses an unknown category")
        roots = {taxonomy["root_by_id"][value] for value in categories}
        if not roots or not roots.issubset(taxonomy["expected_root_ids"]):
            raise RuntimeError("Noticias de la Tierra REST post is outside the reviewed environmental taxonomy")
        if not (
            url and published and title and isinstance(item.get("id"), int)
            and item.get("status") == "publish" and item.get("type") == "post"
        ):
            raise RuntimeError("Noticias de la Tierra REST post lacks identity, title, or publication date")
        rows.append({
            "id": item["id"],
            "url": url,
            "title": title,
            "date_raw": clean(item.get("date")),
            "date_gmt_raw": clean(item.get("date_gmt")),
            "date_publish": published,
            "modified_raw": clean(item.get("modified")),
            "slug": clean(item.get("slug")),
            "category_ids": sorted(set(categories)),
            "category_root_slugs": sorted(taxonomy["by_id"][value]["slug"] for value in roots),
        })
    return response, rows


def _inventory(start: date, end: date, taxonomy: dict) -> tuple[list[dict], list[dict]]:
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
        "_fields": POST_FIELDS,
    }
    rows, pages = [], []
    expected_total = expected_pages = None
    page = 1
    while True:
        response, decoded = _decode_posts(dict(base, page=page), taxonomy)
        total = int(response.headers.get("X-WP-Total", -1))
        total_pages = int(response.headers.get("X-WP-TotalPages", -1))
        if total < 0 or total_pages < 1 or total_pages > 30:
            raise RuntimeError("Noticias de la Tierra REST pagination is invalid")
        if expected_total is None:
            expected_total, expected_pages = total, total_pages
        if (total, total_pages) != (expected_total, expected_pages):
            raise RuntimeError("Noticias de la Tierra REST pagination changed during discovery")
        pages.append({
            "page": page,
            "rows": len(decoded),
            "request_url": response.url,
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
        })
        rows.extend(decoded)
        if page >= total_pages:
            break
        page += 1

    if len(rows) != expected_total:
        raise RuntimeError("Noticias de la Tierra REST total does not match returned posts")
    if len({row["id"] for row in rows}) != len(rows) or len({_url_identity(row["url"]) for row in rows}) != len(rows):
        raise RuntimeError("Noticias de la Tierra REST inventory contains duplicate posts")
    if any(rows[index]["date_publish"] > rows[index + 1]["date_publish"] for index in range(len(rows) - 1)):
        raise RuntimeError("Noticias de la Tierra REST inventory is not publication-date ascending")
    if any(not (start <= row["date_publish"].date() <= end) for row in rows):
        raise RuntimeError("Noticias de la Tierra REST returned a post outside the requested interval")
    return rows, pages


def _boundary(taxonomy: dict, *, start: date | None = None, end: date | None = None) -> tuple[dict, dict]:
    params = {"per_page": 1, "page": 1, "status": "publish", "_fields": POST_FIELDS}
    if start is not None:
        params.update({"before": "%sT00:00:00" % start.isoformat(), "orderby": "date", "order": "desc"})
    elif end is not None:
        params.update({"after": "%sT23:59:59" % end.isoformat(), "orderby": "date", "order": "asc"})
    else:
        raise ValueError("one boundary is required")
    response, rows = _decode_posts(params, taxonomy)
    if len(rows) != 1:
        raise RuntimeError("Noticias de la Tierra REST boundary proof is missing")
    return rows[0], {
        "request_url": response.url,
        "response_sha256": response_sha256(response),
        "response_bytes": len(response.content),
    }


def _signature(rows: list[dict]) -> list[tuple]:
    return [
        (row["id"], row["url"], row["date_raw"], row["title"], tuple(row["category_ids"]))
        for row in rows
    ]


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 200:
        raise ValueError("invalid Noticias de la Tierra window (max 201 days)")
    taxonomy = _load_taxonomy(refresh=True)
    first, first_pages = _inventory(start, end, taxonomy)
    repeat, repeat_pages = _inventory(start, end, taxonomy)
    stable = _signature(first) == _signature(repeat)
    if not stable:
        raise RuntimeError("Noticias de la Tierra REST inventory changed between reads")
    if not first:
        raise RuntimeError("Noticias de la Tierra interval contains no publisher-dated posts")
    prior, prior_response = _boundary(taxonomy, start=start)
    following, following_response = _boundary(taxonomy, end=end)
    if not (prior["date_publish"].date() < start and following["date_publish"].date() > end):
        raise RuntimeError("Noticias de la Tierra REST inventory did not close both date boundaries")

    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in first})
    selected = [row["url"] for row in first]
    month_counts = Counter(row["date_publish"].strftime("%Y-%m") for row in first)
    category_counts = Counter(category_id for row in first for category_id in row["category_ids"])
    root_counts = Counter(slug for row in first for slug in row["category_root_slugs"])
    evidence.update({
        "method": "stable_bounded_wordpress_rest_inventory_with_taxonomy_reconciliation",
        "endpoint": POSTS_REST,
        "scope": "all publisher posts whose categories resolve to the four reviewed environmental roots",
        "taxonomy_endpoint": CATEGORIES_REST,
        "taxonomy_sha256": taxonomy["taxonomy_sha256"],
        "taxonomy_category_count": taxonomy["category_count"],
        "taxonomy_pages": taxonomy["pages"],
        "allowed_root_slugs": sorted(EXPECTED_ROOT_SLUGS),
        "selected_root_membership_counts": dict(sorted(root_counts.items())),
        "selected_category_membership_counts": {
            taxonomy["by_id"][category_id]["slug"]: count
            for category_id, count in sorted(category_counts.items())
        },
        "every_selected_post_in_allowlisted_taxonomy": True,
        "category_archive_duplicates_avoided": sum(len(row["category_ids"]) for row in first) - len(first),
        "first_read_pages": first_pages,
        "repeat_read_pages": repeat_pages,
        "inventory_stable": stable,
        "selected": len(selected),
        "unique_post_ids": len({row["id"] for row in first}),
        "unique_canonical_candidates": len({_url_identity(row["url"]) for row in first}),
        "selected_month_counts": dict(sorted(month_counts.items())),
        "selected_oldest_publication": first[0]["date_publish"].isoformat(),
        "selected_newest_publication": first[-1]["date_publish"].isoformat(),
        "selected_url_set_sha256": url_set_sha256(selected),
        "boundary_before_start": {
            "id": prior["id"], "url": prior["url"],
            "date_publish": prior["date_publish"].isoformat(), **prior_response,
        },
        "boundary_after_end": {
            "id": following["id"], "url": following["url"],
            "date_publish": following["date_publish"].isoformat(), **following_response,
        },
        "publication_date_source": "WordPress REST post.date confirmed by visible Spanish article date",
        "modified_timestamp_used_as_publication_date": False,
        "coverage_complete": True,
    })
    return selected


def _exact_discovery_row(target: str) -> dict:
    cached = _DISCOVERY_ROWS.get(target)
    if cached:
        return cached
    slug = unquote(urlparse(target).path.strip("/").split("/")[-1])
    if not slug:
        raise RuntimeError("Noticias de la Tierra exact retry URL lacks a slug")
    taxonomy = _load_taxonomy()
    _, rows = _decode_posts({
        "slug": slug,
        "per_page": 10,
        "page": 1,
        "status": "publish",
        "_fields": POST_FIELDS,
    }, taxonomy)
    matches = [row for row in rows if _url_identity(row["url"]) == _url_identity(target)]
    if len(matches) != 1:
        raise RuntimeError("Noticias de la Tierra exact retry REST identity is not unique")
    _DISCOVERY_ROWS[target] = matches[0]
    return matches[0]


def _visible_date(soup: BeautifulSoup) -> tuple[date | None, str | None]:
    node = soup.select_one("article.ndt-article-box .ndt-article-meta")
    if not node:
        return None, None
    raw = clean(node.get_text(" ", strip=True).split("·", 1)[0])
    parsed = dateparser.parse(
        raw or "",
        languages=["es"],
        settings={"DATE_ORDER": "DMY", "PREFER_LOCALE_DATE_ORDER": True},
    )
    return (parsed.date() if parsed else None), raw


def _spanish_sanity(title: str, body: str) -> dict:
    sample = clean("%s %s" % (title, body[:1600])) or ""
    tokens = re.findall(r"[a-záéíóúüñ]+", sample.casefold())
    common = sorted({token for token in tokens if token in SPANISH_COMMON_WORDS})
    alphabetic = [character for character in sample if character.isalpha()]
    latin = sum(("a" <= character.casefold() <= "z") or character.casefold() in "áéíóúüñ" for character in alphabetic)
    latin_ratio = latin / len(alphabetic) if alphabetic else 0.0
    return {
        "method": "deterministic_html_lang_plus_spanish_common_word_check",
        "sample_chars": len(sample),
        "token_count": len(tokens),
        "common_words": common,
        "common_word_count": len(common),
        "latin_letter_ratio": round(latin_ratio, 6),
        "passed": len(tokens) >= 40 and len(common) >= 6 and latin_ratio >= 0.95,
    }


def scrape_url(url: str) -> dict:
    target = normalize_url(url, {Config.source_name})
    if not target or urlparse(target).path in {"", "/"}:
        raise RuntimeError("Noticias de la Tierra article URL is invalid")
    discovered = _exact_discovery_row(target)
    response = fetch(target)
    article, soup = extract_article(
        url=target,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("main article.ndt-article-box h1", "article.ndt-article-box h1", "main h1"),
        body_selectors=("main article.ndt-article-box .ndt-article-content", ".ndt-article-content"),
        remove_selectors=(
            ".sharedaddy", ".addtoany_share_save_container", ".wp-block-buttons",
            ".yarpp-related", ".saboxplugin-wrap", ".code-block", "[class*='share']",
            "[class*='related']", "[class*='newsletter']",
        ),
        parser="noticiasdelatierra_rest_visible_spanish_article",
        response=response,
    )
    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = normalize_url(canonical_node.get("href"), {Config.source_name}) if canonical_node else None
    if _url_identity(canonical or "") != _url_identity(target):
        raise RuntimeError("Noticias de la Tierra canonical URL does not match the requested post")

    visible_date, visible_raw = _visible_date(soup)
    if not visible_date or visible_date != discovered["date_publish"].date():
        raise RuntimeError("Noticias de la Tierra visible and REST publication dates disagree")
    page_meta_date = article.get("date_publish")
    if not isinstance(page_meta_date, datetime) or abs((page_meta_date.date() - visible_date).days) > 1:
        raise RuntimeError("Noticias de la Tierra page metadata and visible publication dates disagree")
    article["date_publish"] = discovered["date_publish"]

    if clean(article.get("title")) != discovered["title"]:
        raise RuntimeError("Noticias de la Tierra visible and REST titles disagree")
    language = clean((soup.html or {}).get("lang") if soup.html else None)
    article["language"] = (language or "").split("-", 1)[0].lower()
    sanity = _spanish_sanity(article.get("title") or "", article.get("maintext") or "")
    if article["language"] != "es":
        article["parser_error"] = "unexpected_language:%s" % article["language"]
    elif not sanity["passed"]:
        article["parser_error"] = "spanish_language_sanity_failed"

    article["field_evidence"]["titles"].append({
        "kind": "wordpress_rest_title", "source": "wp-json.post.title.rendered", "raw": discovered["title"],
    })
    article["field_evidence"]["dates"].extend([
        {"kind": "published", "source": "wp-json.post.date", "raw": discovered["date_raw"]},
        {"kind": "visible_time", "source": ".ndt-article-meta", "raw": visible_raw},
    ])
    article["field_evidence"].update({
        "canonical_url": canonical,
        "html_language": language,
        "language_sanity": sanity,
        "visible_published_date": visible_raw,
        "wordpress_rest_post": {
            "id": discovered["id"],
            "date": discovered["date_raw"],
            "date_gmt": discovered["date_gmt_raw"],
            "modified": discovered["modified_raw"],
            "title": discovered["title"],
            "category_ids": discovered["category_ids"],
            "category_root_slugs": discovered["category_root_slugs"],
        },
        "modified_timestamp_used_as_publication_date": False,
    })
    article["field_sources"]["date_publish"] = "WordPress REST post.date confirmed by visible Spanish article date and page metadata"
    article["field_sources"]["language"] = "html[lang] plus deterministic Spanish text sanity"
    return article
