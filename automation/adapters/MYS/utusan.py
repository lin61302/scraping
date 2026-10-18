"""Pure Utusan Malaysia adapter with live sitemap rollover discovery."""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timezone
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from adapters.MYS.common import _jsonld_article, clean, extract_article, fetch, wordpress_sitemap_window


class Config:
    source_name = "utusan.com.my"
    country_code = "MYS"
    is_env = False


INDEX = "https://www.utusan.com.my/sitemap_index.xml"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    return wordpress_sitemap_window(
        index_url=INDEX,
        domain=Config.source_name,
        start=start,
        end=end,
        evidence=evidence,
        child_pattern=r"/post-sitemap(?:\d*)\.xml$",
        newest_overlap=3,
    )


def _widget_parts(widget) -> list[str]:
    container = widget.select_one(".elementor-widget-container") or widget
    clone = BeautifulSoup(str(container), "html.parser")
    for bad in clone.select("script, style, iframe, form, .ad, .ads, .related, [class*='related']"):
        bad.decompose()
    root = clone.select_one(".elementor-widget-container") or clone
    parts: list[str] = []
    for node in root.find_all(recursive=False):
        value = clean(node.get_text(" ", strip=True))
        if value and len(value) >= 20 and value not in parts:
            parts.append(value)
    if not parts:
        for node in root.select("p, h2, h3, div"):
            value = clean(node.get_text(" ", strip=True))
            if value and len(value) >= 20 and value not in parts:
                parts.append(value)
    return parts


def _repair_from_wordpress_rest(article: dict) -> dict:
    parsed = urlparse(article["url"])
    slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    endpoint = (
        "https://www.utusan.com.my/wp-json/wp/v2/posts"
        f"?slug={quote(slug)}&_embed=1"
    )
    response = fetch(endpoint)
    payload = response.json()
    if not isinstance(payload, list) or len(payload) != 1:
        return article
    post = payload[0]
    if not (
        post.get("status") == "publish"
        and post.get("slug") == slug
        and str(post.get("link") or "").rstrip("/") == article["url"].rstrip("/")
    ):
        return article
    title = clean(html.unescape(BeautifulSoup(
        ((post.get("title") or {}).get("rendered") or ""), "html.parser"
    ).get_text(" ", strip=True)))
    body_soup = BeautifulSoup(((post.get("content") or {}).get("rendered") or ""), "html.parser")
    for bad in body_soup.select("script, style, iframe, form, .ad, .ads, .related, [class*='related']"):
        bad.decompose()
    parts = []
    for node in body_soup.select("p, h2, h3"):
        value = clean(node.get_text(" ", strip=True))
        if value and len(value) >= 20 and value not in parts:
            parts.append(value)
    body = "\n\n".join(parts) or clean(body_soup.get_text(" ", strip=True))
    raw_date = str(post.get("date_gmt") or "")
    try:
        published = datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
    except ValueError:
        return article
    if not title or not body:
        return article
    article.update({"title": title, "maintext": body, "date_publish": published})
    article.setdefault("field_sources", {}).update({
        "title": "wordpress_rest.title.rendered",
        "date_publish": "wordpress_rest.date_gmt",
        "maintext": "wordpress_rest.content.rendered",
    })
    evidence = article.setdefault("field_evidence", {})
    evidence.update({
        "titles": [{"kind": "wordpress_rest_title", "source": endpoint, "raw": title}],
        "dates": [{"kind": "published", "source": "wordpress_rest.date_gmt", "raw": raw_date + "Z"}],
        "bodies": [{"kind": "wordpress_rest_content", "source": endpoint, "text": body[:20000]}],
        "http_status": response.status_code,
        "publisher_api_records": [{
            "kind": "wordpress_rest_exact_slug", "endpoint": endpoint,
            "post_id": post.get("id"), "slug": slug, "status": post.get("status"),
            "canonical_url": post.get("link"),
        }],
    })
    return article


def _repair_article_widget(article: dict) -> dict:
    if len(re.findall(r"\b\w+\b", clean(article.get("maintext")) or "", re.UNICODE)) >= 40:
        return article
    response = fetch(article["url"])
    soup = BeautifulSoup(response.content, "html.parser")
    widget = soup.select_one(".elementor-widget-theme-post-content")
    if widget is None:
        return _repair_from_wordpress_rest(article)
    structured = _jsonld_article(soup) or {}
    word_count = int(structured.get("wordCount") or 0)
    body = "\n\n".join(_widget_parts(widget))
    words = re.findall(r"\b\w+\b", body, re.UNICODE)
    evidence = article.setdefault("field_evidence", {})
    evidence["http_status"] = response.status_code
    # Preserve the exact publisher widget and let the shared evidence validator
    # decide whether a short item is substantive enough for promotion.
    if words and word_count > 0:
        article["maintext"] = body
        article.setdefault("field_sources", {})["maintext"] = ".elementor-widget-theme-post-content raw text"
        evidence["bodies"] = [{"kind": "utusan_article_widget", "source": ".elementor-widget-theme-post-content", "text": body[:20000]}]
        return article
    images = widget.select("img[src]")
    if not body and images and 0 < word_count <= 12:
        image_url = urljoin(article["url"], images[0].get("src"))
        evidence["publisher_images"] = [{"url": image_url, "alt": clean(images[0].get("alt")), "selector": ".elementor-widget-theme-post-content img[src]"}]
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_image_only_publisher_item"
        article["exclusion_evidence"] = {
            "policy_type": "publisher_image_only", "publisher_dom_selector": ".elementor-widget-theme-post-content img[src]",
            "publisher_image_url": image_url, "structured_article_body_empty": True, "jsonld_word_count": word_count,
        }
    return article


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        language="ms",
        title_selectors=(
            ".elementor-widget-theme-post-title h1",
            "article h1",
            "main h1",
            "h1",
        ),
        body_selectors=(
            ".elementor-widget-theme-post-content",
            ".entry-content",
            "article",
        ),
        visible_date_selectors=("time.entry-date", "article time", "time"),
        evidence_body_selectors=(
            ".elementor-widget-theme-post-content",
            ".entry-content",
            "article",
        ),
    )
    return _repair_article_widget(article)
