"""Pure observer for Al Jazeera's publisher daily sitemap archives."""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from urllib.parse import urlparse

from adapters.INT.common import (
    extract_article,
    normalize_url,
    parse_datetime,
    response_sha256,
    sitemap_rows,
    url_set_sha256,
    xml_response,
)


class Config:
    source_name = "aljazeera.com"
    country_code = "INT"
    is_env = False


BLACKLIST = (
    "/uploads/", "/opinions/", "/sports/", "/author/", "/videos/", "/gallery/",
    "/inpictures/", "/watch/", "/shows/", "/blogs/", "/tag/", "/podcasts/",
    "/program/", "/schedule/", "/where/",
)


def _daily(day: date) -> str:
    return "https://www.aljazeera.com/sitemap.xml?yyyy=%04d&mm=%02d&dd=%02d" % (
        day.year, day.month, day.day,
    )


def _path_day(url: str):
    parts = [item for item in urlparse(url).path.split("/") if item]
    for offset in range(len(parts) - 2):
        try:
            year, month, day = map(int, parts[offset:offset + 3])
            if 2000 <= year <= 2100:
                return date(year, month, day)
        except ValueError:
            continue
    return None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 62:
        raise ValueError("invalid Al Jazeera daily-sitemap window (max 63 days)")
    checked = []
    selected = []
    excluded = Counter()
    raw_urls = []
    path_mismatches = []
    lastmod_day_differences = []
    current = start
    while current <= end:
        endpoint = _daily(current)
        response, soup = xml_response(endpoint)
        rows = sitemap_rows(soup)
        if not rows:
            raise RuntimeError("Al Jazeera daily sitemap is empty: %s" % endpoint)
        day_selected = []
        for row in rows:
            normalized = normalize_url(row["url"], {Config.source_name})
            if not normalized:
                excluded["invalid_or_external"] += 1
                continue
            raw_urls.append(normalized)
            lastmod = parse_datetime(row.get("lastmod"))
            if lastmod and lastmod.date() != current:
                lastmod_day_differences.append({
                    "url": normalized,
                    "daily_sitemap": current.isoformat(),
                    "lastmod": row.get("lastmod"),
                })
            matches = [pattern for pattern in BLACKLIST if pattern in urlparse(normalized).path]
            if matches:
                for pattern in matches:
                    excluded[pattern] += 1
                continue
            path_day = _path_day(normalized)
            if path_day != current:
                path_mismatches.append({
                    "url": normalized,
                    "daily_sitemap": current.isoformat(),
                    "path_day": path_day.isoformat() if path_day else None,
                })
                continue
            day_selected.append(normalized)
        selected.extend(day_selected)
        checked.append({
            "date": current.isoformat(),
            "url": endpoint,
            "entries": len(rows),
            "selected": len(day_selected),
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
        })
        current += timedelta(days=1)
    duplicates = sorted(url for url, count in Counter(selected).items() if count > 1)
    selected = list(dict.fromkeys(selected))
    coverage = not path_mismatches and not duplicates and len(checked) == (end - start).days + 1
    evidence.update({
        "method": "publisher_daily_sitemaps_for_every_requested_calendar_day",
        "daily_sitemaps": checked,
        "daily_sitemaps_expected": (end - start).days + 1,
        "raw_urls": len(raw_urls),
        "raw_url_set_sha256": url_set_sha256(raw_urls),
        "legacy_blacklist_patterns": list(BLACKLIST),
        "excluded_by_pattern": dict(sorted(excluded.items())),
        "path_date_mismatches": path_mismatches,
        "lastmod_day_differences": lastmod_day_differences,
        "duplicate_selected_urls": duplicates,
        "selected_url_set_sha256": url_set_sha256(selected),
        "sitemap_lastmod_is_inventory_evidence_article_page_date_is_authoritative": True,
        "coverage_complete": coverage,
    })
    if not coverage:
        raise RuntimeError("Al Jazeera daily sitemap accounting did not close")
    return selected


def scrape_url(url: str) -> dict:
    article, _ = extract_article(
        url=url,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("main h1", "h1.compact-featured-area__title", "h1"),
        body_selectors=("main .wysiwyg--all-content", ".wysiwyg--all-content", "main"),
        remove_selectors=(".article-header", ".article-featured-image", ".article-info", ".related-content"),
        parser="aljazeera_visible_h1_jsonld_date_wysiwyg_body",
    )
    page_day = article.get("date_publish").date() if article.get("date_publish") else None
    path_day = _path_day(url)
    if page_day and path_day and page_day != path_day:
        article["parser_error"] = "url_path_vs_page_publication_date_disagreement"
    return article
