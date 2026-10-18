"""Observation-only Rappler adapter with live sitemap rollover discovery."""
from __future__ import annotations

from datetime import date
from urllib.parse import quote, urlencode, urlparse

from adapters.PHL.common import extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "rappler.com"
    country_code = "PHL"
    is_env = False


DENY = (
    "/sports/", "/entertainment/", "/video/", "/life-and-style/",
    "/the-wrap/", "/brandrap/", "/voices/",
)
def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    api_root = "https://www.rappler.com/wp-json/wp/v2/posts"
    params = {
        "after": f"{start.isoformat()}T00:00:00",
        "before": f"{end.isoformat()}T23:59:59",
        "per_page": 100,
        "orderby": "date",
        "order": "desc",
        "_fields": "id,date,link",
    }
    evidence.update({
        "strategy": "date_bounded_wordpress_rest_api",
        "discovery_endpoint": api_root,
        "publication_date_window": {"start": start.isoformat(), "end": end.isoformat()},
        "pages_checked": [],
        "page_failures": [],
        "blacklist_url_patterns": list(DENY),
        "coverage_complete": False,
    })
    result, seen = [], set()
    publisher_rows = excluded = 0
    expected_total = expected_pages = None
    page = 1
    while page <= 100:
        page_url = f"{api_root}?{urlencode({**params, 'page': page})}"
        try:
            response = fetch(page_url)
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Rappler WordPress API did not return a list")
        except Exception as exc:
            evidence["page_failures"].append({"page": page, "url": page_url, "error": str(exc)})
            break
        if expected_total is None:
            expected_total = int(response.headers.get("X-WP-Total", len(rows)))
            expected_pages = int(response.headers.get("X-WP-TotalPages", 1))
        evidence["pages_checked"].append({
            "page": page,
            "url": page_url,
            "rows": len(rows),
            "newest": rows[0].get("date") if rows else None,
            "oldest": rows[-1].get("date") if rows else None,
        })
        publisher_rows += len(rows)
        for row in rows:
            published = parse_datetime(row.get("date"))
            if published is None or not start <= published.date() <= end:
                raise RuntimeError(f"Rappler API returned an out-of-range date on page {page}")
            url = normalize_url(row.get("link") or "", Config.source_name, DENY)
            if not url:
                excluded += 1
                continue
            if url not in seen:
                seen.add(url)
                result.append(url)
        if page >= expected_pages:
            break
        page += 1
    if evidence["page_failures"]:
        raise RuntimeError(f"{len(evidence['page_failures'])} Rappler API pages failed")
    if expected_total is None or publisher_rows != expected_total:
        raise RuntimeError(f"Rappler API total mismatch: expected {expected_total}, rows {publisher_rows}")
    evidence.update({
        "publisher_total": expected_total,
        "publisher_pages": expected_pages,
        "excluded_by_url_policy": excluded,
        "discovered_urls_after_filter": len(result),
        "coverage_complete": True,
    })
    return result


def _restore_missing_publication_date(url: str, article: dict) -> None:
    """Use the publisher's post record when a page omits its publish label.

    Rappler occasionally keeps a live article body while removing all date
    markup from the rendered page. Its first-party WordPress record retains the
    immutable original ``date`` beside a separate ``modified`` value.
    """
    if article.get("date_publish"):
        return
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    endpoint = (
        "https://www.rappler.com/wp-json/wp/v2/posts"
        f"?slug={quote(slug)}&_fields=id,date,modified,link,status"
    )
    rows = fetch(endpoint).json()
    if not isinstance(rows, list) or len(rows) != 1:
        return
    post = rows[0]
    if urlparse(post.get("link") or "").path.rstrip("/") != urlparse(url).path.rstrip("/"):
        return
    published = parse_datetime(post.get("date"))
    if published is None:
        return
    article["date_publish"] = published
    article["field_sources"]["date_publish"] = "wp_rest.date"
    article["field_evidence"]["dates"].append({
        "kind": "published",
        "source": "wp_rest.date",
        "raw": post.get("date"),
        "post_id": post.get("id"),
        "modified": post.get("modified"),
        "retrieval_url": endpoint,
    })


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1.post-single__title", "article h1", "main h1", "h1"),
        # Rappler's long-form visual stories split the article across several
        # sibling ``section.article`` nodes. Their shared ``main`` wrapper is
        # the only single bounded container that contains the complete text.
        body_selectors=("div.post-single__content.entry-content", ".post-single__content", "article", "main"),
        visible_date_selectors=("article time", ".post-single__date time", "time"),
        prefer_visible_date=True,
        evidence_body_selectors=("div.post-single__content.entry-content", "article", "main"),
        reject_paragraph_patterns=("This is AI generated summarization", "ADVERTISEMENT"),
    )
    _restore_missing_publication_date(url, article)
    return article
