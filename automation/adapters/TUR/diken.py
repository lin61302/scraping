"""Observation-only Diken adapter following the live post sitemap index."""
from __future__ import annotations

from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters.TUR.common import clean, extract_article, fetch, sitemap_entries, unique_article_urls, xml_root


class Config:
    source_name = "diken.com.tr"
    country_code = "TUR"
    is_env = False


INDEX = "https://www.diken.com.tr/sitemap.xml"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index = xml_root(INDEX)
    posts = [(url, modified) for url, modified in sitemap_entries(index) if "/post-sitemap" in url]
    if not posts:
        raise RuntimeError("live Diken sitemap index has no post sitemaps")
    # The index is newest first. Include every sitemap modified near or after
    # the requested start and one older boundary sitemap. This is a discovery
    # heuristic, not a publication-date claim; article-page dates are checked
    # separately after fetching each URL.
    threshold = start - timedelta(days=2)
    chosen = []
    for url, modified in posts:
        chosen.append((url, modified))
        if modified and modified < threshold:
            break
    evidence.update({"sitemap_index": INDEX, "index_post_sitemap_count": len(posts),
                     "selected_post_sitemap_count": len(chosen), "selection_threshold": threshold.isoformat(),
                     "sitemaps_checked": [], "sitemap_failures": []})
    entries = []
    for url, modified in chosen:
        try:
            items = sitemap_entries(xml_root(url))
            evidence["sitemaps_checked"].append({"url": url, "entries": len(items), "index_lastmod": modified.isoformat() if modified else None})
            entries.extend(items)
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} selected Diken post sitemaps failed")
    evidence["discovered_urls_before_filter"] = len(entries)
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    undated = sum(modified is None for _, modified in entries)
    entries = [(url, modified) for url, modified in entries if modified is None or lower <= modified <= upper]
    evidence["sitemap_lastmod_candidate_window"] = {"start": lower.isoformat(), "end": upper.isoformat()}
    evidence["sitemap_entries_without_lastmod"] = undated
    evidence["sitemap_candidates_in_window"] = len(entries)
    # The current WordPress sitemap mixes a small number of updated historical
    # URLs with current posts. Lastmod only prioritizes observation candidates;
    # the article-page publication date remains authoritative.
    entries.sort(key=lambda item: item[1] or date.min, reverse=True)
    urls = unique_article_urls(entries, Config.source_name)
    evidence["discovered_urls_after_filter"] = len(urls)
    return urls


def _body_with_lists(soup: BeautifulSoup) -> str | None:
    node = soup.select_one("article .td-post-content")
    if not node:
        return None
    parts = []
    for item in node.select("p, h2, li"):
        value = clean(item.get_text(" ", strip=True))
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts) or None


def scrape_url(url: str) -> dict:
    article = extract_article(
        url, Config.source_name,
        body_selectors=("div.tdb-block-inner.td-fix-index", "div.entry-content", "article .td-post-content", "article"),
        visible_date_selectors=("time.entry-date[datetime]",),
        title_selectors=("h1.tdb-title-text", "h2.entry-title", "h1"),
    )
    if len((article.get("maintext") or "").split()) < 40:
        response = fetch(url)
        body = _body_with_lists(BeautifulSoup(response.content, "html.parser"))
        if body and len(body) > len(article.get("maintext") or ""):
            article["maintext"] = body
            article["parser"] = f"{article.get('parser', 'site_selectors')}+diken_list_items"
            article["field_evidence"]["bodies"] = [{"kind": "article .td-post-content:p,h2,li", "text": body[:20000]}]
    return article
