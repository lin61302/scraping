"""Pure New Straits Times adapter using the live monthly news sitemap index."""
from __future__ import annotations

import html
import json
import re
from datetime import date

from bs4 import BeautifulSoup

from adapters.MYS.common import clean, extract_article, fetch, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "nst.com.my"
    country_code = "MYS"
    is_env = False


INDEX = "https://www.nst.com.my/sitemap.xml"
ROUTES = (
    "/news/crime-courts/", "/news/nation/", "/news/government-public-policy/",
    "/news/politics/", "/world/world/", "/world/region/",
)


def _month(url: str) -> tuple[int, int] | None:
    match = re.search(r"sitemap-news-(20\d{2})-(\d{2})\.xml$", url)
    return (int(match.group(1)), int(match.group(2))) if match else None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index_entries = [item for item in sitemap_entries(xml_document(INDEX)) if _month(item[0])]
    if not index_entries:
        raise RuntimeError("NST monthly sitemap index contains no news sitemaps")
    requested_months = {
        (year, month)
        for year in range(start.year, end.year + 1)
        for month in range(1, 13)
        if (year, month) >= (start.year, start.month) and (year, month) <= (end.year, end.month)
    }
    selected = [item for item in index_entries if _month(item[0]) in requested_months]
    advertised_months = sorted(_month(url) for url, _ in index_entries)
    evidence.update({
        "strategy": "live_monthly_news_sitemap_index",
        "sitemap_index": INDEX,
        "advertised_month_range": [list(advertised_months[0]), list(advertised_months[-1])],
        "requested_month_count": len(requested_months),
        "selected_month_count": len(selected),
        "interested_routes": list(ROUTES),
        "sitemaps_checked": [],
        "sitemap_failures": [],
        "coverage_complete": advertised_months[0] <= (start.year, start.month),
    })
    entries = []
    for sitemap_url, _ in selected:
        try:
            items = sitemap_entries(xml_document(sitemap_url))
            evidence["sitemaps_checked"].append({"url": sitemap_url, "entries": len(items)})
            entries.extend(item for item in items if any(route in item[0].casefold() for route in ROUTES))
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} NST monthly sitemaps failed")
    urls = unique_urls(entries, Config.source_name)
    evidence.update({
        "candidate_entries_after_route_policy": len(entries),
        "discovered_urls_after_filter": len(urls),
    })
    return urls


def _repair_publisher_payload(article: dict) -> dict:
    if not clean(article.get("title")):
        for signal in (article.get("field_evidence") or {}).get("titles") or []:
            raw = html.unescape(clean(signal.get("raw"))) if isinstance(signal, dict) else None
            title = re.sub(r"\s*\|\s*New Straits Times\s*$", "", raw or "", flags=re.I).strip()
            if len(title) >= 8:
                article["title"] = title
                article.setdefault("field_sources", {})["title"] = f"recovered:{signal.get('kind', 'publisher_title')}"
                article["field_evidence"].setdefault("titles", []).append({
                    "kind": "recovered_publisher_title", "source": signal.get("source") or signal.get("kind"), "raw": title,
                })
                break
    if clean(article.get("maintext")) != "© New Straits Times Press (M) Bhd":
        return article
    response = fetch(article["url"])
    soup = BeautifulSoup(response.content, "html.parser")
    node = soup.find("article-component")
    try:
        payload = json.loads(node.get(":article")) if node and node.get(":article") else None
    except (TypeError, ValueError):
        payload = None
    match = re.search(r"/20\d{2}/\d{2}/(\d+)/", article["url"])
    nid = int(match.group(1)) if match else None
    body_soup = BeautifulSoup(str((payload or {}).get("body") or ""), "html.parser")
    inline_soup = BeautifulSoup(str((payload or {}).get("body_with_inline") or ""), "html.parser")
    body = clean(body_soup.get_text(" ", strip=True))
    inline = clean(inline_soup.get_text(" ", strip=True))
    if not (
        payload and payload.get("nid") == nid and payload.get("status") == 1
        and html.unescape(clean(payload.get("title")) or "") == html.unescape(clean(article.get("title")) or "")
    ):
        return article
    word_count = int(payload.get("word_count") or 0)
    evidence = article.setdefault("field_evidence", {})
    evidence["http_status"] = response.status_code
    if word_count == 0 and body == "© New Straits Times Press (M) Bhd" and not inline:
        record = {
            "publisher_url": article["url"], "embedded_nid": nid,
            "embedded_status": 1, "embedded_word_count": 0,
            "embedded_body_text": body, "embedded_inline_body_empty": True,
        }
        evidence["publisher_empty_articles"] = [record]
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_publisher_empty_article"
        article["exclusion_evidence"] = {"policy_type": "publisher_empty_article", **record}
        return article

    images = [node.get("src") for node in inline_soup.select("img[src]") if node.get("src")]
    body_without_ids = clean(re.sub(r"\[\[gn4id:\d+\]\]", "", body or ""))
    if word_count <= 5 and images and not inline and body_without_ids == "© New Straits Times Press (M) Bhd":
        evidence["publisher_images"] = [
            {"url": url, "selector": "article-component[:article].body_with_inline img[src]"}
            for url in images
        ]
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_image_only_publisher_item"
        article["exclusion_evidence"] = {
            "policy_type": "publisher_image_only",
            "publisher_dom_selector": "article-component[:article].body_with_inline img[src]",
            "publisher_image_url": images[0], "structured_article_body_empty": True,
            "embedded_nid": nid, "embedded_word_count": word_count,
        }
        return article

    candidates = [
        clean(re.sub(r"\[\[gn4id:\d+\]\]", "", value or ""))
        for value in (body, inline)
    ]
    candidate = max(candidates, key=lambda value: len(re.findall(r"\b\w+\b", value or "")), default=None)
    recovered_words = len(re.findall(r"\b\w+\b", candidate or "", re.UNICODE))
    if candidate and recovered_words >= 10 and word_count >= 10:
        article["maintext"] = candidate
        article.setdefault("field_sources", {})["maintext"] = "article-component[:article] current publisher payload"
        evidence["bodies"] = [{
            "kind": "article_component_current_payload", "source": "article-component[:article]",
            "text": candidate[:20000],
        }]
        evidence["publisher_payload_repairs"] = [{
            "embedded_nid": nid, "embedded_status": 1,
            "embedded_word_count": word_count, "recovered_words": recovered_words,
        }]
    return article


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        language="en",
        title_selectors=("h1.article-title", "article h1", "main h1", "h1"),
        body_selectors=(
            "[itemprop='articleBody']",
            ".article-content",
            ".field-name-body",
            "article",
        ),
        visible_date_selectors=("[itemprop='datePublished']", "article time", "time"),
        evidence_body_selectors=("[itemprop='articleBody']", ".article-content", "article", "main"),
        embedded_body=("article-component", ":article", "body"),
    )
    return _repair_publisher_payload(article)
