"""Observation-only Yeşil Gazete adapter using the live post sitemap index.

Sitemap ``lastmod`` values bound discovery. Publication dates always come from
the article page. This module has no MongoDB dependency or write path.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from adapters.TUR.common import extract_article, sitemap_entries, unique_article_urls, xml_root


class Config:
    source_name = "yesilgazete.org"
    country_code = "TUR"
    is_env = True


INDEX = "https://yesilgazete.org/sitemap_index.xml"
POST_SITEMAP = re.compile(r"/post-sitemap(\d+)\.xml$")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index_entries = sitemap_entries(xml_root(INDEX))
    children = []
    for url, modified in index_entries:
        match = POST_SITEMAP.search(url)
        if match:
            children.append((int(match.group(1)), url, modified))
    children.sort()
    if not children or children[0][0] != 1:
        raise RuntimeError("live Yeşil Gazete sitemap index has no leading post-sitemap1.xml")

    threshold = start - timedelta(days=2)
    evidence.update({
        "sitemap_index": INDEX,
        "index_post_sitemap_count": len(children),
        "selection_threshold": threshold.isoformat(),
        "sitemaps_checked": [],
        "sitemap_failures": [],
    })
    candidate_entries = []
    boundary_reached = False
    for number, url, index_modified in children:
        try:
            items = sitemap_entries(xml_root(url))
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": url, "number": number, "error": str(exc)})
            raise RuntimeError(f"Yeşil Gazete post sitemap {number} failed: {exc}") from exc

        dated = [modified for _, modified in items if modified]
        newest = max(dated) if dated else None
        oldest = min(dated) if dated else None
        evidence["sitemaps_checked"].append({
            "url": url,
            "number": number,
            "entries": len(items),
            "index_lastmod": index_modified.isoformat() if index_modified else None,
            "newest_entry_lastmod": newest.isoformat() if newest else None,
            "oldest_entry_lastmod": oldest.isoformat() if oldest else None,
        })
        # lastmod only selects candidates. The article-page publication date
        # remains the authority and is checked by the observer.
        upper = end + timedelta(days=1)
        candidate_entries.extend(
            (raw, modified) for raw, modified in items
            if modified is None or threshold <= modified <= upper
        )
        if dated and newest < threshold:
            boundary_reached = True
            evidence["sitemap_stop"] = f"post-sitemap{number}.xml is entirely older than discovery threshold"
            break

    if not boundary_reached:
        raise RuntimeError("Yeşil Gazete post sitemaps did not reach the requested date boundary")
    evidence["discovered_urls_before_filter"] = len(candidate_entries)
    urls = unique_article_urls(candidate_entries, Config.source_name, deny=("/wp-content/", "/author/", "/page/"))
    evidence.update({
        "discovered_urls_after_filter": len(urls),
        "coverage_complete": boundary_reached and not evidence["sitemap_failures"],
        "publication_inactive_for_window": boundary_reached and not evidence["sitemap_failures"] and len(urls) == 0,
    })
    return urls


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        Config.source_name,
        body_selectors=("article .td-post-content", ".td-post-content", ".tdb_single_content", "article"),
        visible_date_selectors=("time.entry-date.updated.td-module-date[datetime]",),
        title_selectors=("h1.tdb-title-text", ".tdb-title-text", "article h1", "h1"),
    )
    article["source"] = Config.source_name
    article["country"] = Config.country_code
    return article
