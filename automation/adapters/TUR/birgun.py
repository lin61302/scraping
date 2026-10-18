"""Observation-only BirGün environmental adapter.

Discovery checks the live sitemap frontier and uses the Çevre category pages
to keep the result source-specific. Importing or running this module never
opens MongoDB and never writes production data.
"""
from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.TUR.common import (
    CHALLENGE,
    article_body,
    clean,
    fetch,
    jsonld_objects,
    normalize_url,
    parse_datetime,
    page_evidence,
    published_date,
    sitemap_entries,
    xml_root,
)


class Config:
    source_name = "birgun.net"
    country_code = "TUR"
    is_env = True


INDEX = "https://www.birgun.net/sitemap.xml"
CATEGORY = "https://www.birgun.net/kategori/cevre-15?p={page}"
MAX_CATEGORY_PAGES = 200


def _listing_date(node) -> date | None:
    container = node.find_parent("div", class_="card-body") or node.parent
    if container is None:
        return None
    for item in container.select("li.nav-item, time"):
        raw = clean(item.get("datetime") or item.get_text(" ", strip=True))
        if not raw:
            continue
        try:
            return datetime.strptime(raw, "%d.%m.%Y %H:%M").date()
        except ValueError:
            parsed = parse_datetime(raw, ["tr"])
            if parsed:
                return parsed.date()
    return None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({
        "sitemap_index": INDEX,
        "category_pattern": CATEGORY,
        "sitemaps_checked": [],
        "sitemap_failures": [],
        "listing_pages_checked": [],
        "listing_page_failures": [],
    })

    frontier_urls: set[str] = set()
    try:
        index_entries = sitemap_entries(xml_root(INDEX))
        evidence["sitemaps_checked"].append({"url": INDEX, "entries": len(index_entries)})
        news_sitemaps = [url for url, _ in index_entries if url.rstrip("/").endswith("/sitemap/haberler")]
        if not news_sitemaps:
            raise RuntimeError("live sitemap index has no /sitemap/haberler entry")
        for sitemap in news_sitemaps:
            items = sitemap_entries(xml_root(sitemap))
            evidence["sitemaps_checked"].append({"url": sitemap, "entries": len(items)})
            for raw, _ in items:
                normalized = normalize_url(raw, Config.source_name)
                if normalized:
                    frontier_urls.add(normalized)
    except Exception as exc:
        evidence["sitemap_failures"].append({"url": INDEX, "error": str(exc)})
        raise RuntimeError(f"BirGün live sitemap discovery failed: {exc}") from exc

    discovered: list[str] = []
    seen: set[str] = set()
    old_page_streak = 0
    missing_listing_dates = 0
    for page in range(1, MAX_CATEGORY_PAGES + 1):
        listing_url = CATEGORY.format(page=page)
        try:
            response = fetch(listing_url)
            soup = BeautifulSoup(response.content, "html.parser")
            nodes = soup.select("h2.card-title a[href]")
        except Exception as exc:
            evidence["listing_page_failures"].append({"url": listing_url, "page": page, "error": str(exc)})
            raise RuntimeError(f"BirGün category page {page} failed: {exc}") from exc

        if not nodes:
            evidence["listing_pages_checked"].append({"url": listing_url, "page": page, "urls": 0, "stop": "empty"})
            break

        page_dates: list[date] = []
        page_added = 0
        for node in nodes:
            normalized = normalize_url(urljoin("https://www.birgun.net", node.get("href")), Config.source_name)
            listed = _listing_date(node)
            if listed:
                page_dates.append(listed)
            else:
                missing_listing_dates += 1
            if not normalized or normalized in seen:
                continue
            if listed is None or start <= listed <= end:
                seen.add(normalized)
                discovered.append(normalized)
                page_added += 1

        evidence["listing_pages_checked"].append({
            "url": listing_url,
            "page": page,
            "urls": len(nodes),
            "added_in_window": page_added,
            "newest_listing_date": max(page_dates).isoformat() if page_dates else None,
            "oldest_listing_date": min(page_dates).isoformat() if page_dates else None,
        })
        if page_dates and max(page_dates) < start:
            old_page_streak += 1
        else:
            old_page_streak = 0
        if old_page_streak >= 2:
            evidence["listing_stop"] = "two consecutive pages entirely older than requested start"
            break
    else:
        raise RuntimeError(f"BirGün category pagination exceeded {MAX_CATEGORY_PAGES} pages without a date boundary")

    evidence["sitemap_frontier_url_count"] = len(frontier_urls)
    evidence["category_urls_matching_sitemap_frontier"] = sum(url in frontier_urls for url in discovered)
    evidence["listing_items_without_dates"] = missing_listing_dates
    evidence["discovered_urls_before_filter"] = sum(row["urls"] for row in evidence["listing_pages_checked"])
    evidence["discovered_urls_after_filter"] = len(discovered)
    return discovered


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")

    title_node = soup.select_one("article h1, main h1, h1")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    title_selector = "article h1, main h1, h1" if title else None
    body = None
    body_selector = None
    raw_date = None
    published = None
    date_selector = None
    for obj in jsonld_objects(soup):
        if not title and obj.get("headline"):
            title = clean(obj.get("headline"))
            title_selector = "jsonld:headline"
        if not body and obj.get("articleBody"):
            body = clean(obj.get("articleBody"))
            body_selector = "jsonld:articleBody"
        if not published and obj.get("datePublished"):
            raw_date = clean(obj.get("datePublished"))
            published = parse_datetime(raw_date, ["tr"])
            if published:
                date_selector = "jsonld:datePublished"

    if not body:
        body, body_selector = article_body(
            soup,
            ("[itemprop='articleBody']", ".article-content", ".news-content", ".detail-content", "main"),
        )
    if not published:
        published, date_selector, raw_date = published_date(soup)

    parser = "birgun_jsonld+site_selectors"
    if not title or not body or not published:
        try:
            from newsplease import NewsPlease

            candidate = NewsPlease.from_html(response.text, url=url)
            title = title or clean(candidate.title)
            body = body or clean(candidate.maintext)
            published = published or candidate.date_publish
            parser += "+newsplease"
        except Exception:
            pass
    if published and published.tzinfo:
        published = published.replace(tzinfo=None)

    missing = [name for name, value in (("title", title), ("maintext", body), ("date_publish", published)) if not value]
    problem = "missing_" + "_".join(missing) if missing else None
    if CHALLENGE.search((title or "")[:180]) or CHALLENGE.search((body or "")[:180]):
        problem = "challenge_or_error_page"
    return {
        "url": url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "source": Config.source_name,
        "country": Config.country_code,
        "title": title,
        "maintext": body,
        "date_publish": published,
        "parser": parser,
        "parser_error": problem,
        "field_evidence": page_evidence(
            soup,
            title=title,
            title_selector=title_selector,
            date_raw=raw_date,
            date_source=date_selector,
            body=body,
            body_selector=body_selector,
            status=response.status_code,
        ),
    }
