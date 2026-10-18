"""Pure Azatutyun adapter for the current Armenian news category archives."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.ARM.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "azatutyun.am"
    country_code = "ARM"
    is_env = False


BASE = "https://www.azatutyun.am"
CATEGORIES = ("armenia", "z/2039", "z/2040", "economy", "z/2081", "z/2095", "z/2041", "z/2038", "z/2037")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    result, seen, routes = [], set(), []
    for route in CATEGORIES:
        pages, boundary = [], False
        for page in range(1, 101):
            listing_url = f"{BASE}/{route}?p={page}"
            soup = BeautifulSoup(fetch(listing_url).content, "html.parser")
            rows = []
            for card in soup.select("li.mb-grid.archive-list__item"):
                anchor = card.select_one("a[href*='/a/']")
                date_node = card.select_one(".date")
                parsed = parse_datetime(date_node.get_text(" ", strip=True)) if date_node else None
                normalized = normalize_url(urljoin(BASE, anchor.get("href", "")), Config.source_name) if anchor else None
                if normalized and parsed:
                    rows.append((normalized, parsed.date()))
            dates = [published for _, published in rows]
            pages.append({
                "page": page, "url": listing_url, "rows": len(rows),
                "newest": max(dates).isoformat() if dates else None,
                "oldest": min(dates).isoformat() if dates else None,
            })
            if not rows:
                boundary = True
                break
            for url, published in rows:
                if lower <= published <= upper and url not in seen:
                    seen.add(url)
                    result.append(url)
            if max(dates) < lower:
                boundary = True
                break
        if not boundary:
            raise RuntimeError(f"Azatutyun route {route} did not reach requested start")
        routes.append({"route": route, "pages_checked": pages, "boundary_reached": boundary})
    evidence.update({
        "strategy": "dated_category_pagination",
        "routes": routes,
        "candidate_date_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "coverage_complete": all(item["boundary_reached"] for item in routes),
        "discovered_urls_after_filter": len(result),
    })
    return result


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        language="hy",
        title_selectors=("h1.title.pg-title", "main h1", "h1"),
        # Visual stories leave the normal WSW container empty and carry their
        # substantive text in publisher-authored gallery captions.
        body_selectors=(".wsw", ".content-floated-wrap", "#galleryItems", "article"),
        visible_date_selectors=("main time[datetime]", "time[datetime]", "main .date"),
        prefer_visible_date=True,
        evidence_body_selectors=(".wsw", ".content-floated-wrap", "#galleryItems"),
        preserve_figures=True,
    )
