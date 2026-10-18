"""Observation-only Pakistan Observer adapter with live sitemap rollover."""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from adapters.PAK.common import clean, extract_article, fetch, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "pakobserver.net"
    country_code = "PAK"
    is_env = False


INDEX = "https://pakobserver.net/sitemap_index.xml"
EXCLUDED = ("Sports", "technology", "Lifestyle", "Daily Cartoon")


def _sequence(url: str) -> int:
    match = re.search(r"post-sitemap(\d*)\.xml", url)
    return int(match.group(1) or 1) if match else -1


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower = start - timedelta(days=30)
    index = [
        item for item in sitemap_entries(xml_document(INDEX))
        if re.search(r"post-sitemap\d+\.xml$", item[0])
    ]
    selected = [item for item in index if item[1] is None or item[1] >= lower]
    selected = sorted(selected, key=lambda item: _sequence(item[0]))
    evidence.update({
        "strategy": "live_wordpress_sitemap_index",
        "sitemap_index": INDEX,
        "post_sitemaps_total": len(index),
        "post_sitemaps_selected": len(selected),
        "selection_overlap_start": lower.isoformat(),
        "sitemaps_checked": [],
        "sitemap_failures": [],
        "coverage_complete": False,
    })
    entries = []
    for sitemap_url, modified in selected:
        try:
            items = sitemap_entries(xml_document(sitemap_url))
            evidence["sitemaps_checked"].append({"url": sitemap_url, "index_lastmod": modified.isoformat() if modified else None, "entries": len(items)})
            entries.extend(items)
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} selected Pakistan Observer sitemaps failed")
    candidates = [item for item in entries if item[1] is None or start <= item[1] <= end + timedelta(days=1)]
    candidates.sort(key=lambda item: item[1] or date.min)
    urls = unique_urls(candidates, Config.source_name, ())
    evidence.update({"entries_before_window_filter": len(entries), "candidate_entries_in_window": len(candidates), "discovered_urls_after_filter": len(urls), "coverage_complete": True})
    return urls


def _apply_verified_video_only_exclusion(article: dict, soup: BeautifulSoup) -> bool:
    if clean(article.get("maintext")) or not article.get("title") or not article.get("date_publish"):
        return False
    container = soup.select_one("div.single__content.brxe-post-content, div.single__content")
    videos = container.select("div.perfmatters-lazy-video[data-provider][data-src]") if container else []
    ordinary_text = clean(container.get_text(" ", strip=True)) if container else None
    if len(videos) != 1 or ordinary_text:
        return False
    video = videos[0]
    provider = clean(video.get("data-provider"))
    embed_url = clean(video.get("data-src") or video.get("data-id"))
    thumbnail = video.select_one("img[data-src], img[src]")
    thumbnail_url = clean(thumbnail.get("data-src") or thumbnail.get("src")) if thumbnail else None
    if provider != "youtube" or not embed_url or not re.match(r"https://www\.youtube\.com/embed/[A-Za-z0-9_-]+", embed_url):
        return False
    if not thumbnail_url or "ytimg.com/" not in thumbnail_url:
        return False
    video_evidence = {
        "provider": provider,
        "selector": "div.single__content div.perfmatters-lazy-video",
        "embed_url": embed_url,
        "thumbnail_url": thumbnail_url,
        "content_text_empty": True,
    }
    article["field_evidence"]["publisher_videos"] = [video_evidence]
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_video_only_publisher_item"
    article["exclusion_evidence"] = {
        "policy_type": "publisher_video_only",
        **video_evidence,
    }
    return True


def _apply_verified_image_only_exclusion(article: dict, soup: BeautifulSoup) -> bool:
    evidence = article.get("field_evidence") or {}
    category = clean(evidence.get("publisher_category"))
    title = clean(article.get("title")) or ""
    if (
        clean(article.get("maintext"))
        or not article.get("date_publish")
        or category != "Daily Cartoon"
        or not title.casefold().startswith("daily cartoon")
    ):
        return False
    container = soup.select_one("div.single__content.brxe-post-content, div.single__content")
    if container and clean(container.get_text(" ", strip=True)):
        return False
    image = soup.select_one("main img.single__img[src], article img.single__img[src]")
    if not image:
        return False
    image_url = urljoin(article["url"], image.get("src"))
    if (urlparse(image_url).hostname or "").casefold() not in {"pakobserver.net", "www.pakobserver.net"}:
        return False
    selector = "main img.single__img[src]"
    evidence["publisher_images"] = [{
        "url": image_url,
        "alt": clean(image.get("alt")),
        "selector": selector,
    }]
    article["field_evidence"] = evidence
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_image_only_publisher_item"
    article["exclusion_evidence"] = {
        "policy_type": "publisher_image_only",
        "publisher_image_url": image_url,
        "publisher_dom_selector": selector,
        "publisher_category": category,
        "structured_article_body_empty": True,
    }
    return True


def _apply_verified_link_only_exclusion(article: dict, soup: BeautifulSoup) -> bool:
    if clean(article.get("maintext")) or not article.get("title") or not article.get("date_publish"):
        return False
    container = soup.select_one("div.single__content.brxe-post-content, div.single__content")
    links = container.select("a[href]") if container else []
    if len(links) != 1:
        return False
    link = links[0]
    target_url = urljoin(article["url"], link.get("href"))
    if (urlparse(target_url).hostname or "").casefold() != "epaper.pakobserver.net":
        return False
    clone = BeautifulSoup(str(container), "html.parser")
    for node in clone.select("a[href]"):
        node.decompose()
    if clean(clone.get_text(" ", strip=True)):
        return False
    link_text = clean(link.get_text(" ", strip=True))
    if not link_text:
        return False
    selector = "div.single__content a[href]"
    link_evidence = {
        "target_url": target_url,
        "selector": selector,
        "link_text": link_text,
        "content_text_empty": True,
    }
    article["field_evidence"]["publisher_links"] = [link_evidence]
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_link_only_publisher_item"
    article["exclusion_evidence"] = {"policy_type": "publisher_link_only", **link_evidence}
    return True


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1", "h1"),
        body_selectors=(".single__content", ".brxe-post-content", "article"),
        visible_date_text_selectors=(".entry-meta", "time"),
        prefer_visible_date=False,
        evidence_body_selectors=(".single__content", ".brxe-post-content"),
        category_selectors=("li.current-post-parent", "meta[property='article:section']"),
        excluded_categories=EXCLUDED,
    )
    if not article.get("maintext"):
        response = fetch(url)
        soup = BeautifulSoup(response.content, "html.parser")
        image_only = _apply_verified_image_only_exclusion(article, soup)
        if not image_only and article.get("collection") != "SKIP":
            link_only = _apply_verified_link_only_exclusion(article, soup)
            if not link_only:
                _apply_verified_video_only_exclusion(article, soup)
    return article
