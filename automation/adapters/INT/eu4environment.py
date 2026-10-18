"""Pure observer for EU4Environment's English news archive.

The publisher exposes a dedicated news sitemap and a finite, dated news
archive.  Discovery reconciles both first-party inventories and treats the
visible archive ``time.updated`` value as the publication date.  Sitemap
``lastmod`` values are retained as evidence only because they can represent
later edits.

This module has no database imports, credentials, cache writes, or production
write path.
"""
from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from adapters.INT.common import (
    clean,
    extract_article,
    fetch,
    jsonld_objects,
    normalize_url,
    parse_datetime,
    response_sha256,
    sitemap_rows,
    url_set_sha256,
    xml_response,
)


class Config:
    source_name = "eu4environment.org"
    country_code = "INT"
    is_env = True


INDEX = "https://www.eu4environment.org/sitemap_index.xml"
NEWS_MAP = "https://www.eu4environment.org/news-sitemap.xml"
ARCHIVE = "https://www.eu4environment.org/news/"


def _canonical_news_url(raw: str) -> str | None:
    normalized = normalize_url(raw, {Config.source_name})
    if not normalized:
        return None
    parts = [part for part in urlparse(normalized).path.split("/") if part]
    # Preserve the legacy/default English news scope. Translated copies live
    # below language prefixes such as /hy/news/ and /ro/news/.
    return normalized if len(parts) >= 2 and parts[0] == "news" else None


def _archive_rows(soup: BeautifulSoup) -> list[dict]:
    rows = []
    for card in soup.select("article.news"):
        link = card.select_one("h3.entry-title a[href]")
        stamp = card.select_one("time.updated[datetime]")
        title = clean(link.get_text(" ", strip=True)) if link else None
        url = _canonical_news_url(link.get("href")) if link else None
        published = parse_datetime(stamp.get("datetime")) if stamp else None
        if not (url and title and published):
            raise RuntimeError("EU4Environment archive card lacks URL, title, or publication date")
        rows.append({"url": url, "title": title, "date_publish": published})
    if not rows:
        raise RuntimeError("EU4Environment archive page has no news cards")
    return rows


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 180:
        raise ValueError("invalid EU4Environment window (max 181 days)")

    index_response, index = xml_response(INDEX)
    news_maps = [row for row in sitemap_rows(index) if row["url"].rstrip("/") == NEWS_MAP]
    if len(news_maps) != 1:
        raise RuntimeError("EU4Environment index does not contain exactly one news sitemap")
    map_response, news_map = xml_response(NEWS_MAP)
    sitemap_urls = []
    sitemap_lastmods = {}
    for row in sitemap_rows(news_map):
        url = _canonical_news_url(row["url"])
        if url:
            sitemap_urls.append(url)
            if row.get("lastmod"):
                sitemap_lastmods.setdefault(url, row["lastmod"])
    sitemap_set = set(sitemap_urls)
    if not sitemap_set:
        raise RuntimeError("EU4Environment news sitemap has no English news URLs")

    first_response = fetch(ARCHIVE)
    first_soup = BeautifulSoup(first_response.content, "html.parser")
    page_numbers = []
    for node in first_soup.select(".pagination .page-numbers"):
        text = clean(node.get_text(" ", strip=True))
        if text and text.isdigit():
            page_numbers.append(int(text))
    last_page = max(page_numbers or [1])
    if not 1 <= last_page <= 200:
        raise RuntimeError("EU4Environment archive page bound is invalid")

    pages = []
    archive_rows = []
    for number in range(1, last_page + 1):
        if number == 1:
            response, soup = first_response, first_soup
            url = ARCHIVE
        else:
            url = f"{ARCHIVE}page/{number}/"
            response = fetch(url)
            soup = BeautifulSoup(response.content, "html.parser")
        rows = _archive_rows(soup)
        archive_rows.extend(rows)
        pages.append({
            "page": number,
            "url": url,
            "rows": len(rows),
            "first_publication": rows[0]["date_publish"].isoformat(),
            "last_publication": rows[-1]["date_publish"].isoformat(),
            "has_next": bool(soup.select_one(".pagination a.next")),
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
        })

    archive_urls = [row["url"] for row in archive_rows]
    archive_set = set(archive_urls)
    if len(archive_urls) != len(archive_set):
        raise RuntimeError("EU4Environment archive contains duplicate English news URLs")
    if archive_set != sitemap_set:
        raise RuntimeError(
            "EU4Environment archive/sitemap URL mismatch: archive_only=%d sitemap_only=%d"
            % (len(archive_set - sitemap_set), len(sitemap_set - archive_set))
        )
    if pages[-1]["has_next"]:
        raise RuntimeError("EU4Environment final archive page still advertises a next page")
    if any(
        archive_rows[index]["date_publish"] < archive_rows[index + 1]["date_publish"]
        for index in range(len(archive_rows) - 1)
    ):
        raise RuntimeError("EU4Environment archive is not publication-date descending")

    selected_rows = [
        row for row in archive_rows
        if start <= row["date_publish"].date() <= end
    ]
    selected = [row["url"] for row in selected_rows]
    latest = archive_rows[0]["date_publish"].date()
    evidence.update({
        "method": "complete_english_news_sitemap_reconciled_to_contiguous_dated_archive_pages",
        "sitemap_index": INDEX,
        "sitemap_index_sha256": response_sha256(index_response),
        "news_sitemap": NEWS_MAP,
        "news_sitemap_sha256": response_sha256(map_response),
        "news_sitemap_rows": len(sitemap_urls),
        "news_sitemap_unique_english_urls": len(sitemap_set),
        "sitemap_duplicate_rows": len(sitemap_urls) - len(sitemap_set),
        "archive": ARCHIVE,
        "archive_page_count": last_page,
        "archive_pages_contiguous": True,
        "archive_rows": len(archive_rows),
        "archive_urls_unique": len(archive_set),
        "archive_sitemap_url_sets_equal": True,
        "archive_publication_dates_descending": True,
        "archive_pages": pages,
        "latest_publication_date": latest.isoformat(),
        "publisher_inactive_for_requested_window": bool(latest < start and not selected),
        "selected": len(selected),
        "selected_url_set_sha256": url_set_sha256(selected),
        "sitemap_lastmod_is_inventory_evidence_not_publication_date": True,
        "sitemap_lastmod_values_recorded": len(sitemap_lastmods),
        "translated_language_prefixes_excluded_to_preserve_legacy_english_scope": True,
        "coverage_complete": True,
    })
    return selected


