"""Inquirer latest-stories adapter with reusable browser clearance."""
from __future__ import annotations

import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters.PHL.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "inquirer.net"
    country_code = "PHL"
    is_env = False


DENY = (
    "//entertainment", "//esports", "//opinion", "//pop", "//sports", "/exam-results/",
    "paginate", ".php", "/mindandbody/", "/image/", "/sports/", ".js", ".phg", ".csc",
    "/nbndeal/", "/template/", "/tag/", "featured-gallery", "featured-headline",
    "featured-video", "/page-one-single/", "/page/", "/wp-content/", "/paskong-pinoy/",
)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    months = (
        "January|February|March|April|May|June|July|August|September|October|"
        "November|December"
    )
    date_pattern = re.compile(rf"\b(?:{months})\s+\d{{1,2}},\s+20\d{{2}}", re.I)
    page_cache = {}

    def load_page(page: int) -> dict:
        if page in page_cache:
            return page_cache[page]
        page_url = f"https://newsinfo.inquirer.net/category/latest-stories/page/{page}"
        response = fetch(page_url, timeout=45)
        soup = BeautifulSoup(response.content, "html.parser")
        cards = soup.select("div#ch-ls-box")
        rows = []
        for card in cards:
            anchor = card.select_one("a[href*='newsinfo.inquirer.net/']")
            match = date_pattern.search(card.get_text(" ", strip=True))
            parsed = parse_datetime(match.group(0)) if match else None
            if not anchor or not parsed:
                continue
            url = normalize_url(anchor.get("href") or "", "newsinfo.inquirer.net", DENY)
            if url and re.search(r"newsinfo\.inquirer\.net/\d+", url):
                rows.append((url, parsed.date()))
        if not rows:
            raise RuntimeError(f"Inquirer latest-stories page {page} yielded no dated article rows")
        value = {
            "page": page,
            "url": page_url,
            "rows": rows,
            "newest": max(item[1] for item in rows),
            "oldest": min(item[1] for item in rows),
            "transport": getattr(response, "scrape_transport", "requests"),
        }
        page_cache[page] = value
        return value

    # Locate the first page whose date range reaches the requested end. This
    # avoids replaying weeks of newer pages during an August-only update.
    low, high = 1, 1
    while load_page(high)["oldest"] > upper:
        low, high = high, high * 2
        if high > 4096:
            raise RuntimeError("Inquirer page search exceeded safety cap")
    while high - low > 1:
        middle = (low + high) // 2
        if load_page(middle)["oldest"] > upper:
            low = middle
        else:
            high = middle

    result, seen, pages = [], set(), []
    boundary = False
    for page in range(max(1, high - 1), high + 1200):
        item = load_page(page)
        selected = 0
        for url, published in item["rows"]:
            if lower <= published <= upper and url not in seen:
                seen.add(url)
                result.append(url)
                selected += 1
        pages.append({
            "page": page,
            "url": item["url"],
            "articles": len(item["rows"]),
            "selected": selected,
            "newest": item["newest"].isoformat(),
            "oldest": item["oldest"].isoformat(),
            "transport": item["transport"],
        })
        if item["oldest"] <= lower:
            boundary = True
            break
    if not boundary:
        raise RuntimeError("Inquirer latest-stories walk did not reach the requested start")
    evidence.update({
        "strategy": "browser_cleared_latest_stories_date_backwalk",
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "candidate_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "first_relevant_page": high,
        "pages_checked": pages,
        "boundary_reached": boundary,
        "coverage_complete": boundary,
        "discovered_urls_after_filter": len(result),
        "blacklist_url_patterns": list(DENY),
    })
    return result


def scrape_url(url: str) -> dict:
    return extract_article(
        url, source=Config.source_name,
        body_selectors=("#article_content", "#article-content", "article"),
        visible_date_selectors=('meta[property="article:published_time"]', "article time"),
        prefer_visible_date=False,
        evidence_body_selectors=("#article_content", "#article-content"),
        reject_paragraph_patterns=("ADVERTISEMENT", "Subscribe to our daily newsletter"),
    )
