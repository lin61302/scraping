"""Pure 168.am adapter using its live multilingual WordPress sitemaps."""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlparse

from adapters.ARM.common import apply_youtube_only_exclusion, extract_article, wordpress_sitemap_window


class Config:
    source_name = "168.am"
    country_code = "ARM"
    is_env = False


SITES = (
    ("https://168.am/wp-sitemap.xml", ("168.am",), "hy"),
    ("https://en.168.am/wp-sitemap.xml", ("en.168.am",), "en"),
    ("https://ru.168.am/wp-sitemap.xml", ("ru.168.am",), "ru"),
)
DENY = ("/gallery/", "/tag/", "/author/", "/category/", "/tv_show/")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    result, seen, sites = [], set(), []
    for index, domains, language in SITES:
        site_evidence = {"language": language}
        urls = wordpress_sitemap_window(
            index_url=index, domains=domains, start=start, end=end,
            evidence=site_evidence, deny=DENY,
        )
        sites.append(site_evidence)
        for url in urls:
            if url not in seen:
                seen.add(url)
                result.append(url)
    evidence.update({
        "strategy": "multilingual_wordpress_sitemap_backwalk",
        "sites": sites,
        "coverage_complete": all(item["coverage_complete"] for item in sites),
        "discovered_urls_after_filter": len(result),
    })
    return result


def scrape_url(url: str) -> dict:
    host = (urlparse(url).hostname or "").lower()
    language = "en" if host.startswith("en.") else "ru" if host.startswith("ru.") else "hy"
    article = extract_article(
        url,
        source=Config.source_name,
        language=language,
        title_selectors=("h1.single-title", "article h1", "h1"),
        body_selectors=(".single-content-wrapper", ".single-content-block", "article"),
        visible_date_selectors=(".single-meta",),
        prefer_visible_date=True,
        evidence_body_selectors=(".single-content-wrapper", ".single-content-block"),
    )
    # The theme's visible timestamp is embedded in a metadata sentence which
    # the shared evidence parser cannot reliably tokenize.  The canonical
    # permalink independently exposes the publisher date and is the same
    # first-party signal used by the legacy parser.
    match = re.search(r"/(20\d{2})/(\d{2})/(\d{2})/", url)
    if match:
        permalink_date = date(*map(int, match.groups()))
        article["date_publish"] = datetime.combine(permalink_date, datetime.min.time())
        article["field_sources"]["date_publish"] = "canonical_permalink_date"
        article["field_evidence"]["dates"].append({
            "kind": "published",
            "source": "canonical_permalink_date",
            "raw": permalink_date.isoformat(),
        })
    apply_youtube_only_exclusion(article)
    return article
