"""Observation-only adapter for bulawayo24.com. No database writes."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from adapters.ZWE.common import clean, extract_article, normalize_url, sitemap_rows, xml_document


class Config:
    source_name = "bulawayo24.com"
    country_code = "ZWE"
    is_env = False


INDEX = "https://bulawayo24.com/sitemap/"
UNINTERESTED = {"sports", "sport", "opinion", "video", "entertainment"}
DISCOVERY_DATES: dict[str, datetime] = {}


def _article_sitemaps() -> list[tuple[int, str]]:
    rows = sitemap_rows(xml_document(INDEX))
    result = []
    for url, _ in rows:
        query = parse_qs(urlparse(url).query)
        if query.get("t") == ["articles"] and query.get("page", [""])[0].isdigit():
            result.append((int(query["page"][0]), url.replace("&amp;", "&")))
    return sorted(result)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    upper = end + timedelta(days=1)
    sitemaps = _article_sitemaps()
    evidence.update({
        "sitemap_index": INDEX,
        "article_sitemaps_total": len(sitemaps),
        "candidate_lastmod_window": {"start": start.isoformat(), "end": upper.isoformat()},
        "sitemaps_checked": [],
        "sitemap_failures": [],
        "boundary_reached": False,
    })
    result = []
    seen = set()
    DISCOVERY_DATES.clear()
    for page, sitemap_url in sitemaps:
        try:
            rows = sitemap_rows(xml_document(sitemap_url))
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
            continue
        dated = [modified for _, modified in rows if modified]
        evidence["sitemaps_checked"].append({
            "page": page,
            "url": sitemap_url,
            "entries": len(rows),
            "newest": max(dated).isoformat() if dated else None,
            "oldest": min(dated).isoformat() if dated else None,
        })
        for raw, modified in rows:
            if modified is None or not (start <= modified.date() <= upper):
                continue
            url = normalize_url(raw, Config.source_name)
            if url and url not in seen:
                seen.add(url)
                DISCOVERY_DATES[url] = modified
                result.append(url)
        if dated and max(item.date() for item in dated) < start:
            evidence["boundary_reached"] = True
            evidence["boundary_sitemap"] = sitemap_url
            break
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} selected sitemap pages failed")
    if not evidence["boundary_reached"]:
        raise RuntimeError("Bulawayo24 sitemap walk did not reach the requested start boundary")
    evidence["discovered_urls_after_filter"] = len(result)
    evidence["publication_fallback"] = (
        "Exact sitemap lastmod is used only while the article page displays a relative age; "
        "older pages expose the corresponding absolute publication timestamp."
    )
    return result


def scrape_url(url: str) -> dict:
    # A small set of sitemap routes contains percent-encoded spaces and soft
    # 404s when requested literally. The publisher serves the same article ID
    # on its hyphenated route.
    retrieval_url = url.replace("%20", "-")
    article, soup = extract_article(
        retrieval_url,
        source=Config.source_name,
        title_selectors=("h1",),
        body_selectors=("#story",),
        evidence_body_selectors=("#story",),
        visible_date_selectors=("#pub_date",),
        fallback_date=DISCOVERY_DATES.get(url),
    )
    article["url"] = url
    if retrieval_url != url:
        article["field_evidence"]["retrieval_url"] = retrieval_url
    category_node = soup.select_one("#category")
    category = category_node.get_text(" ", strip=True).casefold() if category_node else ""
    article["page_categories"] = [part.strip() for part in category.split("/") if part.strip()]
    if any(value in category for value in UNINTERESTED):
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "legacy_uninterested_category"
        article["exclusion_evidence"] = {
            "source": "#category",
            "categories": article["page_categories"],
            "matched_policy_values": sorted(value for value in UNINTERESTED if value in category),
        }
    else:
        story = soup.select_one("#story")
        embeds = story.select("blockquote.twitter-tweet") if story else []
        ordinary_paragraphs = [
            node for node in (story.select("p") if story else [])
            if node.find_parent("blockquote") is None and clean(node.get_text(" ", strip=True))
        ]
        status_links = [
            anchor.get("href") for anchor in (embeds[0].select("a[href]") if len(embeds) == 1 else [])
            if re.search(r"https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/\d+", anchor.get("href") or "")
        ]
        embedded_text = clean(embeds[0].get_text(" ", strip=True)) if len(embeds) == 1 else None
        if (
            len(embeds) == 1 and len(status_links) == 1 and not ordinary_paragraphs
            and embedded_text and article.get("title") and article.get("date_publish")
        ):
            social_evidence = {
                "provider": "twitter",
                "selector": "#story blockquote.twitter-tweet",
                "status_url": status_links[0],
                "embedded_text": embedded_text,
            }
            article["field_evidence"]["publisher_social_embeds"] = [social_evidence]
            article["collection"] = "SKIP"
            article["exclusion_reason"] = "excluded_social_embed_only_publisher_item"
            article["exclusion_evidence"] = {
                "policy_type": "publisher_social_embed_only",
                **social_evidence,
                "ordinary_paragraph_count": 0,
            }
    return article
