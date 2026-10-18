"""Observation-only adapter for NewsDay's migrated WordPress site."""
from __future__ import annotations

from datetime import date
from html import unescape
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from adapters.ZWE.common import (
    article_body,
    clean,
    fetch,
    parse_datetime,
    wordpress_rest_discovery,
)


class Config:
    source_name = "newsday.co.zw"
    country_code = "ZWE"
    is_env = False


# The legacy Selenium collector covered these three sections. They are now
# first-party WordPress category IDs on the migrated site.
CATEGORY_IDS = [1530, 1464, 1408]
WORDPRESS_COM_SITE_ID = 254824843
POST_CACHE: dict[str, dict] = {}


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence["legacy_sections"] = [
        "local-news", "international", "agriculture",
    ]
    evidence["site_migration"] = (
        "The current public site is WordPress; first-party API pagination "
        "replaces the legacy Selenium load-more collector."
    )
    return wordpress_rest_discovery(
        Config.source_name, start, end, evidence,
        categories=CATEGORY_IDS,
        post_cache=POST_CACHE,
    )


def scrape_url(url: str) -> dict:
    cached = POST_CACHE.get(url)
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    if cached is None:
        lookup_url = (
            "https://news-days.com/wp-json/wp/v2/posts?"
            f"slug={quote(slug)}&_fields=id,date,slug"
        )
        lookup_response = fetch(lookup_url)
        rows = lookup_response.json()
        if not isinstance(rows, list) or len(rows) != 1:
            raise RuntimeError(f"NewsDay post lookup returned no unique result for {url}")
        cached = dict(rows[0])
        cached["_discovery_url"] = lookup_response.url
    if not cached.get("id"):
        raise RuntimeError(f"missing publisher post id for NewsDay article: {url}")
    if cached.get("slug") != slug:
        raise RuntimeError(f"discovery slug mismatch for NewsDay article: {url}")

    # The publisher's wp/v2 response flags these posts as truncated.  Its
    # public Jetpack/WordPress.com representation contains the complete,
    # publisher-supplied body.  Use the numeric site id because the hostname
    # form can be rate-disabled independently on the collection host.
    endpoint = (
        "https://public-api.wordpress.com/rest/v1.1/sites/"
        f"{WORDPRESS_COM_SITE_ID}/posts/{cached['id']}"
    )
    response = fetch(endpoint)
    payload = response.json()
    if payload.get("ID") != cached.get("id") or payload.get("site_ID") != WORDPRESS_COM_SITE_ID:
        raise RuntimeError(f"WordPress.com post identity mismatch for {url}")
    if payload.get("slug") != slug:
        raise RuntimeError(f"WordPress.com slug mismatch for {url}: {payload.get('slug')}")

    title_html = payload.get("title") or ""
    title = clean(BeautifulSoup(unescape(title_html), "html.parser").get_text(" ", strip=True))
    published_raw = clean(payload.get("date"))
    published = parse_datetime(published_raw)
    discovery_published = parse_datetime(cached.get("date"))
    if published is None or discovery_published is None or published != discovery_published:
        raise RuntimeError(f"WordPress.com publication date mismatch for {url}")

    content_html = payload.get("content") or ""
    content_soup = BeautifulSoup(f"<article>{content_html}</article>", "html.parser")
    body, body_selector = article_body(content_soup, ("article",))
    if not body or len(body) < 200:
        raise RuntimeError(f"WordPress.com full body missing for {url}")

    return {
        "url": url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": "en",
        "parser": "pure_adapter_wordpress_com_full_post",
        "collection": None,
        "parser_error": None,
        "content_protected": False,
        "content_truncated": False,
        "page_categories": sorted((payload.get("categories") or {}).keys()),
        "field_sources": {
            "title": "wordpress_com.title",
            "date_publish": "wordpress_com.date",
            "maintext": "wordpress_com.content",
        },
        "field_evidence": {
            "titles": [{
                "kind": "publisher_api_title",
                "source": "wordpress_com.title",
                "raw": title,
            }] if title else [],
            "dates": [{
                "kind": "published",
                "source": "wordpress_com.date",
                "raw": published_raw,
            }] if published_raw else [],
            "bodies": [{
                "kind": "publisher_wordpress_com_content",
                "source": body_selector,
                "text": body[:20000],
            }],
            "http_status": response.status_code,
            "retrieval_url": response.url,
            "post_id": payload.get("ID"),
            "site_id": payload.get("site_ID"),
            "discovery_url": cached.get("_discovery_url"),
        },
    }
