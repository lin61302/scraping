"""Observation-only Daily Tribune adapter using its public latest-story RSS."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

from adapters.PHL.common import clean, extract_article, fetch, normalize_url, parse_datetime
from bs4 import BeautifulSoup


class Config:
    source_name = "tribune.net.ph"
    country_code = "PHL"
    is_env = False


FEED = "https://tribune.net.ph/api/v1/collections/latest.rss?limit={limit}&offset={offset}"
DENY = ("/sports/", "/snaps/", "/life/")
DENY_CATEGORIES = {"sports", "life", "photos", "snaps"}


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    limit = 500
    evidence.update({
        "strategy": "cutoff_driven_public_latest_rss",
        "feed_template": FEED,
        "feed_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "pages_checked": [],
        "feed_failures": [],
        "blacklist_url_patterns": list(DENY),
        "blacklist_categories": sorted(DENY_CATEGORIES),
        "excluded_by_category": 0,
        "coverage_complete": False,
    })
    results: list[tuple[str, date]] = []
    seen = set()
    for page in range(50):
        offset = page * limit
        feed_url = FEED.format(limit=limit, offset=offset)
        try:
            soup = BeautifulSoup(fetch(feed_url).content, "xml")
        except Exception as exc:
            evidence["feed_failures"].append({"url": feed_url, "error": str(exc)})
            break
        items = soup.find_all("item")
        parsed_dates = []
        page_added = 0
        for item in items:
            raw_url = item.find("link").get_text(strip=True) if item.find("link") else None
            raw_date = item.find("pubDate").get_text(strip=True) if item.find("pubDate") else None
            categories = {
                node.get_text(" ", strip=True).casefold() for node in item.find_all("category")
            }
            published = parse_datetime(raw_date)
            candidate = normalize_url(raw_url, Config.source_name, DENY) if raw_url else None
            if published:
                parsed_dates.append(published.date())
            if categories & DENY_CATEGORIES:
                evidence["excluded_by_category"] += 1
                continue
            if candidate and published and start <= published.date() <= upper and candidate not in seen:
                seen.add(candidate)
                results.append((candidate, published.date()))
                page_added += 1
        evidence["pages_checked"].append({
            "offset": offset,
            "url": feed_url,
            "items": len(items),
            "dated_items": len(parsed_dates),
            "new_candidates": page_added,
            "newest_date": max(parsed_dates).isoformat() if parsed_dates else None,
            "oldest_date": min(parsed_dates).isoformat() if parsed_dates else None,
        })
        if not items or len(items) < limit:
            evidence["stop_reason"] = "feed_exhausted"
            evidence["coverage_complete"] = True
            break
        if not parsed_dates:
            evidence["stop_reason"] = "feed_dates_missing"
            break
        if min(parsed_dates) < lower:
            evidence["stop_reason"] = "feed_crossed_cutoff"
            evidence["coverage_complete"] = True
            break
    if evidence["feed_failures"]:
        raise RuntimeError(f"Daily Tribune feed failed: {evidence['feed_failures'][-1]['error']}")
    if not evidence["coverage_complete"]:
        raise RuntimeError("Daily Tribune feed did not prove coverage through the requested cutoff")
    results.sort(key=lambda item: item[1])
    evidence["discovered_urls_after_filter"] = len(results)
    return [url for url, _ in results]


def _flight_story_captions(soup: BeautifulSoup) -> list[str]:
    captions: list[str] = []
    prefix = "self.__next_f.push("
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        if not raw.startswith(prefix) or not raw.endswith(")"):
            continue
        try:
            decoded = json.loads(raw[len(prefix):-1])[1]
        except (json.JSONDecodeError, IndexError, TypeError):
            continue
        for match in re.finditer(
            r'"className":"story-caption","children":"((?:\\.|[^"\\])*)"', decoded
        ):
            try:
                value = json.loads(f'"{match.group(1)}"')
            except json.JSONDecodeError:
                continue
            value = clean(value)
            if value and value not in captions:
                captions.append(value)
    return captions


def _apply_verified_photo_caption_repair(article: dict, soup: BeautifulSoup) -> bool:
    if clean(article.get("maintext")) or not article.get("title") or not article.get("date_publish"):
        return False
    page_article = soup.select_one("article[data-story-id][data-story-path][data-story-headline]")
    if not page_article:
        return False
    parsed_article_url = urlparse(article["url"])
    if page_article.get("data-story-path") != parsed_article_url.path:
        return False
    if clean(page_article.get("data-story-headline")) != clean(article.get("title")):
        return False
    story_text = soup.select_one("div.story-text")
    if story_text and clean(story_text.get_text(" ", strip=True)):
        return False
    description = soup.select_one("meta[name='description'][content]")
    description_text = clean(description.get("content")) if description else None
    title = clean(article.get("title"))
    if description_text not in {title, f"{title} — Daily Tribune"}:
        return False
    image = soup.select_one("meta[property='og:image'][content]")
    if not image:
        return False
    image_url = urljoin(article["url"], image.get("content"))
    parsed_image = urlparse(image_url)
    if (parsed_image.hostname or "").casefold() != "images.assettype.com":
        return False
    if not parsed_image.path.startswith("/tribune/"):
        return False
    captions = _flight_story_captions(soup)
    if not captions or len(captions[0]) < 150:
        return False
    caption = captions[0]
    selector = "Next.js flight payload: .story-caption"
    evidence = article.setdefault("field_evidence", {})
    evidence["bodies"] = [{"kind": "publisher_photo_caption", "source": selector, "text": caption}]
    evidence["publisher_images"] = [{"url": image_url, "selector": "meta[property='og:image']"}]
    article["maintext"] = caption
    article.setdefault("field_sources", {})["maintext"] = selector
    return True


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        title_selectors=("article h1.story-headline", "h1.story-headline", "article h1"),
        body_selectors=("div.story-text",),
        visible_date_text_selectors=("p.meta-text time",),
        visible_date_selectors=('meta[property="article:published_time"]',),
        prefer_visible_date=True,
        evidence_body_selectors=("div.story-text",),
    )
    if not article.get("maintext"):
        soup = BeautifulSoup(fetch(url).content, "html.parser")
        _apply_verified_photo_caption_repair(article, soup)
    return article
