"""Observation-only Nation adapter using publisher daily sitemaps."""
from __future__ import annotations

import re
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from adapters.PAK.common import clean, extract_article, fetch, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "nation.com.pk"
    country_code = "PAK"
    is_env = False


DENY = ("/sports/", "/entertainment/", "/technology/", "/lifestyle/")


def _days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({
        "strategy": "publisher_daily_sitemaps",
        "sitemaps_checked": [],
        "sitemap_failures": [],
        "coverage_complete": False,
        "blacklist_url_patterns": list(DENY),
    })
    entries = []
    days = list(_days(start, end))

    def load(day):
        url = f"https://www.nation.com.pk/sitemap_{day.isoformat()}.xml"
        return day, url, sitemap_entries(xml_document(url))

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(load, day): day for day in days}
        for future in as_completed(futures):
            day = futures[future]
            url = f"https://www.nation.com.pk/sitemap_{day.isoformat()}.xml"
            try:
                _, _, items = future.result()
                results.append((day, url, items))
            except Exception as exc:
                evidence["sitemap_failures"].append({"url": url, "error": str(exc)})
    for _, url, items in sorted(results):
        try:
            evidence["sitemaps_checked"].append({"url": url, "entries": len(items)})
            entries.extend(items)
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} Nation daily sitemaps failed")
    candidates = [item for item in entries if item[1] is None or start <= item[1] <= end]
    candidates.sort(key=lambda item: item[1] or date.min)
    urls = unique_urls(candidates, Config.source_name, DENY)
    evidence.update({
        "entries_before_window_filter": len(entries),
        "entries_without_lastmod": sum(modified is None for _, modified in entries),
        "candidate_entries_in_window": len(candidates),
        "discovered_urls_after_filter": len(urls),
        "coverage_complete": True,
    })
    return urls


def _apply_verified_image_only_exclusion(article: dict, soup: BeautifulSoup) -> bool:
    """Classify a Nation publisher image post without admitting empty articles.

    Nation publishes cartoons and occasional image posts in the news sitemap.
    They carry an exact headline and publication date but deliberately have no
    prose body.  Only accept the exclusion when the empty article container and
    a publisher-hosted print image are both present on the page.
    """
    if clean(article.get("maintext")) or not article.get("title") or not article.get("date_publish"):
        return False
    content = soup.select_one("div.news-detail-content-class, .entry-content")
    if not content:
        return False
    text = clean(content.get_text(" ", strip=True)) or ""
    text = re.sub(r"\bShare on (?:Facebook|Twitter|WhatsApp|Whatsapp)\b", " ", text, flags=re.I)
    if clean(text):
        return False
    image = soup.select_one("meta[property='og:image'][content]")
    if not image:
        return False
    image_url = urljoin(article["url"], image.get("content"))
    parsed = urlparse(image_url)
    if (parsed.hostname or "").casefold() not in {"nation.com.pk", "www.nation.com.pk"}:
        return False
    if not parsed.path.startswith(("/print_images/", "/uploads/print_news/")):
        return False
    selector = "meta[property='og:image']"
    evidence = article.setdefault("field_evidence", {})
    evidence["publisher_images"] = [{"url": image_url, "selector": selector}]
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_image_only_publisher_item"
    article["exclusion_evidence"] = {
        "policy_type": "publisher_image_only",
        "publisher_image_url": image_url,
        "publisher_dom_selector": selector,
        "publisher_item_type": "image_post",
        "structured_article_body_empty": True,
    }
    return True


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1", ".entry-header h1", "h1"),
        body_selectors=("div.news-detail-content-class", ".entry-content", "article"),
        visible_date_selectors=("meta[property='article:published_time']", "time"),
        prefer_visible_date=False,
        evidence_body_selectors=("div.news-detail-content-class", ".entry-content"),
    )
    if not article.get("maintext"):
        soup = BeautifulSoup(fetch(url).content, "html.parser")
        _apply_verified_image_only_exclusion(article, soup)
    return article
