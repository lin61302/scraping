"""Observation-only adapter for georgiatoday.ge WordPress sitemaps."""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from adapters.GEO.common import (
    article_body,
    clean,
    fetch,
    first_text,
    jsonld_objects,
    normalize_url,
    page_evidence,
    parse_datetime,
    parser_error,
    sitemap_entries,
    xml_document,
)


class Config:
    source_name = "georgiatoday.ge"
    country_code = "GEO"
    is_env = False


INDEX = "https://georgiatoday.ge/wp-sitemap.xml"
POST_MAP = re.compile(r"/post-sitemap(?P<number>\d*)\.xml$")
DENY = ("/search/", "/adserver/", "/list/", "/img/", "/uploads/", "/wp-content/", "/category/", "/page/", "/travel/")


def _post_number(url: str) -> int | None:
    match = POST_MAP.search(urlparse(url).path)
    if not match:
        return None
    return int(match.group("number") or 1)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index_entries = []
    for url, modified in sitemap_entries(xml_document(INDEX)):
        number = _post_number(url)
        if number is not None:
            index_entries.append((number, url, modified))
    if not index_entries:
        raise RuntimeError("Georgia Today sitemap index contains no post sitemaps")

    overlap_start = start - timedelta(days=2)
    selected_numbers = {
        number for number, _, modified in index_entries if modified is not None and modified >= overlap_start
    }
    # Check one preceding sitemap so a publisher index rollover cannot create a
    # boundary gap. Internal lastmod filtering keeps this inexpensive.
    if selected_numbers:
        selected_numbers.add(max(1, min(selected_numbers) - 1))
    else:
        selected_numbers.add(max(number for number, _, _ in index_entries))
    selected = [entry for entry in index_entries if entry[0] in selected_numbers]

    evidence.update(
        {
            "method": "wordpress_sitemap_index",
            "sitemap_index": INDEX,
            "index_post_sitemap_count": len(index_entries),
            "selection_overlap_start": overlap_start.isoformat(),
            "selected_post_sitemaps": [],
            "sitemap_failures": [],
        }
    )
    candidates: list[tuple[date | None, str]] = []
    lower_boundary_seen = False
    for number, sitemap_url, index_modified in sorted(selected):
        try:
            entries = sitemap_entries(xml_document(sitemap_url))
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
            continue
        accepted = 0
        undated = 0
        for raw_url, modified in entries:
            url = normalize_url(raw_url, Config.source_name, DENY)
            if not url:
                continue
            if modified is None:
                undated += 1
                candidates.append((None, url))
                accepted += 1
            elif start <= modified <= end + timedelta(days=1):
                candidates.append((modified, url))
                accepted += 1
        evidence["selected_post_sitemaps"].append(
            {
                "number": number,
                "url": sitemap_url,
                "index_lastmod": index_modified.isoformat() if index_modified else None,
                "entries": len(entries),
                "candidates": accepted,
                "entries_without_lastmod": undated,
                "newest_lastmod": max((modified for _, modified in entries if modified), default=None),
                "oldest_lastmod": min((modified for _, modified in entries if modified), default=None),
            }
        )
        dated = [modified for _, modified in entries if modified]
        if dated and min(dated) < overlap_start:
            lower_boundary_seen = True
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} selected Georgia Today post sitemaps failed")
    # Newest sitemap changes first makes bounded canaries representative of the
    # requested interval. Publication dates are still verified on article pages.
    candidates.sort(key=lambda item: (item[0] or date.min, item[1]), reverse=True)
    result = list(dict.fromkeys(url for _, url in candidates))
    evidence["coverage_complete"] = bool(lower_boundary_seen and not evidence["sitemap_failures"])
    evidence["discovered_urls_before_dedup"] = len(candidates)
    evidence["discovered_urls_after_dedup"] = len(result)
    return result


def _published_signal(soup: BeautifulSoup) -> tuple[object, object]:
    # Publication metadata is authoritative here. The visible article body can
    # mention unrelated dates, which caused the Sep 17 vs Sep 14 canary error.
    for selector in (
        'meta[property="article:published_time"][content]',
        'meta[itemprop="datePublished"][content]',
    ):
        node = soup.select_one(selector)
        if node and clean(node.get("content")):
            return clean(node.get("content")), selector
    for item in jsonld_objects(soup):
        raw = clean(item.get("datePublished"))
        if raw:
            return raw, "jsonld.datePublished"
    for selector in ("time.entry-date[datetime]", "time[datetime]", "div.jeg_meta_date"):
        node = soup.select_one(selector)
        if node:
            raw = clean(node.get("datetime") or node.get_text(" ", strip=True))
            if raw:
                return raw, selector
    return None, None


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_selector = first_text(soup, ("h1.jeg_post_title", "article h1", "h1"))
    body, body_selector = article_body(soup, ("div.content-inner", "div.news-data", "article .entry-content", "article"))
    date_raw, date_source = _published_signal(soup)
    published = parse_datetime(date_raw)

    return {
        "url": url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "maintext": body,
        "date_publish": published,
        "parser": "georgiatoday_h1_published_meta_content_inner",
        "parser_error": parser_error(title, body, published),
        "field_evidence": page_evidence(
            soup,
            title=title,
            title_selector=title_selector,
            date_raw=date_raw,
            date_source=date_source,
            body=body,
            body_selector=body_selector,
            status=response.status_code,
        ),
    }
