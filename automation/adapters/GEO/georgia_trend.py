"""Pure network observation adapter for Georgia Trend's environment section."""
from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from adapters.GEO.common import (
    article_body, fetch, first_text, normalize_url, page_evidence,
    parse_datetime, parser_error, published_signal,
)


class Config:
    source_name = "georgiatrend.com"
    country_code = "GEO"
    is_env = True


LISTING = "https://www.georgiatrend.com/category/environment/page/{page}/"
PATH_DATE = re.compile(r"/((?:19|20)\d{2})/(\d{2})/(\d{2})/")
MAX_PAGES = 100


def _dated_links(soup: BeautifulSoup) -> list[tuple[str, date]]:
    rows = []
    # The page also renders unrelated recommendation cards after the archive.
    for link in soup.select("article.category-environment h3.entry-title a[href]"):
        url = normalize_url(link.get("href"), Config.source_name)
        match = PATH_DATE.search(url or "")
        if not url or not match:
            continue
        try:
            published = date(*map(int, match.groups()))
        except ValueError:
            continue
        rows.append((url, published))
    return list(dict.fromkeys(rows))


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"method": "dated_environment_category", "listing_template": LISTING,
                     "pages_checked": [], "listing_failures": []})
    candidates = []
    boundary = False
    newest_visible = None
    for page in range(1, MAX_PAGES + 1):
        url = LISTING.format(page=page)
        response = fetch(url)
        rows = _dated_links(BeautifulSoup(response.content, "html.parser"))
        if not rows:
            evidence["pages_checked"].append({"page": page, "url": url, "articles": 0, "stop": "empty"})
            boundary = True
            break
        dates = [published for _, published in rows]
        newest_visible = max(dates) if newest_visible is None else max(newest_visible, max(dates))
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
        raise RuntimeError("Georgia Trend category walk reached its page cap without a lower boundary")
    candidates.sort(reverse=True)
    result = list(dict.fromkeys(url for _, url in candidates))
    evidence["discovered_urls_before_dedup"] = len(candidates)
    evidence["discovered_urls_after_dedup"] = len(result)
    if not result and newest_visible and newest_visible < start:
        evidence["publication_inactive_for_window"] = True
        evidence["newest_visible_publication_date"] = newest_visible.isoformat()
    return result


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_selector = first_text(soup, ("h1.entry-title", "article h1", "h1"))
    body, body_selector = article_body(soup, ("article .basic-content-wrap", "article"))
    date_raw, date_source = published_signal(soup)
    published = parse_datetime(date_raw)
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "maintext": body, "date_publish": published,
        "parser": "georgiatrend_environment_h1_published_metadata_article_body",
        "parser_error": parser_error(title, body, published),
        "field_evidence": page_evidence(soup, title=title, title_selector=title_selector,
                                         date_raw=date_raw, date_source=date_source, body=body,
                                         body_selector=body_selector, status=response.status_code),
    }
