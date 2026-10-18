"""Pure observer for Asia Times' dated post-sitemap scope.

The legacy updater hard-coded two sitemap shard numbers, filtered year/month
strings in URLs, and wrote directly to MongoDB.  This adapter reads the live
publisher sitemap index, verifies the complete post-sitemap sequence, scans
every current post shard for the requested whole months, and rereads every
matching shard before returning URLs.  Sitemap ``lastmod`` is retained only
as inventory evidence; article pages provide the authoritative publication
timestamp, title, body, language, and section.

This module has no database imports, credentials, cache mutation, or
production write path.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

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
    source_name = "asiatimes.com"
    country_code = "REG"
    is_env = False
    access_block_circuit_threshold = 3


INDEX = "https://asiatimes.com/sitemap_index.xml"
POST_MAP = re.compile(r"/post-sitemap(\d*)\.xml$")
PATH_MONTH = re.compile(r"^/(20\d{2})/(\d{2})/")
_DISCOVERY_ROWS: dict[str, dict] = {}


def _month(url: str) -> str | None:
    match = PATH_MONTH.match(urlparse(url).path)
    if not match:
        return None
    year, month = map(int, match.groups())
    if not 1 <= month <= 12:
        return None
    return "%04d-%02d" % (year, month)


def _requested_months(start: date, end: date) -> list[str]:
    if start.day != 1 or (end + timedelta(days=1)).day != 1:
        raise ValueError("Asia Times sitemap observations require whole calendar months")
    months = []
    cursor = start
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (1 if cursor.month == 12 else 0), 1 if cursor.month == 12 else cursor.month + 1, 1)
    if not 1 <= len(months) <= 3:
        raise ValueError("Asia Times window must contain one to three whole months")
    return months


def _post_sitemaps() -> tuple[object, list[dict]]:
    response, soup = xml_response(INDEX)
    children = []
    for row in sitemap_rows(soup):
        path = urlparse(row["url"]).path
        match = POST_MAP.search(path)
        if not match:
            continue
        sequence = int(match.group(1) or 1)
        children.append({"sequence": sequence, "url": row["url"], "index_lastmod": row.get("lastmod")})
    children.sort(key=lambda item: item["sequence"])
    sequences = [item["sequence"] for item in children]
    if not children or sequences != list(range(1, len(children) + 1)):
        raise RuntimeError("Asia Times post-sitemap sequence is incomplete")
    if len({item["url"] for item in children}) != len(children):
        raise RuntimeError("Asia Times sitemap index repeats a post sitemap")
    return response, children


def _scan_child(child: dict, requested: set[str]) -> tuple[list[dict], dict]:
    response, soup = xml_response(child["url"])
    rows = []
    month_counts = Counter()
    all_urls = []
    first_by_url: dict[str, dict] = {}
    duplicate_counts = Counter()
    for item in sitemap_rows(soup):
        url = normalize_url(item["url"], {Config.source_name})
        if not url:
            raise RuntimeError("Asia Times post sitemap contains a non-publisher URL")
        if url in first_by_url:
            duplicate_counts[url] += 1
            continue
        first_by_url[url] = item
        month = _month(url)
        if month:
            month_counts[month] += 1
        all_urls.append(url)
        if month in requested:
            rows.append({
                "url": url,
                "path_month": month,
                "sitemap_lastmod": item.get("lastmod"),
                "sitemap_sequence": child["sequence"],
            })
    summary = {
        "sequence": child["sequence"],
        "url": child["url"],
        "index_lastmod": child.get("index_lastmod"),
        "entries": len(all_urls),
        "raw_entries": len(all_urls) + sum(duplicate_counts.values()),
        "duplicate_canonical_urls": len(duplicate_counts),
        "duplicate_extra_entries": sum(duplicate_counts.values()),
        "duplicate_examples": [
            {"url": url, "occurrences": extra + 1}
            for url, extra in sorted(duplicate_counts.items())[:10]
        ],
        "path_month_entries": sum(month_counts.values()),
        "oldest_path_month": min(month_counts) if month_counts else None,
        "newest_path_month": max(month_counts) if month_counts else None,
        "requested_matches": len(rows),
        "url_set_sha256": url_set_sha256(all_urls),
        "requested_url_set_sha256": url_set_sha256(row["url"] for row in rows),
        "response_sha256": response_sha256(response),
        "response_bytes": len(response.content),
    }
    return rows, {"summary": summary, "month_counts": dict(month_counts)}


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 92:
        raise ValueError("invalid Asia Times window (max three whole months)")
    requested_list = _requested_months(start, end)
    requested = set(requested_list)

    index_response, children = _post_sitemaps()
    selected_rows = []
    shard_summaries = []
    all_month_counts = Counter()
    relevant_children = []
    for child in children:
        rows, result = _scan_child(child, requested)
        selected_rows.extend(rows)
        shard_summaries.append(result["summary"])
        all_month_counts.update(result["month_counts"])
        if rows:
            relevant_children.append(child)

    selected = [row["url"] for row in selected_rows]
    if not selected or len(selected) != len(set(selected)):
        raise RuntimeError("Asia Times requested sitemap inventory is empty or duplicated")
    if any(row["path_month"] not in requested for row in selected_rows):
        raise RuntimeError("Asia Times selected a URL outside the requested months")

    # The whole-month inventory must be enclosed by at least one publisher URL
    # in the previous and following calendar month.
    previous_day = start - timedelta(days=1)
    following_day = end + timedelta(days=1)
    previous_month = previous_day.strftime("%Y-%m")
    following_month = following_day.strftime("%Y-%m")
    if all_month_counts[previous_month] < 1 or all_month_counts[following_month] < 1:
        raise RuntimeError("Asia Times post sitemaps did not close both month boundaries")

    repeat_index_response, repeat_children = _post_sitemaps()
    stable_index = [item["url"] for item in children] == [item["url"] for item in repeat_children]
    if not stable_index:
        raise RuntimeError("Asia Times post-sitemap index changed between reads")
    repeated_rows = []
    repeated_summaries = []
    for child in relevant_children:
        rows, result = _scan_child(child, requested)
        repeated_rows.extend(rows)
        repeated_summaries.append(result["summary"])
    repeated = [row["url"] for row in repeated_rows]
    if selected != repeated:
        raise RuntimeError("Asia Times requested URL inventory changed between reads")

    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in selected_rows})
    selected_month_counts = Counter(row["path_month"] for row in selected_rows)
    evidence.update({
        "method": "complete_live_post_sitemap_sequence_with_stable_whole_month_url_inventory",
        "sitemap_index": INDEX,
        "index_sha256": response_sha256(index_response),
        "repeat_index_sha256": response_sha256(repeat_index_response),
        "index_url_set_stable": stable_index,
        "post_sitemap_sequence_complete": True,
        "post_sitemap_count": len(children),
        "scanned_post_sitemaps": shard_summaries,
        "publisher_duplicate_canonical_urls_total": sum(
            item["duplicate_canonical_urls"] for item in shard_summaries
        ),
        "publisher_duplicate_extra_entries_total": sum(
            item["duplicate_extra_entries"] for item in shard_summaries
        ),
        "matching_sitemaps_have_duplicate_canonical_urls": any(
            item["duplicate_canonical_urls"] for item in repeated_summaries
        ),
        "repeated_matching_sitemaps": repeated_summaries,
        "matching_sitemap_sequences": [item["sequence"] for item in relevant_children],
        "requested_whole_months": requested_list,
        "selected": len(selected),
        "selected_month_counts": dict(sorted(selected_month_counts.items())),
        "selected_url_set_sha256": url_set_sha256(selected),
        "boundary_previous_month": {"month": previous_month, "publisher_urls": all_month_counts[previous_month]},
        "boundary_following_month": {"month": following_month, "publisher_urls": all_month_counts[following_month]},
        "legacy_policy": "exclude publisher Opinion section after article-page category confirmation",
        "sitemap_path_month_is_candidate_only_article_page_date_is_authoritative": True,
        "sitemap_lastmod_used_as_publication_date": False,
        "legacy_database_writer_executed": False,
        "coverage_complete": True,
    })
    return selected


def _aware_datetime(raw: str | None, label: str) -> datetime:
    value = clean(raw)
    if not value:
        raise RuntimeError("Asia Times lacks %s timestamp" % label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Asia Times %s timestamp is invalid" % label) from exc
    if parsed.tzinfo is None:
        raise RuntimeError("Asia Times %s timestamp lacks timezone" % label)
    return parsed


def scrape_url(url: str) -> dict:
    target = normalize_url(url, {Config.source_name})
    path_month = _month(target) if target else None
    if not target or not path_month:
        raise RuntimeError("Asia Times article URL lacks its publisher year/month path")
    response = fetch(target)
    article, soup = extract_article(
        url=target,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("h1.entry-title", "main h1.entry-title", "main h1"),
        body_selectors=("article .entry-content", ".entry-content"),
        remove_selectors=(
            ".newspack-popup-container", ".newspack-registration", ".sharedaddy",
            ".jp-relatedposts", ".post-footer", ".newsletter", ".subscription",
            "[class*='newspack-popup']", "[class*='newsletter']", "[class*='related']",
        ),
        parser="asiatimes_visible_h1_local_published_time_entry_content",
        response=response,
    )
    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = normalize_url(canonical_node.get("href"), {Config.source_name}) if canonical_node else None
    if canonical != target:
        raise RuntimeError("Asia Times canonical URL does not match the requested post")

    visible_node = soup.select_one("time.entry-date.published[datetime]")
    meta_node = soup.select_one('meta[property="article:published_time"][content]')
    item = article_jsonld(soup) or {}
    visible_raw = clean(visible_node.get("datetime")) if visible_node else None
    meta_raw = clean(meta_node.get("content")) if meta_node else None
    jsonld_raw = clean(item.get("datePublished"))
    visible = _aware_datetime(visible_raw, "visible publication")
    meta = _aware_datetime(meta_raw, "article:published_time")
    jsonld = _aware_datetime(jsonld_raw, "NewsArticle datePublished")
    visible_utc = visible.astimezone(timezone.utc)
    if abs((visible_utc - meta.astimezone(timezone.utc)).total_seconds()) > 1:
        raise RuntimeError("Asia Times visible and metadata publication timestamps disagree")
    if abs((visible_utc - jsonld.astimezone(timezone.utc)).total_seconds()) > 1:
        raise RuntimeError("Asia Times visible and JSON-LD publication timestamps disagree")
    if visible.strftime("%Y-%m") != path_month:
        raise RuntimeError("Asia Times URL month and visible publication month disagree")
    article["date_publish"] = visible.replace(tzinfo=None)

    discovered = _DISCOVERY_ROWS.get(target)
    if discovered and discovered["path_month"] != visible.strftime("%Y-%m"):
        raise RuntimeError("Asia Times sitemap and page publication months disagree")

    language = clean((soup.html or {}).get("lang") if soup.html else None)
    article["language"] = (language or clean(item.get("inLanguage")) or "").split("-", 1)[0].lower()
    if article["language"] != "en":
        article["parser_error"] = "unexpected_language:%s" % article["language"]

    post = soup.select_one("article.post")
    post_classes = list(post.get("class") or []) if post else []
    sections = item.get("articleSection")
    sections = sections if isinstance(sections, list) else ([sections] if sections else [])
    sections = [clean(value) for value in sections if clean(value)]
    opinion = "category-opinion" in post_classes or any(value.casefold() == "opinion" for value in sections)
    if opinion:
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_legacy_opinion_section"
        article["exclusion_evidence"] = {
            "policy_type": "legacy_publisher_opinion_section",
            "publisher_url": target,
            "publisher_article_classes": post_classes,
            "publisher_article_sections": sections,
        }

    dates = article["field_evidence"].setdefault("dates", [])
    dates.append({"kind": "visible_time", "source": "time.entry-date.published", "raw": visible_raw})
    article["field_evidence"].update({
        "canonical_url": canonical,
        "html_language": language,
        "visible_published_time": visible_raw,
        "published_meta_time": meta_raw,
        "jsonld_published_time": jsonld_raw,
        "publication_timestamp_signals_same_instant": True,
        "publisher_article_classes": post_classes,
        "publisher_article_sections": sections,
        "legacy_opinion_exclusion": opinion,
        "sitemap_inventory": ({
            "path_month": discovered["path_month"],
            "sitemap_sequence": discovered["sitemap_sequence"],
            "lastmod_not_used": discovered["sitemap_lastmod"],
        } if discovered else None),
        "sitemap_lastmod_used_as_publication_date": False,
    })
    article["field_sources"]["date_publish"] = "visible time.entry-date.published, same instant as article:published_time and NewsArticle datePublished"
    article["field_sources"]["language"] = "html[lang] or NewsArticle inLanguage"
    return article
