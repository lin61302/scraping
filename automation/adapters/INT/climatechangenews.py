"""Pure observer for Climate Change News' legacy interested-category scope."""
from __future__ import annotations

import re
from datetime import date, datetime

from adapters.INT.common import (
    extract_article,
    normalize_url,
    response_sha256,
    sitemap_rows,
    url_set_sha256,
    xml_response,
)


class Config:
    source_name = "climatechangenews.com"
    country_code = "INT"
    is_env = True


INDEX = "https://www.climatechangenews.com/sitemap_index.xml"
POST_MAP = re.compile(r"/post-sitemap(\d*)\.xml$")
PATH_DAY = re.compile(r"^/(20\d{2})/(\d{2})/(\d{2})/")
INTERESTED_CATEGORIES = {
    "politics", "carbon-markets", "energy", "nature", "science", "justice", "finance",
}


def _path_day(url: str):
    from urllib.parse import urlparse
    match = PATH_DAY.match(urlparse(url).path)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 180:
        raise ValueError("invalid Climate Change News window (max 181 days)")
    index_response, index = xml_response(INDEX)
    children = []
    for row in sitemap_rows(index):
        match = POST_MAP.search(row["url"])
        if match:
            children.append((int(match.group(1) or 1), row))
    children.sort()
    if not children or [number for number, _ in children] != list(range(1, len(children) + 1)):
        raise RuntimeError("Climate Change News post-sitemap sequence is incomplete")
    selected = []
    before = after = False
    checked = []
    for number, child in children:
        response, soup = xml_response(child["url"])
        rows = sitemap_rows(soup)
        dated = []
        for row in rows:
            normalized = normalize_url(row["url"], {Config.source_name})
            published = _path_day(normalized) if normalized else None
            if published:
                dated.append(published)
                before = before or published < start
                after = after or published > end
                if start <= published <= end:
                    selected.append(normalized)
        checked.append({
            "sequence": number,
            "url": child["url"],
            "index_lastmod": child.get("lastmod"),
            "entries": len(rows),
            "path_dated_entries": len(dated),
            "oldest_path_date": min(dated).isoformat() if dated else None,
            "newest_path_date": max(dated).isoformat() if dated else None,
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
        })
    repeat_response, repeat_index = xml_response(INDEX)
    stable = [row["url"] for row in sitemap_rows(index)] == [row["url"] for row in sitemap_rows(repeat_index)]
    selected = sorted(set(selected), key=lambda item: (_path_day(item), item))
    coverage = bool(before and after and stable)
    evidence.update({
        "method": "complete_yoast_post_sitemap_inventory_with_exact_path_publication_days",
        "sitemap_index": INDEX,
        "index_sha256": response_sha256(index_response),
        "repeat_index_sha256": response_sha256(repeat_response),
        "index_url_set_stable": stable,
        "post_sitemaps": checked,
        "post_sitemap_sequence_complete": True,
        "boundary_before_start": before,
        "boundary_after_end": after,
        "path_date_is_candidate_only_article_page_date_is_authoritative": True,
        "raw_window_urls": len(selected),
        "selected_url_set_sha256": url_set_sha256(selected),
        "legacy_policy_categories": sorted(INTERESTED_CATEGORIES),
        "coverage_complete": coverage,
    })
    if not coverage:
        raise RuntimeError("Climate Change News sitemap inventory did not close both date boundaries")
    return selected


def scrape_url(url: str) -> dict:
    article, soup = extract_article(
        url=url,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("article h1.post__title", "h1.post__title", "article h1"),
        body_selectors=("#mepr-full-content", "#mepr-truncated-content", ".post__content-main"),
        remove_selectors=(".mepr-unauthorized-message", ".blurred-fifth-paragraph", "blockquote"),
        parser="ccn_visible_h1_jsonld_date_memberpress_body",
    )
    post = soup.select_one("article.post")
    class_list = list(post.get("class") or []) if post else []
    categories = sorted({value[len("category-"):] for value in class_list if value.startswith("category-")})
    article["categories"] = categories
    article["field_evidence"]["publisher_categories"] = categories
    article["field_evidence"]["publisher_article_classes"] = class_list
    if not INTERESTED_CATEGORIES.intersection(categories):
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_outside_legacy_interested_categories"
        article["exclusion_evidence"] = {
            "policy_type": "legacy_interested_categories",
            "publisher_categories": categories,
            "interested_categories": sorted(INTERESTED_CATEGORIES),
        }
    path_date = _path_day(url)
    if path_date and article.get("date_publish") and article["date_publish"].date() != path_date:
        article["parser_error"] = "url_path_vs_page_publication_date_disagreement"
    return article
