"""Observation-only adapter for ambebi.ge category archives."""
from __future__ import annotations

from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.GEO.common import (
    article_body,
    clean,
    fetch,
    first_text,
    normalize_url,
    page_evidence,
    parse_datetime,
    parser_error,
)


class Config:
    source_name = "ambebi.ge"
    country_code = "GEO"
    is_env = False


BASE = "https://www.ambebi.ge"
CATEGORY = BASE + "/category/{slug}/archive/?page={page}"
CATEGORIES = (
    "politika",
    "sazogadoeba",
    "samartali",
    "msoplio",
    "samkhedro",
    "semtxveva",
    "ekonomika",
    "conflicts",
)
MAX_PAGES_PER_CATEGORY = 100
def _listing_cards(soup: BeautifulSoup) -> list[tuple[str, date]]:
    rows = []
    for card in soup.select("div.maintopnewsitem"):
        link = card.select_one("a[itemprop='url'][href]")
        time_node = card.select_one("time[itemprop='datePublished']")
        if not link or not time_node:
            continue
        raw_date = time_node.get("datetime") or time_node.get_text(" ", strip=True)
        published = parse_datetime(raw_date, day_first=True)
        url = normalize_url(urljoin(BASE, link.get("href")), Config.source_name)
        if url and published:
            rows.append((url, published.date()))
    return rows


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update(
        {
            "method": "dated_category_archives",
            "category_template": CATEGORY,
            "categories": list(CATEGORIES),
            "pages_checked": [],
            "coverage_boundaries": {},
            "listing_failures": [],
        }
    )
    by_category: dict[str, list[str]] = {}
    for slug in CATEGORIES:
        category_urls = []
        boundary = False
        for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
            listing_url = CATEGORY.format(slug=slug, page=page)
            response = fetch(listing_url)
            soup = BeautifulSoup(response.content, "html.parser")
            cards = _listing_cards(soup)
            if not cards:
                evidence["pages_checked"].append(
                    {"category": slug, "page": page, "url": listing_url, "articles": 0, "stop": "empty"}
                )
                evidence["coverage_boundaries"][slug] = {"page": page, "reason": "empty"}
                boundary = True
                break
            dates = [published for _, published in cards]
            evidence["pages_checked"].append(
                {
                    "category": slug,
                    "page": page,
                    "url": listing_url,
                    "articles": len(cards),
                    "newest": max(dates).isoformat(),
                    "oldest": min(dates).isoformat(),
                }
            )
            category_urls.extend(url for url, published in cards if start <= published <= end)
            if max(dates) < start:
                evidence["coverage_boundaries"][slug] = {
                    "page": page,
                    "reason": "full_page_before_start",
                    "newest": max(dates).isoformat(),
                }
                boundary = True
                break
        if not boundary:
            raise RuntimeError(f"Ambebi category {slug!r} reached the {MAX_PAGES_PER_CATEGORY}-page cap without a date boundary")
        by_category[slug] = list(dict.fromkeys(category_urls))
    # Interleave categories so a bounded canary samples more than the first,
    # highest-volume section. Full observations still receive every URL.
    discovered = []
    for index in range(max((len(urls) for urls in by_category.values()), default=0)):
        for slug in CATEGORIES:
            urls = by_category[slug]
            if index < len(urls):
                discovered.append(urls[index])
    result = list(dict.fromkeys(discovered))
    evidence["urls_by_category"] = {slug: len(urls) for slug, urls in by_category.items()}
    evidence["coverage_complete"] = (
        len(evidence["coverage_boundaries"]) == len(CATEGORIES)
        and not evidence["listing_failures"]
    )
    evidence["discovered_urls_before_dedup"] = len(discovered)
    evidence["discovered_urls_after_dedup"] = len(result)
    return result


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_selector = first_text(soup, ("h1", "div.article_title", "[itemprop='headline']"))
    body, body_selector = article_body(soup, ("div.article_content", "[itemprop='articleBody']"))

    date_node = soup.select_one("div.maintopnewsdate time[datetime]") or soup.select_one("time[datetime]")
    date_raw = clean(date_node.get("datetime")) if date_node else None
    date_source = "div.maintopnewsdate time[datetime]" if date_node else None
    if not date_raw:
        date_node = soup.select_one("meta[itemprop='datePublished'][content]")
        date_raw = clean(date_node.get("content")) if date_node else None
        date_source = "meta[itemprop='datePublished'][content]" if date_node else None
    published = parse_datetime(date_raw, day_first=True)

    return {
        "url": url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "maintext": body,
        "date_publish": published,
        "parser": "ambebi_visible_h1_time_article_content",
        "parser_error": parser_error(title, body, published),
        "field_evidence": page_evidence(
            soup,
            title=title,
            title_selector=title_selector,
            date_raw=date_raw,
            date_source=date_source,
            body=body,
            body_selector=body_selector,
            status=response.status_code,
        ),
    }
