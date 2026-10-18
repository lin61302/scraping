"""Pure observer for New Eastern Europe's complete Yoast post inventory."""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

from adapters.INT.common import (
    extract_article,
    normalize_url,
    response_sha256,
    sitemap_rows,
    url_set_sha256,
    xml_response,
)


class Config:
    source_name = "neweasterneurope.eu"
    country_code = "REG"
    is_env = False


INDEX = "https://neweasterneurope.eu/sitemap_index.xml"
POST_MAP = re.compile(r"/post-sitemap(\d*)\.xml$")
PATH_DAY = re.compile(r"^/(20\d{2})/(\d{2})/(\d{2})/")


def _path_day(url: str):
    match = PATH_DAY.match(urlparse(url).path)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 180:
        raise ValueError("invalid New Eastern Europe window (max 181 days)")
    index_response, index = xml_response(INDEX)
    children = []
    for row in sitemap_rows(index):
        match = POST_MAP.search(row["url"])
        if match:
            children.append((int(match.group(1) or 1), row))
    children.sort()
    if not children or [number for number, _ in children] != list(range(1, len(children) + 1)):
        raise RuntimeError("New Eastern Europe post-sitemap sequence is incomplete")
    checked = []
    selected = []
    before = after = False
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
            "entries": len(rows),
            "oldest_path_date": min(dated).isoformat() if dated else None,
            "newest_path_date": max(dated).isoformat() if dated else None,
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
        })
    selected = sorted(set(selected), key=lambda item: (_path_day(item), item))
    coverage = bool(before and after)
    evidence.update({
        "method": "complete_yoast_post_sitemap_inventory_with_exact_path_publication_days",
        "sitemap_index": INDEX,
        "index_sha256": response_sha256(index_response),
        "post_sitemaps": checked,
        "post_sitemap_sequence_complete": True,
        "boundary_before_start": before,
        "boundary_after_end": after,
        "selected_url_set_sha256": url_set_sha256(selected),
        "path_date_is_candidate_only_article_page_date_is_authoritative": True,
        "coverage_complete": coverage,
    })
    if not coverage:
        raise RuntimeError("New Eastern Europe sitemap inventory did not close both boundaries")
    return selected


def scrape_url(url: str) -> dict:
    article, _ = extract_article(
        url=url,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("h1.post-title", "main h1", "h1"),
        body_selectors=(".single-post-content", "#single-content"),
        remove_selectors=(".post-heading", ".thumb", ".media-block__meta", ".author-box", "blockquote"),
        parser="nee_visible_h1_jsonld_date_single_post_body",
    )
    path_day = _path_day(url)
    if path_day and article.get("date_publish") and article["date_publish"].date() != path_day:
        article["parser_error"] = "url_path_vs_page_publication_date_disagreement"
    return article
