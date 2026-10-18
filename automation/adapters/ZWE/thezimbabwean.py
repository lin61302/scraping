"""Observation-only adapter for thezimbabwean.co. No database writes."""
from __future__ import annotations

from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.ZWE.common import wordpress_rest_article, wordpress_rest_discovery


class Config:
    source_name = "thezimbabwean.co"
    country_code = "ZWE"
    is_env = False


INDEX = "https://www.thezimbabwean.co/wp-sitemap.xml"
UNINTERESTED_CLASSES = {
    "category-entertainment", "category-sport", "category-arts", "category-music",
    "category-lifestyle", "category-tech", "category-travel",
}
OPINION_CLASSES = {"category-opinions", "category-ppinions", "category-opinions-analysis"}
POST_CACHE: dict[str, dict] = {}
CATEGORY_SLUGS: dict[int, str] = {}


def _apply_verified_image_only_exclusion(article: dict, post: dict) -> bool:
    if len(article.get("maintext") or "") >= 100:
        return False
    content_html = (post.get("content") or {}).get("rendered") or ""
    soup = BeautifulSoup(f"<article>{content_html}</article>", "html.parser")
    ordinary_paragraphs = [
        node for node in soup.select("p, h2")
        if "wp-caption-text" not in (node.get("class") or [])
        and len(node.get_text(" ", strip=True)) >= 20
    ]
    images = soup.select("img[src], img[data-src]")
    if ordinary_paragraphs or not images:
        return False
    image = images[0]
    image_url = urljoin(article["url"], image.get("data-src") or image.get("src"))
    image_evidence = {
        "url": image_url,
        "alt": image.get("alt") or None,
        "selector": "wp_rest.content.rendered img",
    }
    article["field_evidence"]["publisher_images"] = [image_evidence]
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_image_only_publisher_item"
    article["exclusion_evidence"] = {
        "policy_type": "publisher_image_only",
        "publisher_image_url": image_url,
        "publisher_dom_selector": "wp_rest.content.rendered img",
        "ordinary_paragraph_count": 0,
    }
    return True


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence["legacy_sitemap_index"] = INDEX
    evidence["discovery_change"] = "WordPress date-bounded API pages replace coarse sitemap path-month selection."
    # Cache the date-bounded API rows so scrape_url does not make one request
    # per article. The same immutable rows carry title, date, body, and category
    # IDs into field validation.
    urls = wordpress_rest_discovery(
        Config.source_name,
        start,
        end,
        evidence,
        per_page=100,
        post_cache=POST_CACHE,
        embed_terms=True,
    )
    category_ids = sorted({int(item) for post in POST_CACHE.values() for item in post.get("categories", [])})
    CATEGORY_SLUGS.clear()
    for post in POST_CACHE.values():
        for group in (post.get("_embedded") or {}).get("wp:term", []):
            for item in group if isinstance(group, list) else []:
                if item.get("taxonomy") == "category" and item.get("id") and item.get("slug"):
                    CATEGORY_SLUGS[int(item["id"])] = item["slug"]
    missing = set(category_ids) - set(CATEGORY_SLUGS)
    if missing:
        raise RuntimeError(f"Embedded WordPress category metadata is incomplete: {sorted(missing)}")
    evidence["category_metadata_source"] = "wp_rest._embedded.wp:term"
    evidence["category_metadata_count"] = len(CATEGORY_SLUGS)
    return urls


def scrape_url(url: str) -> dict:
    article, categories = wordpress_rest_article(url, source=Config.source_name, post=POST_CACHE.get(url))
    cached = POST_CACHE.get(url) or {}
    slugs = {item.get("slug") for item in categories if item.get("slug")}
    slugs.update(CATEGORY_SLUGS[item] for item in cached.get("categories", []) if item in CATEGORY_SLUGS)
    classes = {f"category-{slug}" for slug in slugs if slug}
    article["category_classes"] = sorted(classes)
    article["field_evidence"]["publisher_categories"] = article["category_classes"]
    image_only = _apply_verified_image_only_exclusion(article, cached)
    if classes & UNINTERESTED_CLASSES and not image_only:
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "legacy_uninterested_category"
        article["exclusion_evidence"] = {
            "policy_type": "publisher_category",
            "source": "wp_rest._embedded.wp:term",
            "categories": article["category_classes"],
            "matched_policy_values": sorted(classes & UNINTERESTED_CLASSES),
        }
    elif classes & OPINION_CLASSES and not image_only:
        article["collection"] = "opinion-articles"
    return article
