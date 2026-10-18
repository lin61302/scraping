"""Pure network observation adapter for publika.ge."""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from adapters.GEO.common import (
    article_body, fetch, first_text, normalize_url, page_evidence,
    parse_datetime, parser_error, published_signal, sitemap_entries, xml_document,
)


class Config:
    source_name = "publika.ge"
    country_code = "GEO"
    is_env = False


INDEX = "https://publika.ge/sitemap_index.xml"
POST_MAP = re.compile(r"/post-sitemap(?P<number>\d*)\.xml$")


def _number(url: str) -> int | None:
    match = POST_MAP.search(urlparse(url).path)
    return int(match.group("number") or 1) if match else None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    overlap = start - timedelta(days=2)
    maps = []
    for url, modified in sitemap_entries(xml_document(INDEX)):
        number = _number(url)
        if number is not None:
            maps.append((number, url, modified))
    recent_numbers = {number for number, _, modified in maps if modified and modified >= overlap}
    numbered_recent = [number for number in recent_numbers if number > 1]
    if numbered_recent:
        recent_numbers.add(max(2, min(numbered_recent) - 1))
    selected = [item for item in maps if item[0] in recent_numbers]
    evidence.update({"method": "wordpress_sitemap_index", "sitemap_index": INDEX,
                     "overlap_start": overlap.isoformat(), "index_post_sitemap_count": len(maps),
                     "selected_post_sitemaps": [], "sitemap_failures": []})
    candidates = []
    boundary = False
    for number, sitemap_url, index_modified in sorted(selected):
        try:
            entries = sitemap_entries(xml_document(sitemap_url))
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
            continue
        dates = [modified for _, modified in entries if modified]
        accepted = []
        for raw, modified in entries:
            normalized = normalize_url(raw, Config.source_name, ("/category/", "/author/", "/tag/"))
            if normalized and modified and overlap <= modified <= end + timedelta(days=1):
                accepted.append((modified, normalized))
        evidence["selected_post_sitemaps"].append({
            "number": number, "url": sitemap_url,
            "index_lastmod": index_modified.isoformat() if index_modified else None,
            "entries": len(entries), "candidates": len(accepted),
            "newest_lastmod": max(dates).isoformat() if dates else None,
            "oldest_lastmod": min(dates).isoformat() if dates else None,
        })
        candidates.extend(accepted)
        if number > 1 and dates and max(dates) < overlap:
            boundary = True
    evidence["coverage_complete"] = bool(maps and boundary and not evidence["sitemap_failures"])
    if not evidence["coverage_complete"]:
        raise RuntimeError("Publika sitemap selection did not prove a clean lower date boundary")
    candidates.sort(reverse=True)
    result = list(dict.fromkeys(url for _, url in candidates))
    evidence["discovered_urls_before_dedup"] = len(candidates)
    evidence["discovered_urls_after_dedup"] = len(result)
    return result


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_selector = first_text(soup, ("h1.wi-article-title", "article h1", "h1"))
    body, body_selector = article_body(soup, ("div.wi-content-editor", "article"))
    date_raw, date_source = published_signal(soup)
    published = parse_datetime(date_raw)
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "maintext": body, "date_publish": published,
        "parser": "publika_h1_published_metadata_content_editor",
        "parser_error": parser_error(title, body, published),
        "field_evidence": page_evidence(soup, title=title, title_selector=title_selector,
                                         date_raw=date_raw, date_source=date_source, body=body,
                                         body_selector=body_selector, status=response.status_code),
    }
