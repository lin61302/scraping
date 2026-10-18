"""Pure Malay Mail adapter using date-bearing category pagination."""
from __future__ import annotations

from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters.MYS.common import extract_article, fetch, normalize_url, url_date


class Config:
    source_name = "malaymail.com"
    country_code = "MYS"
    is_env = False


CATEGORIES = ("malaysia", "singapore", "world", "opinion")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    evidence.update({
        "strategy": "category_pagination_with_permalink_date_stop",
        "categories": list(CATEGORIES),
        "category_pages": {},
        "listing_failures": [],
        "coverage_complete": False,
    })
    found, seen = [], set()
    for category in CATEGORIES:
        pages = []
        crossed_lower = False
        previous_signature = None
        for page in range(1, 801):
            listing_url = f"https://www.malaymail.com/morearticles/{category}?pgno={page}"
            try:
                soup = BeautifulSoup(fetch(listing_url).content, "html.parser")
            except Exception as exc:
                evidence["listing_failures"].append({"url": listing_url, "error": str(exc)})
                break
            page_urls = []
            for anchor in soup.select(".article-item h2.article-title a[href]"):
                url = normalize_url(anchor.get("href", ""), Config.source_name)
                if url and url_date(url):
                    page_urls.append(url)
            page_urls = list(dict.fromkeys(page_urls))
            signature = tuple(page_urls)
            dated = [url_date(url) for url in page_urls]
            pages.append({
                "page": page,
                "url": listing_url,
                "entries": len(page_urls),
                "newest_permalink_date": max(dated).isoformat() if dated else None,
                "oldest_permalink_date": min(dated).isoformat() if dated else None,
            })
            if not page_urls or signature == previous_signature:
                crossed_lower = bool(page_urls and dated and min(dated) < lower)
                break
            previous_signature = signature
            for url, published in zip(page_urls, dated):
                if lower <= published <= upper and url not in seen:
                    seen.add(url)
                    found.append(url)
            # The template repeats a small latest-news rail on every page, so
            # max(date) never ages. The paginated article grid is descending;
            # seeing its oldest permalink cross the overlap is the stop signal.
            if dated and min(dated) < lower:
                crossed_lower = True
                break
        evidence["category_pages"][category] = {
            "pages_checked": len(pages),
            "last_page": pages[-1] if pages else None,
            "crossed_requested_lower_bound": crossed_lower,
        }
        if not crossed_lower:
            evidence["listing_failures"].append({
                "category": category,
                "error": "pagination did not cross requested lower bound",
            })
    evidence.update({
        "discovered_urls_after_filter": len(found),
        "coverage_complete": not evidence["listing_failures"],
    })
    if any("url" in failure for failure in evidence["listing_failures"]):
        raise RuntimeError(f"{len(evidence['listing_failures'])} Malay Mail listing failures")
    return found


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        language="en",
        title_selectors=("h1.article-title", "article h1", "main h1", "h1"),
        body_selectors=(
            ".main-container-article-body .article-body",
            ".malaymail-article-details .article-body",
            ".article-body",
            "article",
        ),
        visible_date_selectors=("time", "[itemprop='datePublished']"),
        evidence_body_selectors=(".article-body", "article", "main"),
    )
