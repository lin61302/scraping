"""Pure network observation adapter for Georgia Recorder's environment section."""
from __future__ import annotations

import re
import time
from datetime import date

import cloudscraper
from bs4 import BeautifulSoup

from adapters.GEO.common import (
    CHALLENGE, article_body, clean, first_text, normalize_url, page_evidence,
    parse_datetime, parser_error, published_signal,
)


class Config:
    source_name = "georgiarecorder.com"
    country_code = "GEO"
    is_env = True


LISTING = "https://georgiarecorder.com/category/energy-environment/page/{page}/"
PATH_DATE = re.compile(r"/((?:19|20)\d{2})/(\d{2})/(\d{2})/")
MAX_PAGES = 200
SCRAPER = cloudscraper.create_scraper(browser={"browser": "firefox", "platform": "windows", "mobile": False})


def fetch(url: str, timeout: int = 30):
    errors = []
    for attempt in range(3):
        try:
            response = SCRAPER.get(url, timeout=timeout)
            response.raise_for_status()
            if not response.content:
                raise ValueError("empty response")
            if CHALLENGE.search(clean(response.text[:10000]) or ""):
                raise ValueError("challenge response")
            return response
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"fetch failed for {url}: {'; '.join(errors)}")


def _dated_links(soup: BeautifulSoup) -> list[tuple[str, date]]:
    rows = []
    for card in soup.select("div.archiveCard"):
        article_url = None
        for link in card.select("a[href]"):
            candidate = normalize_url(link.get("href"), Config.source_name, ("/author/", "/category/"))
            if candidate and PATH_DATE.search(candidate):
                article_url = candidate
                break
        match = PATH_DATE.search(article_url or "")
        if not article_url or not match:
            continue
        try:
            published = date(*map(int, match.groups()))
        except ValueError:
            continue
        rows.append((article_url, published))
    return list(dict.fromkeys(rows))


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"method": "cloudflare_dated_environment_category", "listing_template": LISTING,
                     "pages_checked": [], "listing_failures": []})
    candidates = []
    boundary = False
    for page in range(1, MAX_PAGES + 1):
        url = LISTING.format(page=page)
        response = fetch(url)
        rows = _dated_links(BeautifulSoup(response.content, "html.parser"))
        if not rows:
            evidence["pages_checked"].append({"page": page, "url": url, "articles": 0, "stop": "empty"})
            boundary = True
            break
        dates = [published for _, published in rows]
        evidence["pages_checked"].append({"page": page, "url": url, "articles": len(rows),
                                          "newest": max(dates).isoformat(), "oldest": min(dates).isoformat()})
        candidates.extend((published, article_url) for article_url, published in rows if start <= published <= end)
        if max(dates) < start:
            evidence["coverage_boundary"] = {"page": page, "reason": "full_page_before_start",
                                             "newest": max(dates).isoformat()}
            boundary = True
            break
    evidence["coverage_complete"] = boundary
    if not boundary:
        raise RuntimeError("Georgia Recorder category walk reached its page cap without a lower boundary")
    candidates.sort(reverse=True)
    result = list(dict.fromkeys(url for _, url in candidates))
    evidence["discovered_urls_before_dedup"] = len(candidates)
    evidence["discovered_urls_after_dedup"] = len(result)
    return result


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_selector = first_text(soup, ("h1#singleHed", "article h1", "h1"))
    body, body_selector = article_body(soup, ("div#dataContent", "article"))
    date_raw, date_source = published_signal(soup)
    published = parse_datetime(date_raw)
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "maintext": body, "date_publish": published,
        "parser": "georgiarecorder_environment_h1_published_metadata_data_content",
        "parser_error": parser_error(title, body, published),
        "field_evidence": page_evidence(soup, title=title, title_selector=title_selector,
                                         date_raw=date_raw, date_source=date_source, body=body,
                                         body_selector=body_selector, status=response.status_code),
    }
