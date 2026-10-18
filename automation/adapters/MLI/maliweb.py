"""Observation-only Maliweb adapter using exact-dated category pages.

The single 49k-entry sitemap currently stops in April 2026, so it cannot cover
the September frontier. Discovery records that state and walks the four source
sections used historically, stopping only after each section crosses the
requested publication-date boundary.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.MLI.common import clean, extract_article, fetch, jsonld_objects, normalize_url, parse_datetime


class Config:
    source_name = "maliweb.net"
    country_code = "MLI"
    is_env = False


BASE = "https://www.maliweb.net"
CATEGORIES = ("politique", "economie", "international", "insecurite")
MAX_PAGES_PER_CATEGORY = 100
SITEMAP = BASE + "/sitemap.xml"
GALLERY_IMAGE_SELECTOR = "section#postDetailsPage .gallery-post-item .post-image img"


def _listing_rows(soup: BeautifulSoup) -> list[tuple[str, date]]:
    container = soup.select_one("div.col-sm-12.col-md-12.col-lg-8")
    if not container:
        return []
    rows = []
    for card in container.select("div.post-item"):
        link = card.select_one("h3.title a[href]")
        metadata = card.select_one("p.post-meta")
        normalized = normalize_url(link.get("href"), Config.source_name) if link else None
        if not normalized or not metadata:
            continue
        raw = clean(metadata.get_text(" ", strip=True)) or ""
        match = re.search(r"\b\d{1,2}\s+[A-Za-zÀ-ÿ]{3,12}\s+20\d{2}\s*-\s*\d{1,2}:\d{2}\b", raw)
        published = parse_datetime(match.group(0).replace("-", " ", 1)) if match else None
        if published:
            rows.append((normalized, published.date()))
    return rows


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    # Check the sitemap frontier without trusting it as an article date source.
    sitemap_response = None
    sitemap_error = None
    try:
        sitemap_response = fetch(SITEMAP, timeout=120, allow_browser=False)
    except Exception as exc:
        # The sitemap is already known to be stale and is diagnostic only.
        # Exact-dated category pages remain the bounded discovery frontier.
        sitemap_error = {"type": type(exc).__name__, "message": str(exc)[:500]}
    lastmods = re.findall(rb"<lastmod>(20\d{2}-\d{2}-\d{2})", sitemap_response.content) if sitemap_response else []
    newest_sitemap_date = max(value.decode("ascii") for value in lastmods) if lastmods else None
    evidence.update(
        {
            "method": "exact_dated_category_pagination",
            "categories": list(CATEGORIES),
            "sitemap": SITEMAP,
            "sitemap_bytes": len(sitemap_response.content) if sitemap_response else 0,
            "sitemap_diagnostic_failure": sitemap_error,
            "sitemap_newest_lastmod": newest_sitemap_date,
            "sitemap_usable_for_window": bool(newest_sitemap_date and newest_sitemap_date >= start.isoformat()),
            "pages_checked": [],
            "coverage_boundaries": {},
        }
    )
    by_category: dict[str, list[str]] = {}
    for category in CATEGORIES:
        urls = []
        reached_boundary = False
        for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
            listing_url = f"{BASE}/category/{category}?page={page}"
            listing_response = fetch(listing_url)
            soup = BeautifulSoup(listing_response.content, "html.parser")
            rows = _listing_rows(soup)
            dates = [published for _, published in rows]
            row_evidence = {
                "category": category, "page": page, "url": listing_url, "articles": len(rows),
                "transport": getattr(listing_response, "scrape_transport", "requests"),
            }
            if dates:
                row_evidence.update({"newest": max(dates).isoformat(), "oldest": min(dates).isoformat()})
            evidence["pages_checked"].append(row_evidence)
            if not rows:
                evidence["coverage_boundaries"][category] = {"page": page, "reason": "empty_or_undated"}
                reached_boundary = True
                break
            urls.extend(url for url, published in rows if start <= published <= end)
            if max(dates) < start:
                evidence["coverage_boundaries"][category] = {
                    "page": page,
                    "reason": "full_page_before_start",
                    "newest": max(dates).isoformat(),
                }
                reached_boundary = True
                break
        if not reached_boundary:
            raise RuntimeError(f"Maliweb category {category!r} reached page cap without a date boundary")
        by_category[category] = list(dict.fromkeys(urls))

    discovered = []
    for index in range(max((len(urls) for urls in by_category.values()), default=0)):
        for category in CATEGORIES:
            if index < len(by_category[category]):
                discovered.append(by_category[category][index])
    result = list(dict.fromkeys(discovered))
    evidence["urls_by_category"] = {category: len(urls) for category, urls in by_category.items()}
    evidence["discovered_urls_after_dedup"] = len(result)
    return result


def _apply_verified_photo_gallery_exclusion(article: dict, soup: BeautifulSoup) -> bool:
    """Mark a media-only gallery only when the publisher template proves it.

    Ordinary articles and generic empty pages continue through main-text
    validation. The observer also rechecks title, publication date, image, and
    main-text-only failure before accepting this exclusion.
    """
    if clean(article.get("maintext")) or not article.get("title") or not article.get("date_publish"):
        return False
    gallery = soup.select_one("section#postDetailsPage .gallery-post-item")
    image = soup.select_one(GALLERY_IMAGE_SELECTOR)
    if not gallery or not image:
        return False
    counter_match = re.search(r"\b\d+\s*/\s*(\d+)\b", clean(gallery.get_text(" ", strip=True)) or "")
    item_count = int(counter_match.group(1)) if counter_match else 0
    if item_count < 2:
        return False
    news_articles = []
    for item in jsonld_objects(soup):
        item_type = item.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if "NewsArticle" in types:
            news_articles.append(item)
    if not news_articles or any(clean(item.get("articleBody")) for item in news_articles):
        return False
    image_url = clean(image.get("src") or image.get("data-src"))
    if not image_url:
        return False
    image_url = urljoin(BASE, image_url)
    image_alt = clean(image.get("alt"))
    article["field_evidence"]["bodies"] = []
    article["field_evidence"]["publisher_images"] = [{
        "url": image_url, "alt": image_alt, "source": GALLERY_IMAGE_SELECTOR,
    }]
    article["field_evidence"]["publisher_galleries"] = [{
        "selector": "section#postDetailsPage .gallery-post-item",
        "item_count": item_count,
        "image_url": image_url,
        "structured_article_body_empty": True,
    }]
    article["collection"] = "SKIP"
    # The generic extractor uses this marker when a page has no text. Once the
    # page is proven to be a publisher gallery, missing text is expected and
    # must not survive as a parser failure in the policy exclusion artifact.
    article["parser_error"] = None
    article["exclusion_reason"] = "excluded_image_only_publisher_item"
    article["exclusion_evidence"] = {
        "policy_type": "publisher_image_only",
        "publisher_item_type": "photo_gallery",
        "publisher_dom_selector": GALLERY_IMAGE_SELECTOR,
        "publisher_gallery_selector": "section#postDetailsPage .gallery-post-item",
        "publisher_image_url": image_url,
        "publisher_image_alt": image_alt,
        "gallery_item_count": item_count,
        "structured_article_body_empty": True,
    }
    return True


def scrape_url(url: str) -> dict:
    article, soup = extract_article(
        url=url,
        domain=Config.source_name,
        title_selectors=("section#postDetailsPage h1", "main h1", "h1"),
        body_selectors=("section#postDetailsPage .post-text", ".post-content .post-text", ".post-text"),
        parser="maliweb_visible_h1_jsonld_published_post_text",
    )
    _apply_verified_photo_gallery_exclusion(article, soup)
    return article
