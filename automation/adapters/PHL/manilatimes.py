"""Manila Times category adapter using dated first-party article URLs."""
from __future__ import annotations

import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters.PHL.common import extract_article, fetch, normalize_url


class Config:
    source_name = "manilatimes.net"
    country_code = "PHL"
    is_env = False


DENY = (
    "/commencement/", "/title-1", "/Entertainment/", "/Funpage/", "/coach/",
    "/wp-content/", "/amp/", "/lifestyle/", "/opinions-and-editorials/", "/sports/",
    "/challenge-platform/", "/entertainment/", "/opinions/", "/wp-json/", "/weekly/",
    ".js", ".gif",
)

CATEGORIES = ("news", "regions", "world", "business")
DATED_URL = re.compile(r"/((?:20\d{2})/(?:0[1-9]|1[0-2])/(?:0[1-9]|[12]\d|3[01]))/")
ARCHIVE = "https://www.manilatimes.net/products/archive"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end:
        raise ValueError("start date follows end date")
    result, seen, days = [], set(), []
    current = start
    while current <= end:
        page_url = f"{ARCHIVE}?date={current.isoformat()}"
        response = fetch(page_url, timeout=40)
        soup = BeautifulSoup(response.content, "html.parser")
        page_date = soup.select_one("#date_value")
        observed_date = page_date.get("value") if page_date else None
        if observed_date != current.isoformat():
            raise RuntimeError(
                f"Manila Times archive returned {observed_date!r} for {current.isoformat()}"
            )
        selected = []
        for anchor in soup.select("a[href]"):
            raw = anchor.get("href") or ""
            match = DATED_URL.search(raw)
            if not match or match.group(1).replace("/", "-") != current.isoformat():
                continue
            path_tail = raw.split(match.group(0), 1)[-1]
            section = path_tail.split("/", 1)[0].casefold()
            if section not in CATEGORIES:
                continue
            url = normalize_url(raw, Config.source_name, DENY)
            if url and url not in seen:
                seen.add(url)
                result.append(url)
                selected.append(url)
        days.append({
            "date": current.isoformat(),
            "url": page_url,
            "selected": len(selected),
            "transport": getattr(response, "scrape_transport", "requests"),
        })
        current += timedelta(days=1)
    evidence.update({
        "strategy": "publisher_daily_archive_by_publication_date",
        "archive_endpoint": ARCHIVE,
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "included_sections": list(CATEGORIES),
        "days_checked": days,
        "coverage_complete": len(days) == (end - start).days + 1,
        "discovered_urls_after_filter": len(result),
        "blacklist_url_patterns": list(DENY),
    })
    return result


def scrape_url(url: str) -> dict:
    return extract_article(
        url, source=Config.source_name,
        body_selectors=(".article-body", ".article-content", ".story-content", "article"),
        visible_date_selectors=(
            'meta[name="datePublished"]', 'meta[property="article:published_time"]',
            'meta[itemprop="datePublished"]', "article time",
        ),
        prefer_visible_date=False,
        evidence_body_selectors=(".article-body", ".article-content"),
    )
