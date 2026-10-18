"""Observation-only Brasil 247 adapter. No database imports or writes."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.BRA.common import extract_article, fetch, jsonld_objects, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "brasil247.com"
    country_code = "BRA"
    is_env = False


INDEX = "https://www.brasil247.com/sitemap-posts.xml"
DENY = (
    "/cultura/", "/esporte/", "/blog/", "/midia/", "/entrevistas", "/estude-em-macau/",
    "/colunistas/", "/ideias/", "/247-na-copa/", "/empreender/", "/historia/", "/compliance/",
    # Publisher cartoon pages contain an image but no article text. They cannot
    # satisfy the project's required maintext field and are a distinct content
    # type rather than failed news-article parses.
    "/charges/", "/conteudo-publicitario/",
)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    child_lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    children = sitemap_entries(xml_document(INDEX))
    selected = [(url, modified) for url, modified in children if modified is None or modified >= child_lower]
    evidence.update({
        "sitemap_index": INDEX,
        "child_selection_window": {"start": child_lower.isoformat(), "end": upper.isoformat()},
        "candidate_lastmod_window": {"start": start.isoformat(), "end": upper.isoformat()},
        "child_sitemaps_total": len(children),
        "child_sitemaps_selected": len(selected),
        "sitemaps_checked": [],
        "sitemap_failures": [],
    })
    entries = []
    for sitemap_url, modified in selected:
        try:
            items = sitemap_entries(xml_document(sitemap_url))
            evidence["sitemaps_checked"].append({
                "url": sitemap_url, "index_lastmod": modified.isoformat() if modified else None, "entries": len(items)
            })
            entries.extend(items)
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} selected child sitemaps failed")
    evidence["entries_before_window_filter"] = len(entries)
    candidates = [(url, modified) for url, modified in entries if modified is None or start <= modified <= upper]
    # Stable chronological order lets the observer's --reverse-urls option
    # sample the newest candidates consistently across all BRA adapters.
    candidates.sort(key=lambda item: item[1] or date.min)
    urls = unique_urls(candidates, Config.source_name, DENY)
    evidence["entries_without_lastmod"] = sum(modified is None for _, modified in entries)
    evidence["candidate_entries_in_window"] = len(candidates)
    evidence["discovered_urls_after_filter"] = len(urls)
    return urls


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        body_selectors=(".entry-content.wp-block-post-content", ".wp-block-post-content", "article"),
        visible_date_selectors=(),
        prefer_visible_date=False,
        evidence_body_selectors=("main .entry-content", "main"),
    )
    body = article.get("maintext") or ""
    if "/xinhua/" in url and "Fonte: Agência Xinhua" in body and len(body) < 500:
        # These publisher pages explicitly say "Confira as imagens" and only
        # expose a source link after the image gallery. Keep the URL accounted
        # for as a verified non-text item instead of promoting the link as body.
        selector = ".entry-content.wp-block-post-content"
        soup = BeautifulSoup(fetch(url).content, "html.parser")
        container = soup.select_one(selector)
        images = []
        if container:
            for node in container.select("img"):
                image_url = node.get("src") or node.get("data-src")
                if image_url:
                    image_url = urljoin(url, image_url)
                    if image_url not in images:
                        images.append(image_url)
        structured_body_empty = not any(
            item.get("articleBody") for item in jsonld_objects(soup)
            if item.get("@type") in {"Article", "NewsArticle", "ReportageNewsArticle"}
        )
        if images and structured_body_empty:
            signal = {
                "selector": selector,
                "item_count": len(images),
                "image_url": images[0],
                "structured_article_body_empty": True,
            }
            article["field_evidence"]["publisher_images"] = [
                {"url": image_url, "alt": None} for image_url in images
            ]
            article["field_evidence"]["publisher_galleries"] = [signal]
            article["collection"] = "SKIP"
            article["exclusion_reason"] = "excluded_image_only_publisher_item"
            article["exclusion_evidence"] = {
                "policy_type": "publisher_image_only",
                "publisher_dom_selector": selector,
                "publisher_image_url": images[0],
            }
            if len(images) >= 2:
                article["exclusion_evidence"].update({
                    "publisher_item_type": "photo_gallery",
                    "publisher_gallery_selector": selector,
                    "gallery_item_count": len(images),
                    "structured_article_body_empty": True,
                })
    return article
