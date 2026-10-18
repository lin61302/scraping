"""Pure theSun adapter using the WordPress REST publication-date frontier."""
from __future__ import annotations

from datetime import date, timedelta

from adapters.MYS.common import extract_article, fetch, normalize_url


class Config:
    source_name = "thesun.my"
    country_code = "MYS"
    is_env = False


API = "https://thesun.my/wp-json/wp/v2/posts"
DENY = ("/world/reuters",)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    params = (
        f"after={lower.isoformat()}T00:00:00&before={upper.isoformat()}T23:59:59&"
        "per_page=100&orderby=date&order=asc&_fields=id,date,link,status"
    )
    first_url = f"{API}?{params}&page=1"
    first = fetch(first_url)
    pages = int(first.headers.get("X-WP-TotalPages", "1"))
    total = int(first.headers.get("X-WP-Total", "0"))
    evidence.update({
        "strategy": "wordpress_rest_api_publication_date_window",
        "api": API,
        "query_window": {"after": lower.isoformat(), "before": upper.isoformat()},
        "api_total": total,
        "api_pages": pages,
        "pages_checked": [],
        "page_failures": [],
        "blacklist_url_patterns": list(DENY),
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
                article_url = normalize_url(str(row.get("link") or ""), Config.source_name, DENY)
                if row.get("status") == "publish" and article_url and article_url not in seen:
                    seen.add(article_url)
                    found.append(article_url)
        except Exception as exc:
            evidence["page_failures"].append({"page": page, "url": url, "error": str(exc)})
    if evidence["page_failures"]:
        raise RuntimeError(f"{len(evidence['page_failures'])} theSun REST pages failed")
    evidence.update({
        "discovered_urls_after_filter": len(found),
        "coverage_complete": len(evidence["pages_checked"]) == pages,
    })
    return found


def scrape_url(url: str) -> dict:
    return extract_article(
        url,
        source=Config.source_name,
        language="en",
        title_selectors=("h1.tdb-title-text", "article h1", "main h1", "h1"),
        body_selectors=(".td-post-content", ".tdb_single_content", ".entry-content", "article"),
        visible_date_selectors=("time.entry-date", "article time", "time"),
        evidence_body_selectors=(".td-post-content", ".entry-content", "article", "main"),
    )
