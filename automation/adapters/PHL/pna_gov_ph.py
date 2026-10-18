"""PNA environment adapter with a reusable browser clearance session."""
from __future__ import annotations

import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters.PHL.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "pna.gov.ph"
    country_code = "PHL"
    is_env = True


PUBLISHED_DATE_PATTERN = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s+20\d{2},\s+\d{1,2}:\d{2}\s*(?:am|pm)",
    re.I,
)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    date_pattern = re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|"
        r"November|December)\s+\d{1,2},\s+20\d{2}(?:,\s+\d{1,2}:\d{2}\s*(?:am|pm))?",
        re.I,
    )
    result, seen, pages = [], set(), []
    boundary = False
    for page in range(1, 80):
        page_url = f"https://www.pna.gov.ph/categories/environment?p={page}"
        response = fetch(page_url, timeout=45)
        soup = BeautifulSoup(response.content, "html.parser")
        cards = soup.select("div.article-item")
        if not cards:
            raise RuntimeError(f"PNA environment page {page} contained no article cards")
        page_dates = []
        selected = 0
        for card in cards:
            anchor = card.select_one("h2 a[href*='/articles/'], a[href*='/articles/']")
            match = date_pattern.search(card.get_text(" ", strip=True))
            published = parse_datetime(match.group(0)).date() if match and parse_datetime(match.group(0)) else None
            if published:
                page_dates.append(published)
            if not anchor or (published and not (lower <= published <= upper)):
                continue
            url = normalize_url(anchor.get("href") or "", Config.source_name)
            if url and url not in seen:
                seen.add(url)
                result.append(url)
                selected += 1
        if not page_dates:
            raise RuntimeError(f"PNA environment page {page} had no publication dates")
        pages.append({
            "page": page,
            "url": page_url,
            "articles": len(cards),
            "selected": selected,
            "newest": max(page_dates).isoformat(),
            "oldest": min(page_dates).isoformat(),
            "transport": getattr(response, "scrape_transport", "requests"),
        })
        if min(page_dates) <= lower:
            boundary = True
            break
    if not boundary:
        raise RuntimeError("PNA category walk did not reach the requested start date")
    evidence.update({
        "strategy": "browser_cleared_environment_category_backwalk",
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "candidate_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "pages_checked": pages,
        "boundary_reached": boundary,
        "coverage_complete": boundary,
        "discovered_urls_after_filter": len(result),
        "blacklist_url_patterns": [],
    })
    return result


def scrape_url(url: str) -> dict:
    article = extract_article(
        url, source=Config.source_name,
        title_selectors=("main h1", "h1"),
        body_selectors=(".article-content", "main"),
        visible_date_text_selectors=("main span.block", "span.block"),
        visible_date_pattern=(
            r"\b(?:January|February|March|April|May|June|July|August|September|October|"
            r"November|December)\s+\d{1,2},\s+20\d{2},\s+\d{1,2}:\d{2}\s*(?:am|pm)"
        ),
        prefer_visible_date=True,
        evidence_body_selectors=(".article-content",),
    )
    # Updated PNA stories render publication and modification timestamps in
    # one text node ("August 18 ... Updated on August 18 ...").  The generic
    # date parser correctly refuses that two-date string.  Recover only the
    # leading publisher timestamp and retain the full combined value as audit
    # evidence so an update time can never replace the publication time.
    if article.get("date_publish") is None:
        for signal in article.get("field_evidence", {}).get("dates", []):
            if signal.get("kind") != "visible_time":
                continue
            match = PUBLISHED_DATE_PATTERN.search(signal.get("raw") or "")
            published = parse_datetime(match.group(0)) if match else None
            if published is None:
                continue
            source = f"{signal.get('source')}::leading_publication_timestamp"
            article["date_publish"] = published
            article["field_sources"]["date_publish"] = source
            article["field_evidence"]["dates"].insert(0, {
                "kind": "published",
                "source": source,
                "raw": match.group(0),
            })
            break
    return article
