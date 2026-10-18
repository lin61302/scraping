"""Pure Hetq environmental adapter across Armenian, English, and Russian."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.ARM.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "hetq.am"
    country_code = "ARM"
    is_env = True


BASE = "https://hetq.am"
LANGUAGES = ("hy", "en", "ru")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    result, seen, languages = [], set(), []
    for language in LANGUAGES:
        pages, boundary = [], False
        for page in range(1, 501):
            listing_url = f"{BASE}/{language}/articles/ecology" + (f"?page={page}" if page > 1 else "")
            soup = BeautifulSoup(fetch(listing_url).content, "html.parser")
            page_urls, dates = [], []
            for anchor in soup.select("a.news-block[href]"):
                normalized = normalize_url(urljoin(BASE, anchor.get("href", "")), Config.source_name)
                if normalized and f"/{language}/article/" in normalized and normalized not in page_urls:
                    page_urls.append(normalized)
                time_node = anchor.select_one("time[datetime]")
                parsed = parse_datetime(time_node.get("datetime")) if time_node else None
                if parsed:
                    dates.append(parsed.date())
            pages.append({
                "page": page, "url": listing_url, "rows": len(page_urls),
                "dated_rows": len(dates),
                "newest": max(dates).isoformat() if dates else None,
                "oldest": min(dates).isoformat() if dates else None,
            })
            if not page_urls:
                boundary = True
                break
            page_overlaps = not dates or max(dates) >= lower
            if page_overlaps:
                for url in page_urls:
                    if url not in seen:
                        seen.add(url)
                        result.append(url)
            if dates and max(dates) < lower:
                boundary = True
                break
        if not boundary:
            raise RuntimeError(f"Hetq {language} listing did not reach requested start")
        languages.append({"language": language, "pages_checked": pages, "boundary_reached": boundary})
    evidence.update({
        "strategy": "multilingual_ecology_listing_pagination",
        "languages": languages,
        "candidate_date_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "coverage_complete": all(item["boundary_reached"] for item in languages),
        "discovered_urls_after_filter": len(result),
    })
    return result


def scrape_url(url: str) -> dict:
    language = url.split("/", 4)[3] if len(url.split("/", 4)) > 3 else "hy"
    return extract_article(
        url,
        source=Config.source_name,
        language=language,
        title_selectors=('meta[property="og:title"]', "article h1:not(.loading)", "article h1"),
        body_selectors=(".news-content-block", "article.open-news-block"),
        visible_date_selectors=("article time[datetime]", "time[datetime]"),
        prefer_visible_date=True,
        evidence_body_selectors=(".news-content-block",),
    )

