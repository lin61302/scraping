"""Observation adapter for the general Danas news source."""
from __future__ import annotations

from datetime import date

from adapters.SRB.common import extract_article, normalize_url, sitemap_rows, xml_document


class Config:
    source_name = "danas.rs"
    country_code = "SRB"
    is_env = False


SITEMAP = "https://www.danas.rs/post-sitemap{}.xml"
KNOWN_GOOD_SITEMAP = 761


def _rows(number: int):
    return sitemap_rows(xml_document(SITEMAP.format(number)))


def _terminal_sitemap() -> int:
    low = KNOWN_GOOD_SITEMAP if _rows(KNOWN_GOOD_SITEMAP) else 1
    step = 1
    high = low + step
    while _rows(high):
        low = high
        step *= 2
        high = low + step
        if high > 4096:
            raise RuntimeError("Danas sitemap ceiling exceeded safety bound")
    while low + 1 < high:
        middle = (low + high) // 2
        if _rows(middle):
            low = middle
        else:
            high = middle
    return low


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    terminal = _terminal_sitemap()
    selected = {}
    checked = []
    reached_cutoff = False
    for number in range(terminal, 0, -1):
        rows = _rows(number)
        dated = [stamp.date() for _, stamp in rows if stamp]
        checked.append({"number": number, "rows": len(rows), "oldest": min(dated).isoformat() if dated else None, "newest": max(dated).isoformat() if dated else None})
        for raw, stamp in rows:
            article_url = normalize_url(raw, "danas.rs", deny=("/author/", "/tag/"))
            if article_url and (stamp is None or start <= stamp.date() <= end):
                selected[article_url] = stamp.date() if stamp else None
        if dated and max(dated) < start:
            reached_cutoff = True
            break
    evidence.update({
        "strategy": "dynamic_terminal_numbered_post_sitemaps",
        "terminal_sitemap": terminal,
        "sitemaps_checked": checked,
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": reached_cutoff,
    })
    return sorted(selected, key=lambda url: (selected[url] or date.min, url))


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1.post-title", "h1.post-title", "h1"),
        body_selectors=("article .post-content", ".post-content.content", "article"),
        visible_date_selectors=('meta[property="article:published_time"]',),
        evidence_body_selectors=("article .post-content",),
    )
