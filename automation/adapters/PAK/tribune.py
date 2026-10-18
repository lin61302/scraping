"""Observation-only Express Tribune adapter using its live post sitemap index."""
from __future__ import annotations

import re
from datetime import date

from adapters.PAK.common import extract_article, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "tribune.com.pk"
    country_code = "PAK"
    is_env = False


INDEX = "https://tribune.com.pk/sitemap.xml"
EXCLUDED = ("Sports", "TV", "Life & Style", "Entertainment", "Sindh", "Technology", "Magazine")


def _sequence(url: str) -> int:
    match = re.search(r"/posts-(\d+)\.xml", url)
    return int(match.group(1)) if match else 10**6


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index = [item for item in sitemap_entries(xml_document(INDEX)) if re.search(r"/posts-\d+\.xml$", item[0])]
    if not index:
        raise RuntimeError("Express Tribune sitemap index contains no post sitemaps")
    # Sitemaps are newest first in fixed 25,000-row partitions. Read in sequence
    # until one partition's oldest timestamp is older than the window start.
    evidence.update({"strategy": "live_partitioned_post_sitemaps", "sitemap_index": INDEX, "sitemaps_checked": [], "sitemap_failures": [], "coverage_complete": False})
    entries = []
    reached_lower_boundary = False
    for sitemap_url, _ in sorted(index, key=lambda item: _sequence(item[0])):
        try:
            items = sitemap_entries(xml_document(sitemap_url))
            dated = [modified for _, modified in items if modified]
            evidence["sitemaps_checked"].append({"url": sitemap_url, "entries": len(items), "newest_lastmod": max(dated).isoformat() if dated else None, "oldest_lastmod": min(dated).isoformat() if dated else None})
            entries.extend(items)
            if dated and min(dated) < start:
                reached_lower_boundary = True
                break
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
            break
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"Express Tribune sitemap failure: {evidence['sitemap_failures'][0]}")
    candidates = [item for item in entries if item[1] is None or start <= item[1] <= end]
    candidates.sort(key=lambda item: item[1] or date.min)
    urls = unique_urls(candidates, Config.source_name, ())
    evidence.update({"lower_boundary_reached": reached_lower_boundary, "entries_before_window_filter": len(entries), "candidate_entries_in_window": len(candidates), "discovered_urls_after_filter": len(urls), "coverage_complete": reached_lower_boundary})
    return urls


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        # The publisher's magazine-style template has no h1 or story-text
        # wrapper.  Its headline and reported text live in these two containers.
        # Keep the standard selectors first so this fallback cannot replace a
        # normal article's canonical headline or body.
        title_selectors=("article h1", "h1", ".feed-content h3"),
        body_selectors=(".story-text", ".story-text-content", ".content-main", "article"),
        visible_date_selectors=("meta[property='article:published_time']", "time"),
        prefer_visible_date=False,
        evidence_body_selectors=(".story-text", ".story-text-content", ".content-main"),
        category_selectors=("meta[property='article:section']",),
        excluded_categories=EXCLUDED,
    )
