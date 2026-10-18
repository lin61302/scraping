"""Observation-only adapter for Jang's Urdu numeric article frontier."""
from __future__ import annotations

import re
from datetime import date
import requests
from bs4 import BeautifulSoup

from adapters.PAK.common import HEADERS, _body, clean, extract_article, fetch, parse_datetime


class Config:
    source_name = "jang.com.pk"
    country_code = "PAK"
    is_env = False


LATEST = "https://jang.com.pk/category/latest-news"
LAST_LEGACY_EXCLUSIVE_ID = 1_588_112


def _jang_body(soup: BeautifulSoup) -> tuple[str, str]:
    for selector in (".detail_view_content", ".description-area"):
        node = soup.select_one(selector)
        if not node:
            continue
        direct = [clean(item.get_text(" ", strip=True)) for item in node.find_all("p", recursive=False)]
        paragraphs = [item for item in direct if item]
        if paragraphs:
            return "\n\n".join(paragraphs), selector
    return _body(soup, (".detail_view_content", ".description-area"))


def _empty_numeric_slot_article(url: str, observations: int) -> dict:
    slot = {
        "publisher_url": url, "http_status": 200, "response_bytes": 0,
        "empty_observations": observations,
    }
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": None, "date_publish": None, "maintext": None, "language": "ur",
        "parser": "pure_adapter", "collection": "SKIP", "parser_error": None,
        "field_sources": {"title": None, "date_publish": None, "maintext": None},
        "field_evidence": {
            "titles": [], "dates": [], "bodies": [], "http_status": 200,
            "publisher_empty_numeric_slots": [slot],
        },
        "exclusion_reason": "publisher_unallocated_numeric_slot",
        "exclusion_evidence": {"policy_type": "publisher_unallocated_numeric_slot", **slot},
    }


def _repair_alternate_template(article: dict, soup: BeautifulSoup) -> None:
    evidence = article.setdefault("field_evidence", {})
    if not article.get("date_publish"):
        date_raw = date_source = None
        for node in soup.select(".detail-w-t > .detail-time"):
            raw = clean(node.get_text(" ", strip=True))
            if raw and parse_datetime(raw):
                date_raw, date_source = raw, ".detail-w-t > .detail-time"
                break
        if not date_raw:
            raw_html = str(soup)
            date_match = re.search(r"['\"]publish_date['\"]\s*:\s*['\"]([^'\"]+)", raw_html, re.I)
            time_match = re.search(r"['\"]publish_time['\"]\s*:\s*['\"]([^'\"]+)", raw_html, re.I)
            if date_match:
                date_raw = date_match.group(1)
                if time_match:
                    date_raw = f"{date_raw} {time_match.group(1)}"
                date_source = "publisher analytics publish_date/publish_time"
        published = parse_datetime(date_raw)
        if published:
            article["date_publish"] = published
            article.setdefault("field_sources", {})["date_publish"] = date_source
            evidence["dates"] = [{"kind": "published", "source": date_source, "raw": date_raw}]

    body, body_source = _jang_body(soup)
    old_body = clean(article.get("maintext")) or ""
    if body and (not old_body or len(body) > len(old_body)):
        article["maintext"] = body
        article.setdefault("field_sources", {})["maintext"] = body_source
    if article.get("maintext"):
        evidence["bodies"] = [{"kind": body_source or "selected_body", "text": article["maintext"][:20000]}]


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    soup = BeautifulSoup(fetch(LATEST).content, "html.parser")
    dated = []
    for item in soup.select("ul.scrollPaginationNew__ li"):
        link = item.select_one('a[href*="/news/"]')
        stamp = item.select_one(".cat-time")
        match = re.search(r"/news/(\d+)", link.get("href", "") if link else "")
        parsed = parse_datetime(stamp.get_text(" ", strip=True)) if stamp else None
        if match:
            dated.append((int(match.group(1)), parsed.date() if parsed else None))
    all_ids = [value for value, _ in dated]
    if not all_ids:
        raise RuntimeError("Jang latest listing exposed no numeric article IDs")
    ids_through_end = [value for value, published in dated if published and published <= end]
    upper = max(ids_through_end or all_ids)
    lower = LAST_LEGACY_EXCLUSIVE_ID
    if upper < lower:
        raise RuntimeError(f"Jang frontier regressed: {upper} < {lower}")
    evidence.update({
        "strategy": "numeric_article_frontier",
        "frontier_listing": LATEST,
        "legacy_range_end_exclusive": LAST_LEGACY_EXCLUSIVE_ID,
        "frontier_max_id": max(all_ids),
        "upper_id_through_requested_end": upper,
        "listing_dated_ids": len(dated),
        "generated_first_id": lower,
        "generated_last_id": upper,
        "coverage_complete": start <= date(2026, 6, 3),
        "coverage_note": "The prior production range ended at this numeric frontier; every subsequent numeric ID is generated through the latest listing ID for the requested end date.",
    })
    return [f"https://jang.com.pk/news/{value}" for value in range(lower, upper + 1)]


def scrape_url(url: str) -> dict:
    try:
        article = extract_article(
            url,
            source=Config.source_name,
            title_selectors=(".detail-right h1", "article h1", "h1"),
            body_selectors=(".detail_view_content", ".description-area", ".detail-right", "article"),
            visible_date_text_selectors=(".detail-w-t > .detail-time:not(:has(a))",),
            evidence_body_selectors=(".detail_view_content", ".description-area"),
            language="ur",
        )
    except RuntimeError as exc:
        empty_observations = str(exc).count("ValueError: empty response")
        if not empty_observations:
            raise
        while empty_observations < 4:
            response = requests.get(url, headers=HEADERS, timeout=35)
            response.raise_for_status()
            if response.content:
                return scrape_url(url)
            empty_observations += 1
        return _empty_numeric_slot_article(url, empty_observations)
    if not article.get("date_publish") or len(clean(article.get("maintext")) or "") < 150:
        soup = BeautifulSoup(fetch(url).content, "html.parser")
        _repair_alternate_template(article, soup)
    return article
