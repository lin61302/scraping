"""Observation adapter for English-language Balkan Insight articles."""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

from adapters.SRB.common import extract_article, normalize_url, sitemap_rows, xml_document


class Config:
    source_name = "balkaninsight.com"
    country_code = "SRB"
    is_env = False


INDEX = "https://balkaninsight.com/sitemap_index.xml"
TRANSLATION_PREFIX = re.compile(r"^/(?:sq|sr|mk|ro|bg|tr|gr|hu|ba)/", re.I)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index_rows = sitemap_rows(xml_document(INDEX))
    all_posts = [(url, stamp) for url, stamp in index_rows if re.search(r"/post-sitemap\d*\.xml$", url)]
    candidates = [(url, stamp) for url, stamp in all_posts if stamp is None or stamp.date() >= start]
    if candidates and all_posts.index(candidates[0]) > 0:
        candidates.insert(0, all_posts[all_posts.index(candidates[0]) - 1])
    selected = {}
    checked = []
    for sitemap_url, _ in candidates:
        rows = sitemap_rows(xml_document(sitemap_url))
        checked.append({"url": sitemap_url, "rows": len(rows)})
        for raw, stamp in rows:
            article_url = normalize_url(raw, Config.source_name, deny=("/author/", "/tag/"))
            path = urlparse(article_url).path if article_url else ""
            if (
                not article_url
                or TRANSLATION_PREFIX.match(path)
                or not re.match(r"^/20\d{2}/\d{2}/\d{2}/[^/]+(?:/[^/]+)?$", path)
            ):
                continue
            if stamp is None or start <= stamp.date() <= end:
                selected[article_url] = stamp.date() if stamp else None
    evidence.update({
        "strategy": "dynamic_wordpress_sitemap_index",
        "language_scope": "English root paths; publisher translations excluded",
        "sitemaps_checked": checked,
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": bool(candidates),
        "registry_note": "Active SRB major-regional source missing from the SRB Notion list at audit time",
    })
    return sorted(selected, key=lambda url: (selected[url] or date.min, url))


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1", ".btArticleHeadline h1", "h1"),
        body_selectors=(".btArticleBody", ".btArticleContent", "article"),
        visible_date_selectors=('meta[property="article:published_time"]',),
        evidence_body_selectors=(".btArticleBody",),
        language="en",
    )
