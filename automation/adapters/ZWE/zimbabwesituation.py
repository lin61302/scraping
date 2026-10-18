"""Observation-only adapter for zimbabwesituation.com."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from adapters.ZWE.common import (
    fetch,
    normalize_url,
    parse_datetime,
    sitemap_rows,
    wordpress_rest_article,
    wordpress_rest_discovery,
    xml_document,
)


class Config:
    source_name = "zimbabwesituation.com"
    country_code = "ZWE"
    is_env = False


INDEX = "https://www.zimbabwesituation.com/sitemap_index.xml"
POST_CACHE: dict[str, dict] = {}


def _post_number(url: str) -> int:
    match = re.search(r"post-sitemap(\d*)\.xml", url)
    return int(match.group(1) or 1) if match else 0


def _sitemap_candidates(start: date, end: date, evidence: dict) -> list[str]:
    lower = start - timedelta(days=2)
    upper = end + timedelta(days=1)
    children = [row for row in sitemap_rows(xml_document(INDEX)) if _post_number(row[0])]
    children.sort(key=lambda row: _post_number(row[0]), reverse=True)
    selected = []
    boundary_added = False
    for row in children:
        _, modified = row
        if modified is None or modified.date() >= lower:
            selected.append(row)
        elif not boundary_added:
            selected.append(row)
            boundary_added = True
            break
    evidence.update({
        "sitemap_index": INDEX,
        "post_sitemaps_total": len(children),
        "child_selection_start": lower.isoformat(),
        "candidate_lastmod_window": {"start": start.isoformat(), "end": upper.isoformat()},
        "boundary_child_included": boundary_added,
        "sitemaps_checked": [],
        "sitemap_failures": [],
    })
    urls = []
    seen = set()
    for sitemap_url, index_modified in selected:
        try:
            rows = sitemap_rows(xml_document(sitemap_url))
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
            continue
        evidence["sitemaps_checked"].append({
            "url": sitemap_url,
            "index_lastmod": index_modified.isoformat() if index_modified else None,
            "entries": len(rows),
        })
        for raw, modified in rows:
            if modified is not None and not (start <= modified.date() <= upper):
                continue
            url = normalize_url(raw, Config.source_name)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} selected post sitemaps failed")
    evidence["sitemap_candidate_count"] = len(urls)
    evidence["lastmod_role"] = "candidate discovery only; WordPress post.date is publication date"
    return urls


def _verify_spillover(url: str, start: date, end: date) -> dict:
    """Confirm a modified sitemap URL was published outside the run window."""
    slug = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    api_url = (
        f"https://www.{Config.source_name}/wp-json/wp/v2/posts?"
        + urlencode({"slug": slug, "_fields": "date,slug,link"})
    )
    response = fetch(api_url)
    posts = response.json()
    if not isinstance(posts, list) or len(posts) != 1:
        raise RuntimeError(f"Expected one publisher post for sitemap-only URL: {url}")
    post = posts[0]
    published = parse_datetime(post.get("date"))
    canonical = normalize_url(post.get("link") or "", Config.source_name)
    if canonical != url or published is None:
        raise RuntimeError(f"Publisher post identity/date mismatch for sitemap-only URL: {url}")
    if start <= published.date() <= end:
        raise RuntimeError(f"Publisher API omitted an in-window sitemap post: {url}")
    return {
        "url": url,
        "date_publish": published.isoformat(),
        "reason": "publisher_post_date_outside_requested_window",
        "publisher_evidence_url": response.url,
    }


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    api_evidence: dict = {}
    urls = wordpress_rest_discovery(
        Config.source_name, start, end, api_evidence, post_cache=POST_CACHE,
    )
    # Publication date and the article payload both come from the publisher's
    # date-bounded WordPress endpoint.  The sitemap is a useful independent
    # coverage check, but it is served through a separate route which has
    # intermittently timed out from the collection host.  Do not discard a
    # complete publisher API result merely because that secondary route is
    # unavailable; retain the failure explicitly for later review instead.
    sitemap_evidence: dict = {}
    try:
        sitemap_urls = _sitemap_candidates(start, end, sitemap_evidence)
    except Exception as exc:
        evidence.update(api_evidence)
        evidence.update({
            "strategy": "publisher_date_bounded_wp_rest_sitemap_reconciliation_deferred",
            "publisher_in_window_count": len(urls),
            "sitemap_reconciliation_status": "skipped_unreachable",
            "sitemap_evidence": sitemap_evidence,
            "sitemap_error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        })
        return urls
    api_set = set(urls)
    sitemap_set = set(sitemap_urls)
    missing_from_sitemap = sorted(api_set - sitemap_set)
    if missing_from_sitemap:
        raise RuntimeError(
            f"{len(missing_from_sitemap)} date-bounded publisher posts absent from selected post sitemaps"
        )
    sitemap_only = sorted(sitemap_set - api_set)
    # This small, bounded comparison catches modified older posts and posts
    # published after the cutoff. Every exclusion has its own publisher date.
    with ThreadPoolExecutor(max_workers=4) as executor:
        spillovers = list(executor.map(
            lambda url: _verify_spillover(url, start, end), sitemap_only,
        ))
    evidence.update(api_evidence)
    evidence.update({
        "strategy": "publisher_date_bounded_wp_rest_with_sitemap_reconciliation",
        "sitemap_evidence": sitemap_evidence,
        "sitemap_candidate_count": len(sitemap_urls),
        "publisher_in_window_count": len(urls),
        "publisher_in_window_missing_from_sitemap": missing_from_sitemap,
        "sitemap_only_page_confirmed_exclusions": spillovers,
        "sitemap_only_exclusion_count": len(spillovers),
        "sitemap_reconciliation_status": "complete",
    })
    return urls


def scrape_url(url: str) -> dict:
    post = POST_CACHE.get(url)
    article, _ = wordpress_rest_article(url, source=Config.source_name, post=post)
    if post and "/the-zim-monodialogues-" in url and len(article.get("maintext") or "") < 180:
        content_html = (post.get("content") or {}).get("rendered") or ""
        soup = BeautifulSoup(content_html, "html.parser")
        image_selector = "figure img[src], figure img[data-src]"
        image = soup.select_one(image_selector)
        if image is None:
            # Older Monodialogues items use the classic WordPress caption
            # wrapper rather than a Gutenberg figure element.
            image_selector = ".wp-caption img[src], .wp-caption img[data-src]"
            image = soup.select_one(image_selector)
        source = soup.select_one('a[href*="thezimbabwean.co/"]')
        if image and source:
            image_url = urljoin(url, image.get("data-src") or image.get("src"))
            article["field_evidence"]["publisher_images"] = [{
                "url": image_url,
                "alt": image.get("alt") or None,
                "selector": f"wp_rest.content.rendered {image_selector}",
            }]
            article["collection"] = "SKIP"
            article["exclusion_reason"] = "excluded_image_only_publisher_item"
            article["exclusion_evidence"] = {
                "policy_type": "publisher_image_only",
                "publisher_item_type": "image_led_cartoon",
                "publisher_image_url": image_url,
                "publisher_dom_selector": f"wp_rest.content.rendered {image_selector}",
                "source_link": source.get("href"),
                "short_visible_text": article.get("maintext"),
            }
    return article
