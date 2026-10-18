"""Pure observer for Green Climate Fund news and video pages.

Discovery uses the publisher's combined ``All news and updates`` listing.  It
reads the first page, which closes both requested date boundaries, and the
immediately following page to prove the dated sequence continues below the
lower boundary.  A repeated first-page request must expose the same URL/date
inventory before the interval is accepted as complete.

Publisher video pages follow the same field gate as ordinary articles.  Their
visible page description is the only eligible ``maintext``; this adapter does
not invent a transcript and does not silently exclude a video.  A video page
whose own textual body is missing or too short therefore remains an ordinary
validation quarantine in the observer output.

This module has no database imports, credentials, cache writes, or production
write path.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from adapters.INT.common import (
    clean,
    fetch,
    normalize_url,
    parse_datetime,
    response_sha256,
    url_set_sha256,
)


class Config:
    source_name = "greenclimate.fund"
    country_code = "INT"
    is_env = True


LISTING = "https://www.greenclimate.fund/about/news-and-updates/all"
DATE_TEXT = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+20\d{2}$")
ARTICLE_PATH = re.compile(r"^/about/news-and-updates/(?:stories/)?[^/]+/?$")
VIDEO_PATH = re.compile(r"^/video/[^/]+/?$")


def _canonical_item_url(raw: str) -> str | None:
    absolute = urljoin(LISTING, raw)
    normalized = normalize_url(absolute, {Config.source_name})
    if not normalized:
        return None
    path = urlparse(normalized).path
    if ARTICLE_PATH.fullmatch(path) or VIDEO_PATH.fullmatch(path):
        return normalized
    return None


def _listing_rows(soup: BeautifulSoup) -> list[dict]:
    rows = []
    for card in soup.select('[data-filters-results] [data-component="cards:news"]'):
        link = card.select_one("a[href]")
        title_node = card.select_one("p.f-body-bold")
        values = [
            clean(node.get_text(" ", strip=True))
            for node in card.select(
                '[data-component="lists:inline-bullets"] '
                'li > span.f-caption:not([aria-hidden="true"])'
            )
        ]
        values = [value for value in values if value]
        date_values = [value for value in values if DATE_TEXT.fullmatch(value)]
        type_values = [value for value in values if value not in date_values]
        url = _canonical_item_url(link.get("href")) if link else None
        title = clean(title_node.get_text(" ", strip=True)) if title_node else None
        published = parse_datetime(date_values[0]) if len(date_values) == 1 else None
        if not (url and title and published and len(type_values) == 1):
            raise RuntimeError("Green Climate Fund listing card lacks a unique URL, title, type, or date")
        rows.append({
            "url": url,
            "title": title,
            "content_type": type_values[0],
            "date_publish": published,
        })
    if not rows:
        raise RuntimeError("Green Climate Fund listing page has no news cards")
    return rows


def _dated_descending(rows: list[dict]) -> bool:
    return all(
        rows[index]["date_publish"] >= rows[index + 1]["date_publish"]
        for index in range(len(rows) - 1)
    )


def _page_record(page: int, url: str, response, rows: list[dict]) -> dict:
    return {
        "page": page,
        "url": url,
        "rows": len(rows),
        "unique_urls": len({row["url"] for row in rows}),
        "first_publication": rows[0]["date_publish"].isoformat(),
        "last_publication": rows[-1]["date_publish"].isoformat(),
        "dates_descending": _dated_descending(rows),
        "response_sha256": response_sha256(response),
        "response_bytes": len(response.content),
    }


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 180:
        raise ValueError("invalid Green Climate Fund window (max 181 days)")

    first_response = fetch(LISTING)
    first_soup = BeautifulSoup(first_response.content, "html.parser")
    first_rows = _listing_rows(first_soup)

    # Page numbers are zero based on this Drupal view.  Only the contiguous
    # next page is needed because page zero already spans both date boundaries.
    next_link = first_soup.select_one('[data-filters-pager] a[rel="next"][href]')
    if not next_link:
        raise RuntimeError("Green Climate Fund listing has no contiguous next page")
    second_url = urljoin(LISTING, next_link.get("href"))
    if "page=1" not in second_url:
        raise RuntimeError("Green Climate Fund listing next-page boundary is not page=1")
    second_response = fetch(second_url)
    second_rows = _listing_rows(BeautifulSoup(second_response.content, "html.parser"))

    repeat_response = fetch(LISTING)
    repeat_rows = _listing_rows(BeautifulSoup(repeat_response.content, "html.parser"))

    first_signature = [
        (row["url"], row["content_type"], row["date_publish"].isoformat())
        for row in first_rows
    ]
    repeat_signature = [
        (row["url"], row["content_type"], row["date_publish"].isoformat())
        for row in repeat_rows
    ]
    stable = first_signature == repeat_signature
    pages_descending = _dated_descending(first_rows) and _dated_descending(second_rows)
    contiguous = first_rows[-1]["date_publish"] >= second_rows[0]["date_publish"]
    boundary_after = any(row["date_publish"].date() > end for row in first_rows)
    boundary_before = any(row["date_publish"].date() < start for row in first_rows)
    next_page_below_start = all(row["date_publish"].date() < start for row in second_rows)

    combined_urls = [row["url"] for row in [*first_rows, *second_rows]]
    unique = len(combined_urls) == len(set(combined_urls))
    if not all((stable, pages_descending, contiguous, boundary_after, boundary_before, next_page_below_start, unique)):
        raise RuntimeError(
            "Green Climate Fund listing did not prove a stable, descending, closed date interval"
        )

    selected_rows = [
        row for row in first_rows
        if start <= row["date_publish"].date() <= end
    ]
    selected = [row["url"] for row in selected_rows]
    type_counts = {}
    month_counts = {}
    for row in selected_rows:
        type_counts[row["content_type"]] = type_counts.get(row["content_type"], 0) + 1
        month = row["date_publish"].strftime("%Y-%m")
        month_counts[month] = month_counts.get(month, 0) + 1

    evidence.update({
        "method": "stable_combined_news_listing_with_contiguous_older_page_boundary",
        "listing": LISTING,
        "listing_scope": "all publisher news and update content types, including video pages",
        "pages": [
            _page_record(0, LISTING, first_response, first_rows),
            _page_record(1, second_url, second_response, second_rows),
        ],
        "first_page_repeat_sha256": response_sha256(repeat_response),
        "first_page_inventory_stable": stable,
        "first_two_pages_unique_urls": unique,
        "publication_dates_descending": pages_descending,
        "page_boundary_contiguous": contiguous,
        "boundary_after_end": boundary_after,
        "boundary_before_start": boundary_before,
        "next_page_entirely_before_start": next_page_below_start,
        "selected": len(selected),
        "selected_content_type_counts": dict(sorted(type_counts.items())),
        "selected_month_counts": dict(sorted(month_counts.items())),
        "selected_video_pages": sum(1 for row in selected_rows if VIDEO_PATH.fullmatch(urlparse(row["url"]).path)),
        "selected_listing_rows": [
            {
                "url": row["url"],
                "title": row["title"],
                "content_type": row["content_type"],
                "date_publish": row["date_publish"].date().isoformat(),
            }
            for row in selected_rows
        ],
        "selected_url_set_sha256": url_set_sha256(selected),
        "listing_dates_are_candidate_inventory_dates_article_page_dates_are_authoritative": True,
        "video_policy": (
            "include publisher page only when date, title, and textual page body meet normal field gate; "
            "otherwise retain as video-page body quarantine; never invent transcript or silently exclude"
        ),
        "coverage_complete": True,
    })
    return selected


def _visible_header_date(soup: BeautifulSoup):
    header = soup.select_one('article > header[data-component="heros:article"]')
    candidates = []
    if header:
        for node in header.select("p.f-caption.text-secondary"):
            raw = clean(node.get_text(" ", strip=True))
            if raw and DATE_TEXT.fullmatch(raw):
                candidates.append(raw)
    if len(candidates) != 1:
        raise RuntimeError("Green Climate Fund article page lacks one visible publisher date")
    return parse_datetime(candidates[0]), candidates[0]


def _main_body(soup: BeautifulSoup) -> tuple[str | None, list[dict], int]:
    article = soup.select_one("article.node-article, article.node-video")
    if not article:
        return None, [], 0
    parts = []
    sources = []
    for index, section in enumerate(article.select('[data-component="content-sections:text"]'), start=1):
        section_parts = []
        for node in section.select("p, h2, h3, li"):
            value = clean(node.get_text(" ", strip=True))
            if not value or len(value) < 20:
                continue
            if value not in section_parts:
                section_parts.append(value)
        if not section_parts:
            continue
        for value in section_parts:
            if any(value in previous for previous in parts):
                continue
            parts = [previous for previous in parts if previous not in value]
            if value not in parts:
                parts.append(value)
        sources.append({
            "selector": '[data-component="content-sections:text"]',
            "section_index": index,
            "paragraphs": len(section_parts),
        })
    return ("\n\n".join(parts) if parts else None), sources, len(parts)


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    canonical = _canonical_item_url(response.url)
    if canonical != _canonical_item_url(url):
        raise RuntimeError("Green Climate Fund article redirected outside its canonical source path")

    article_node = soup.select_one("article.node-article, article.node-video")
    if not article_node:
        raise RuntimeError("Green Climate Fund page has no article or video node")
    is_video = "node-video" in (article_node.get("class") or [])

    title_node = article_node.select_one('header[data-component="heros:article"] h1')
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    published, visible_raw = _visible_header_date(soup)
    body, body_sources, paragraph_count = _main_body(soup)

    meta = soup.select_one('meta[property="article:published_time"][content]')
    meta_raw = clean(meta.get("content")) if meta else None
    meta_published = parse_datetime(meta_raw)
    if meta_raw and (not meta_published or not published or meta_published.date() != published.date()):
        raise RuntimeError("Green Climate Fund visible and metadata publication dates disagree")

    content_type_node = article_node.select_one(
        'header[data-component="heros:article"] '
        'span[data-component="primitives:tag:quaternary"] span.f-caption'
    )
    content_type = clean(content_type_node.get_text(" ", strip=True)) if content_type_node else None
    video_nodes = article_node.select('[data-component^="content-sections:video"] [data-video-url]')
    videos = [
        {
            "selector": '[data-component^="content-sections:video"] [data-video-url]',
            "page_url": clean(node.get("data-video-url")),
            "embed_url": clean((node.select_one("iframe") or {}).get("data-src")),
        }
        for node in video_nodes
    ]

    title_evidence = [{"kind": "visible_h1", "source": "article header h1", "raw": title}] if title else []
    og = soup.select_one('meta[property="og:title"][content]')
    og_title = clean(og.get("content")) if og else None
    if og_title and og_title != title:
        title_evidence.append({"kind": "og_title", "source": 'meta[property="og:title"]', "raw": og_title})
    date_evidence = [{
        "kind": "published", "source": "visible article header date", "raw": visible_raw,
    }]
    if meta_raw:
        date_evidence.append({
            "kind": "published", "source": 'meta[property="article:published_time"]', "raw": meta_raw,
        })

    missing = [
        name for name, value in (("title", title), ("date_publish", published), ("maintext", body))
        if not value
    ]
    return {
        "url": canonical or url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": clean((soup.html or {}).get("lang") if soup.html else None) or "en",
        "parser": "gcf_visible_header_and_text_sections",
        "collection": None,
        "parser_error": "missing:%s" % ",".join(missing) if missing else None,
        "content_type": content_type,
        "page_kind": "video" if is_video else "article",
        "field_sources": {
            "title": "article header h1",
            "date_publish": "visible article header date",
            "maintext": '[data-component="content-sections:text"]',
            "language": "html[lang]",
        },
        "field_evidence": {
            "http_status": response.status_code,
            "response_url": response.url,
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
            "transport": getattr(response, "scrape_transport", "requests"),
            "titles": title_evidence,
            "dates": date_evidence,
            "bodies": ([{
                "kind": "publisher_text_sections",
                "source": '[data-component="content-sections:text"]',
                "text": body[:20000],
            }] if body else []),
            "body_sections": body_sources,
            "paragraph_count": paragraph_count,
            "html_language": clean((soup.html or {}).get("lang") if soup.html else None),
            "publisher_content_type": content_type,
            "publisher_page_kind": "video" if is_video else "article",
            "publisher_videos": videos,
            "video_page_policy": (
                "publisher_text_only_normal_field_gate_no_transcript_invention_or_silent_exclusion"
                if is_video else None
            ),
        },
    }
