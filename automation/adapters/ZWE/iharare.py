"""Observation-only adapter for iharare.com. No database writes."""
from __future__ import annotations

from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters.ZWE.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "iharare.com"
    country_code = "ZWE"
    is_env = False


BASE = "https://iharare.com/category/local-zimbabwe-news/"
UNINTERESTED = {"bizarre", "sport", "sports", "entertainment"}


def _listing(page: int) -> tuple[str, list[tuple[str, date]]]:
    url = BASE if page == 1 else f"{BASE}page/{page}/"
    soup = BeautifulSoup(fetch(url).content, "html.parser")
    rows = []
    for card in soup.select("div.jeg_posts_wrap article.jeg_pl_md_5"):
        anchor = card.select_one("h3.jeg_post_title a[href]")
        date_node = card.select_one(".jeg_meta_date, time")
        parsed = parse_datetime(date_node.get("datetime") or date_node.get_text(" ", strip=True)) if date_node else None
        normalized = normalize_url(anchor.get("href"), Config.source_name) if anchor else None
        if normalized and parsed:
            rows.append((normalized, parsed.date()))
    return url, rows


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    upper = end + timedelta(days=1)
    evidence.update({
        "listing_root": BASE,
        "listing_date_window": {"start": start.isoformat(), "end": upper.isoformat()},
        "pages_checked": [],
        "boundary_reached": False,
    })
    result = []
    seen = set()
    for page in range(1, 501):
        page_url, rows = _listing(page)
        dates = [published for _, published in rows]
        evidence["pages_checked"].append({
            "page": page,
            "url": page_url,
            "rows": len(rows),
            "newest": max(dates).isoformat() if dates else None,
            "oldest": min(dates).isoformat() if dates else None,
        })
        if not rows:
            evidence["boundary_reached"] = True
            evidence["boundary_reason"] = "empty_listing"
            break
        for raw, published in rows:
            if start <= published <= upper and raw not in seen:
                seen.add(raw)
                result.append(raw)
        if max(dates) < start:
            evidence["boundary_reached"] = True
            evidence["boundary_reason"] = "full_page_older_than_start"
            break
    if not evidence["boundary_reached"]:
        raise RuntimeError("iHarare listing walk did not reach the requested start boundary")
    evidence["discovered_urls_after_filter"] = len(result)
    return result


def scrape_url(url: str) -> dict:
    article, soup = extract_article(
        url,
        source=Config.source_name,
        title_selectors=("h1.jeg_post_title", "article h1", "h1"),
        body_selectors=(".content-inner", ".entry-content"),
        evidence_body_selectors=(".entry-content", ".content-inner"),
        visible_date_selectors=(".jeg_meta_date", "time[datetime]"),
    )
    categories = [
        node.get_text(" ", strip=True).casefold()
        for node in soup.select("div.jeg_meta_category a[rel='category tag']")
    ]
    article["page_categories"] = categories
    if UNINTERESTED.intersection(categories):
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "legacy_uninterested_category"
        article["exclusion_evidence"] = {
            "source": "div.jeg_meta_category a[rel='category tag']",
            "categories": categories,
            "matched_policy_values": sorted(UNINTERESTED.intersection(categories)),
        }
    return article
