"""Pure MGTC environmental in-the-news adapter."""
from __future__ import annotations

from datetime import date, timedelta

from adapters.MYS.common import extract_article, fetch, normalize_url


class Config:
    source_name = "mgtc.gov.my"
    country_code = "MYS"
    is_env = True


API = "https://www.mgtc.gov.my/wp-json/wp/v2/posts"
CATEGORY_ID = 5  # live /category/in-the-news taxonomy id


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    params = (
        f"categories={CATEGORY_ID}&after={lower.isoformat()}T00:00:00&"
        f"before={upper.isoformat()}T23:59:59&per_page=100&orderby=date&order=asc&"
        "_fields=id,date,link,status,categories"
    )
    first_url = f"{API}?{params}&page=1"
    first = fetch(first_url)
    pages = int(first.headers.get("X-WP-TotalPages", "0"))
    total = int(first.headers.get("X-WP-Total", "0"))
    evidence.update({
        "strategy": "wordpress_rest_category_publication_date_window",
        "api": API,
        "category_id": CATEGORY_ID,
        "category_evidence": "GET /wp-json/wp/v2/categories?slug=in-the-news",
        "query_window": {"after": lower.isoformat(), "before": upper.isoformat()},
        "api_total": total,
        "api_pages": pages,
        "pages_checked": [],
        "page_failures": [],
        "coverage_complete": False,
    })
    found, seen = [], set()
    for page in range(1, pages + 1):
        url = f"{API}?{params}&page={page}"
        try:
            response = first if page == 1 else fetch(url)
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("REST response is not a list")
            evidence["pages_checked"].append({"page": page, "entries": len(rows)})
            for row in rows:
                article_url = normalize_url(str(row.get("link") or ""), Config.source_name)
                if (
                    row.get("status") == "publish"
                    and CATEGORY_ID in (row.get("categories") or [])
                    and article_url and article_url not in seen
                ):
                    seen.add(article_url)
                    found.append(article_url)
        except Exception as exc:
            evidence["page_failures"].append({"page": page, "url": url, "error": str(exc)})
    if evidence["page_failures"]:
        raise RuntimeError(f"{len(evidence['page_failures'])} MGTC REST pages failed")
    evidence.update({
        "discovered_urls_after_filter": len(found),
        "coverage_complete": len(evidence["pages_checked"]) == pages,
        "inactivity_verified": total == 0,
        "publication_inactive_for_window": bool(
            total == 0 and not found and len(evidence["pages_checked"]) == pages
        ),
    })
    return found


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        language="en",
        title_selectors=(
            ".elementor-widget-theme-post-title h1",
            "article h1",
            "main h1",
        ),
        body_selectors=(".elementor-widget-theme-post-content", ".entry-content", "article"),
        visible_date_selectors=("[itemprop='datePublished']", "article time", "time"),
        evidence_body_selectors=(".elementor-widget-theme-post-content", ".entry-content", "article"),
    )
