"""Pure ArmInfo Eco adapter with Russian listing and page parsers."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.ARM.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "arminfo.info"
    country_code = "ARM"
    is_env = True


BASE = "https://arminfo.info/"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    result, seen, pages, boundary = [], set(), [], False
    for page in range(1, 201):
        listing_url = f"https://arminfo.info/news.php?pagenum={page}&stpage=11&n=5&lang=2"
        soup = BeautifulSoup(fetch(listing_url).content, "html.parser")
        rows = []
        for block in soup.select(".news_block"):
            anchor = block.select_one(".pages_article_title a[href]")
            date_node = block.select_one(".news_date")
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
        raise RuntimeError("ArmInfo listing did not reach requested start")
    evidence.update({
        "strategy": "dated_eco_listing_pagination",
        "pages_checked": pages,
        "candidate_date_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "coverage_complete": boundary,
        "discovered_urls_after_filter": len(result),
    })
    return result


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        language="ru",
        title_selectors=("h1.article_title", ".pages_article_title", "h1"),
        body_selectors=(".intrig_style div[style*='font-size:14px'][style*='text-align:justify']", ".intrig_style"),
        visible_date_selectors=(".intrig_style > div[style*='color:#17708d']",),
        prefer_visible_date=True,
        evidence_body_selectors=(".intrig_style div[style*='font-size:14px'][style*='text-align:justify']",),
    )
    if article.get("maintext", "").startswith("АрмИнфо ."):
        article["maintext"] = article["maintext"].replace("АрмИнфо .", "", 1).strip()
    return article

