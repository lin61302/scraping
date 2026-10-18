"""Observation-only Climate Change Commission press-release adapter."""
from __future__ import annotations

from datetime import date, timedelta

from adapters.PHL.common import clean, extract_article, fetch, normalize_url, parse_datetime
from bs4 import BeautifulSoup


class Config:
    source_name = "climate.gov.ph"
    country_code = "PHL"
    is_env = True


LISTING = "https://climate.gov.ph/newsroom/press-releases?page={page}"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    evidence.update({
        "strategy": "cutoff_driven_press_release_pagination",
        "listing_template": LISTING,
        "listing_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "pages_checked": [],
        "listing_failures": [],
        "coverage_complete": False,
    })
    results: list[tuple[str, date]] = []
    seen = set()
    old_page_streak = 0
    for page in range(1, 101):
        url = LISTING.format(page=page)
        try:
            soup = BeautifulSoup(fetch(url).content, "html.parser")
        except Exception as exc:
            evidence["listing_failures"].append({"url": url, "error": str(exc)})
            break
        cards = soup.select("div.row.mb-4.p-2")
        parsed_dates = []
        page_added = 0
        for card in cards:
            anchor = card.select_one("div.title a[href]")
            date_node = card.select_one("small, .date")
            published = parse_datetime(date_node.get_text(" ", strip=True) if date_node else None)
            candidate = normalize_url(anchor.get("href"), Config.source_name) if anchor else None
            if published:
                parsed_dates.append(published.date())
            if candidate and published and start <= published.date() <= upper and candidate not in seen:
                seen.add(candidate)
                results.append((candidate, published.date()))
                page_added += 1
        evidence["pages_checked"].append({
            "page": page,
            "url": url,
            "cards": len(cards),
            "dated_cards": len(parsed_dates),
            "new_candidates": page_added,
            "newest_date": max(parsed_dates).isoformat() if parsed_dates else None,
            "oldest_date": min(parsed_dates).isoformat() if parsed_dates else None,
        })
        if not cards:
            evidence["stop_reason"] = "empty_listing_page"
            evidence["coverage_complete"] = True
            break
        if not parsed_dates:
            evidence["stop_reason"] = "listing_dates_missing"
            break
        old_page_streak = old_page_streak + 1 if max(parsed_dates) < lower else 0
        if old_page_streak >= 2:
            evidence["stop_reason"] = "two_pages_entirely_before_cutoff"
            evidence["coverage_complete"] = True
            break
    if evidence["listing_failures"]:
        raise RuntimeError(f"Climate listing failed: {evidence['listing_failures'][-1]['error']}")
    results.sort(key=lambda item: item[1])
    evidence["discovered_urls_after_filter"] = len(results)
    return [url for url, _ in results]


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        title_selectors=('meta[property="og:title"]', 'h5[style*="margin-bottom"]'),
        body_selectors=("div.row.template-content div.col-sm-12", "div.row.template-content"),
        visible_date_text_selectors=("p.mb-0.mt-2", "h5[style*='margin-bottom'] small"),
        prefer_visible_date=True,
        evidence_body_selectors=("div.row.template-content div.col-sm-12",),
    )
