"""Observation-only Sözcü adapter. No database imports or writes."""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from adapters.TUR.common import extract_article, sitemap_entries, unique_article_urls, xml_root


class Config:
    source_name = "sozcu.com.tr"
    country_code = "TUR"
    is_env = False


INDEX = "https://www.sozcu.com.tr/feeds/sitemapindex"
MONTHLY = re.compile(r"/feeds/sitemapposts(?:\?|$)")
DENY = ("/spor/", "/futbol/", "/diger-sporlar/", "/saglik/", "/astroloji/", "/yazarlar/", "/galeri/")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index = xml_root(INDEX)
    months = {(y, m) for y in range(start.year, end.year + 1) for m in range(1, 13)
              if (y, m) >= (start.year, start.month) and (y, m) <= (end.year, end.month)}
    selected = {}
    for url, _ in sitemap_entries(index):
        if not MONTHLY.search(urlparse(url).path + ("?" if urlparse(url).query else "")):
            continue
        q = parse_qs(urlparse(url).query)
        try:
            key = int(q["year"][0]), int(q["month"][0])
        except (KeyError, ValueError, IndexError):
            continue
        if key in months:
            selected[key] = url
    missing = sorted(months - set(selected))
    evidence.update({"sitemap_index": INDEX, "months_requested": [f"{y}-{m:02d}" for y, m in sorted(months)],
                     "sitemaps_checked": [], "sitemap_failures": [], "missing_months_in_index": [f"{y}-{m:02d}" for y, m in missing]})
    if missing:
        raise RuntimeError(f"monthly sitemaps missing from live index: {missing}")
    entries = []
    for key, url in sorted(selected.items()):
        try:
            items = sitemap_entries(xml_root(url))
            evidence["sitemaps_checked"].append({"url": url, "entries": len(items), "month": f"{key[0]}-{key[1]:02d}"})
            entries.extend(items)
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} requested monthly sitemaps failed")
    evidence["discovered_urls_before_filter"] = len(entries)
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    undated = sum(modified is None for _, modified in entries)
    entries = [(url, modified) for url, modified in entries if modified is None or lower <= modified <= upper]
    evidence["sitemap_lastmod_candidate_window"] = {"start": lower.isoformat(), "end": upper.isoformat()}
    evidence["sitemap_entries_without_lastmod"] = undated
    evidence["sitemap_candidates_in_window"] = len(entries)
    entries.sort(key=lambda item: item[1] or date.min, reverse=True)
    urls = unique_article_urls(entries, Config.source_name, DENY)
    evidence["discovered_urls_after_filter"] = len(urls)
    return urls


def scrape_url(url: str) -> dict:
    return extract_article(
        url, Config.source_name,
        body_selectors=(".article-body", "article .article-content", "article .news-detail", ".news-detail-content", "article"),
        visible_date_selectors=("time[datetime]", "time"),
        title_selectors=("article h1", "h1"),
    )
