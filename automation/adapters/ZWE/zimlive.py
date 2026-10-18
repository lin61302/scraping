"""Observation-only adapter for zimlive.com. No database writes."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from urllib.parse import urlencode

from adapters.ZWE.common import fetch, normalize_url, parse_datetime, wordpress_rest_article


class Config:
    source_name = "zimlive.com"
    country_code = "ZWE"
    is_env = False


INDEX = "https://www.zimlive.com/sitemap_index.xml"
UNINTERESTED = {
    "life & style", "life-style", "life-and-style", "travel", "sport", "football",
    "showbiz", "events guide", "events-guide", "arts & culture", "arts-culture",
}
POST_CACHE: dict[str, dict] = {}


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence["legacy_sitemap_index"] = INDEX
    evidence["discovery_change"] = (
        "Count-reconciled, date-bounded WordPress API windows replace sitemap "
        "lastmod filtering and blocked page-number pagination."
    )
    endpoint = f"http://{Config.source_name}/wp-json/wp/v2/posts"
    evidence.update({
        "discovery_endpoint": endpoint,
        "publication_date_window": {"start": start.isoformat(), "end": end.isoformat()},
        "pages_checked": [],
        "window_splits": [],
        "page_failures": [],
    })
    if start > end:
        raise ValueError("start date follows end date")
    POST_CACHE.clear()

    def one_page(window_start: date, window_end: date) -> list[str]:
        params = {
            "after": f"{window_start.isoformat()}T00:00:00",
            "before": f"{window_end.isoformat()}T23:59:59",
            "per_page": 100,
            "orderby": "date",
            "order": "desc",
            "page": 1,
        }
        request_url = f"{endpoint}?{urlencode(params)}"
        try:
            response = fetch(request_url)
            posts = response.json()
            if not isinstance(posts, list):
                raise ValueError("WordPress API did not return a list")
            total = int(response.headers["X-WP-Total"])
            page_count = int(response.headers["X-WP-TotalPages"])
        except Exception as exc:
            evidence["page_failures"].append({
                "start": window_start.isoformat(), "end": window_end.isoformat(),
                "url": request_url, "error": str(exc),
            })
            raise RuntimeError(f"ZimLive discovery failed for {window_start}–{window_end}") from exc

        if page_count > 1:
            if window_start == window_end:
                raise RuntimeError(f"ZimLive published more than 100 posts on {window_start}")
            midpoint = window_start + timedelta(days=(window_end - window_start).days // 2)
            evidence["window_splits"].append({
                "start": window_start.isoformat(), "end": window_end.isoformat(),
                "publisher_total": total, "publisher_pages": page_count,
            })
            return one_page(window_start, midpoint) + one_page(midpoint + timedelta(days=1), window_end)

        if len(posts) != total:
            raise RuntimeError(
                f"ZimLive count mismatch for {window_start}–{window_end}: "
                f"header {total}, returned {len(posts)}"
            )
        urls = []
        for post in posts:
            published = parse_datetime(post.get("date"))
            if published is None or not window_start <= published.date() <= window_end:
                raise RuntimeError(f"ZimLive returned an out-of-window post on {request_url}")
            url = normalize_url(post.get("link") or "", Config.source_name)
            if url is None:
                raise RuntimeError(f"ZimLive returned a non-source article URL on {request_url}")
            # Existing ZimLive production rows retain WordPress's trailing
            # slash. Keep that publisher identity for exact URL deduplication.
            url += "/"
            urls.append(url)
            cached = dict(post)
            cached["_discovery_url"] = response.url
            POST_CACHE[url] = cached
        if len(set(urls)) != total:
            raise RuntimeError(f"ZimLive duplicate article URLs within {window_start}–{window_end}")
        evidence["pages_checked"].append({
            "start": window_start.isoformat(), "end": window_end.isoformat(),
            "url": request_url, "publisher_total": total, "rows": len(posts),
            "newest": posts[0].get("date") if posts else None,
            "oldest": posts[-1].get("date") if posts else None,
        })
        return urls

    urls = []
    current = start
    while current <= end:
        month_end = date(current.year, current.month, monthrange(current.year, current.month)[1])
        window_end = min(end, month_end)
        urls.extend(one_page(current, window_end))
        current = window_end + timedelta(days=1)
    if len(set(urls)) != len(urls):
        raise RuntimeError("ZimLive duplicate article URLs across date windows")
    evidence["publisher_total"] = sum(page["publisher_total"] for page in evidence["pages_checked"])
    evidence["discovered_urls_after_filter"] = len(urls)
    evidence["article_payloads_cached"] = len(POST_CACHE)
    evidence["boundary_reached"] = True
    return urls


def scrape_url(url: str) -> dict:
    post = POST_CACHE.get(url)
    article, category_terms = wordpress_rest_article(url, source=Config.source_name, post=post)
    categories = {
        value
        for item in category_terms
        for value in (str(item.get("name") or "").casefold(), str(item.get("slug") or "").casefold())
        if value
    }
    if post:
        categories.update(
            str(item)[len("category-"):].casefold()
            for item in post.get("class_list") or []
            if str(item).startswith("category-")
        )
        for node in (post.get("yoast_head_json") or {}).get("schema", {}).get("@graph", []):
            sections = node.get("articleSection") or []
            if isinstance(sections, str):
                sections = [sections]
            categories.update(str(section).casefold() for section in sections if section)
    article["page_categories"] = sorted(categories)
    if UNINTERESTED.intersection(categories):
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "legacy_uninterested_category"
        article["exclusion_evidence"] = {
            "source": "wp_rest.class_list / yoast_head_json.schema.articleSection / _embedded.wp:term",
            "categories": sorted(categories),
            "category_ids": post.get("categories", []) if post else [],
            "matched_policy_values": sorted(UNINTERESTED.intersection(categories)),
        }
    return article
