"""Observation adapter for Daily Times; access is explicitly probed for WAF state."""
from __future__ import annotations

import re
from datetime import date, timedelta

from adapters.PAK.common import AccessBlockedError, extract_article, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "dailytimes.com.pk"
    country_code = "PAK"
    is_env = False


INDEX = "https://dailytimes.com.pk/sitemap_index.xml"
EXCLUDED = ("Sports", "Arts, Culture & Books", "Lifestyle", "Movies", "TV", "Cartoons", "Letters", "Blogs")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({
        "strategy": "live_wordpress_sitemap_index",
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "sitemaps_checked": [],
        "sitemap_failures": [],
        "coverage_complete": False,
    })
    try:
        index = [item for item in sitemap_entries(xml_document(INDEX)) if re.search(r"post-sitemap\d*\.xml$", item[0])]
    except Exception as exc:
        evidence.update({"access_status": "blocked_or_unavailable", "fatal_probe_error": str(exc)})
        return []
    selected = [item for item in index if item[1] is None or item[1] >= start - timedelta(days=30)]
    entries = []
    for sitemap_url, modified in selected:
        try:
            items = sitemap_entries(xml_document(sitemap_url))
            evidence["sitemaps_checked"].append({"url": sitemap_url, "index_lastmod": modified.isoformat() if modified else None, "entries": len(items)})
            entries.extend(items)
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        return []
    candidates = [item for item in entries if item[1] is None or start <= item[1] <= end + timedelta(days=1)]
    candidates.sort(key=lambda item: item[1] or date.min)
    urls = unique_urls(candidates, Config.source_name, ())
    evidence.update({"post_sitemaps_total": len(index), "post_sitemaps_selected": len(selected), "entries_before_window_filter": len(entries), "candidate_entries_in_window": len(candidates), "discovered_urls_after_filter": len(urls), "coverage_complete": True, "access_status": "accessible"})
    return urls


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1", ".entry-title", "h1"),
        body_selectors=(".td-post-content", ".entry-content", ".post-content", "article"),
        visible_date_selectors=("meta[property='article:published_time']", "time"),
        prefer_visible_date=False,
        evidence_body_selectors=(".td-post-content", ".entry-content", "article"),
        category_selectors=("p.entry-meta a", "meta[property='article:section']"),
        excluded_categories=EXCLUDED,
    )
