"""Observation adapter for Južne Vesti using sitemaps or its public RSS archive."""
from __future__ import annotations

from datetime import date
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from adapters.SRB.common import AccessBlockedError, extract_article, fetch, normalize_url, sitemap_rows, xml_document


class Config:
    source_name = "juznevesti.com"
    country_code = "SRB"
    is_env = False


INDEX = "https://www.juznevesti.com/sitemap_index.xml"
FEED = "https://www.juznevesti.com/feed/?paged={page}"
DENY = ("/author/", "/tag/", "/bbc-news-na-srpskom/")
_BROWSER_ONLY = False


def _rss_window(start: date, end: date, evidence: dict) -> list[str]:
    """Backwalk the publisher's paged RSS feed when Cloudflare blocks XML sitemaps."""
    selected: dict[str, date] = {}
    pages = []
    reached_cutoff = False
    for page in range(1, 201):
        feed_url = FEED.format(page=page)
        soup = BeautifulSoup(fetch(feed_url).content, "xml")
        items = soup.find_all("item")
        if not items:
            break
        dated = []
        for item in items:
            link = item.find("link")
            published_node = item.find("pubDate")
            if not link or not published_node:
                continue
            try:
                published = parsedate_to_datetime(published_node.get_text(strip=True)).date()
            except (TypeError, ValueError, OverflowError):
                continue
            dated.append(published)
            article_url = normalize_url(link.get_text(strip=True), Config.source_name, deny=DENY)
            if article_url and start <= published <= end:
                selected[article_url] = published
        pages.append({
            "url": feed_url,
            "rows": len(items),
            "oldest": min(dated).isoformat() if dated else None,
            "newest": max(dated).isoformat() if dated else None,
        })
        if dated and min(dated) < start:
            reached_cutoff = True
            break
    evidence.update({
        "strategy": "paged_publisher_rss_backwalk",
        "sitemap_blocked": True,
        "pages_checked": pages,
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": reached_cutoff,
    })
    return [url for url, _ in sorted(selected.items(), key=lambda item: (item[1], item[0]))]


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    try:
        index_rows = sitemap_rows(xml_document(INDEX))
    except AccessBlockedError:
        return _rss_window(start, end, evidence)
    sitemaps = [(url, stamp) for url, stamp in index_rows if "post-sitemap" in url]
    selected = {}
    checked = []
    reached_cutoff = False
    for sitemap_url, _ in sitemaps:
        try:
            rows = sitemap_rows(xml_document(sitemap_url))
        except AccessBlockedError as exc:
            evidence["blocked_sitemap"] = sitemap_url
            evidence["sitemap_error"] = str(exc)
            return _rss_window(start, end, evidence)
        dated = [stamp.date() for _, stamp in rows if stamp]
        checked.append({"url": sitemap_url, "rows": len(rows), "oldest": min(dated).isoformat() if dated else None, "newest": max(dated).isoformat() if dated else None})
        for raw, stamp in rows:
            article_url = normalize_url(raw, Config.source_name, deny=DENY)
            if article_url and (stamp is None or start <= stamp.date() <= end):
                selected[article_url] = stamp.date() if stamp else None
        if dated and max(dated) < start:
            reached_cutoff = True
            break
    evidence.update({"strategy": "newest_first_wordpress_sitemap_shards", "sitemaps_checked": checked, "discovered_urls_after_filter": len(selected), "coverage_complete": reached_cutoff})
    return sorted(selected, key=lambda url: (selected[url] or date.min, url))


def scrape_url(url: str) -> dict:
    global _BROWSER_ONLY
    if not _BROWSER_ONLY:
        try:
            return extract_article(
                url,
                source=Config.source_name,
                title_selectors=("article h1", ".single-post h1", "h1"),
                body_selectors=(".prose.max-w-none", ".entry-content", ".post-content", ".td-post-content", "article"),
                visible_date_selectors=('meta[property="article:published_time"]', "time[datetime]"),
                evidence_body_selectors=(".prose.max-w-none",),
            )
        except AccessBlockedError:
            # Once the publisher starts challenging article requests, keep the
            # remainder of this run on one reusable browser session.  Repeating
            # four blocked HTTP attempts for every subsequent URL only raises
            # the publisher's request rate without adding evidence.
            _BROWSER_ONLY = True

    from adapters.SRB.juznevesti_browser import extract_article_browser

    return extract_article_browser(url, source=Config.source_name)