def _visible_publication(soup: BeautifulSoup):
    node = soup.select_one("article.news time.updated[datetime], time.updated[datetime]")
    raw = clean(node.get("datetime")) if node else None
    parsed = parse_datetime(raw)
    source = "time.updated[datetime]" if parsed else None
    jsonld_raw = None
    for item in jsonld_objects(soup):
        kinds = item.get("@type")
        kinds = set(kinds if isinstance(kinds, list) else [kinds])
        if kinds & {"WebPage", "Article", "NewsArticle"} and item.get("datePublished"):
            jsonld_raw = clean(item.get("datePublished"))
            break
    if parsed and jsonld_raw:
        other = parse_datetime(jsonld_raw)
        if not other or other != parsed:
            raise RuntimeError("EU4Environment visible and JSON-LD publication dates disagree")
    return parsed, source, raw, jsonld_raw


def scrape_url(url: str) -> dict:
    response = fetch(url)
    article, soup = extract_article(
        url=url,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("article.news h1.title", "h1.title", "article h1", "h1"),
        body_selectors=("article.news .entry-content", ".entry-content"),
        remove_selectors=(".sharedaddy", ".sd-sharing-enabled", ".post-tags", ".yarpp-related"),
        parser="eu4environment_visible_h1_archive_time_entry_content",
        response=response,
    )
    published, source, raw, jsonld_raw = _visible_publication(soup)
    article["date_publish"] = published
    article["field_sources"]["date_publish"] = source
    article["field_evidence"]["dates"] = [
        {"kind": "published", "source": source, "raw": raw},
    ]
    if jsonld_raw:
        article["field_evidence"]["dates"].append({
            "kind": "published", "source": "jsonld.WebPage.datePublished", "raw": jsonld_raw,
        })
    missing = [
        name for name in ("title", "date_publish", "maintext")
        if not article.get(name)
    ]
    article["parser_error"] = "missing:%s" % ",".join(missing) if missing else None
    return article
