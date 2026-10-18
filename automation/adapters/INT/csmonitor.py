"""Pure bounded observer for The Christian Science Monitor.

The legacy implementations mixed keyword searches or a yearly sitemap with
direct MongoDB writes.  This adapter keeps only the publisher-owned sitemap
route.  It reads the complete live sitemap index, verifies the complete shard
sequence for the requested year, and rereads the target shards before
returning a bounded publication-date inventory.  Dates encoded in sitemap
paths are discovery candidates; article pages provide the authoritative
publication timestamp, visible headline, main text, canonical URL, and
language.

The publisher asks general crawlers to wait one second between requests.  A
process-wide throttle enforces that delay even though the intended observation
uses one worker.  This module has no database imports, credentials, cache
mutation, or production-write path.
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

from adapters.INT.common import (
    article_jsonld,
    clean,
    extract_article,
    fetch,
    first_text,
    normalize_url,
    response_sha256,
    sitemap_rows,
    url_set_sha256,
)


class Config:
    source_name = "csmonitor.com"
    country_code = "INT"
    is_env = False
    access_block_circuit_threshold = 3


INDEX = "https://www.csmonitor.com/sitemap-index.xml"
YEAR_MAP_RE = re.compile(r"^/sitemap-(20\d{2})-auto-(\d+)\.xml$")
ARTICLE_RE = re.compile(r"^/.+/(20\d{2})/(\d{2})(\d{2})/[^/]+/?$")
MIN_REQUEST_INTERVAL_SECONDS = 1.05
_DISCOVERY_ROWS: dict[str, dict] = {}
_THROTTLE_LOCK = threading.Lock()
_LAST_REQUEST_STARTED = 0.0


def _throttled_fetch(url: str):
    global _LAST_REQUEST_STARTED
    with _THROTTLE_LOCK:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_STARTED)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_STARTED = time.monotonic()
    return fetch(url)


def _xml(url: str):
    response = _throttled_fetch(url)
    # Use the common parser and root validation without performing a second
    # request.  xml_response cannot accept a prepared response, so keep its
    # behavior explicit here.
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(response.content, "xml")
    root = soup.find()
    if root is None or root.name not in {"sitemapindex", "urlset"}:
        raise RuntimeError("Christian Science Monitor returned non-sitemap XML")
    return response, soup


def _https_publisher_url(raw: str) -> str:
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in {Config.source_name, "www.%s" % Config.source_name}:
        raise RuntimeError("Christian Science Monitor sitemap index names another host")
    if parsed.scheme not in {"http", "https"} or not parsed.path:
        raise RuntimeError("Christian Science Monitor sitemap URL is invalid")
    return urlunparse(("https", "www.%s" % Config.source_name, parsed.path, "", "", ""))


def _year_sitemaps(year: int) -> tuple[object, list[dict]]:
    response, soup = _xml(INDEX)
    children = []
    seen = set()
    for row in sitemap_rows(soup):
        parsed = urlparse(row["url"])
        match = YEAR_MAP_RE.match(parsed.path)
        if not match or int(match.group(1)) != year:
            continue
        url = _https_publisher_url(row["url"])
        if url in seen:
            raise RuntimeError("Christian Science Monitor sitemap index repeats a year shard")
        seen.add(url)
        children.append({
            "sequence": int(match.group(2)),
            "url": url,
            "index_url": row["url"],
            "index_lastmod": row.get("lastmod"),
        })
    children.sort(key=lambda item: item["sequence"])
    sequences = [item["sequence"] for item in children]
    if not children or sequences != list(range(1, len(children) + 1)):
        raise RuntimeError("Christian Science Monitor year sitemap sequence is incomplete")
    return response, children


def _path_day(url: str) -> date | None:
    match = ARTICLE_RE.match(urlparse(url).path)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _scan_year_shard(child: dict, year: int) -> tuple[list[dict], dict]:
    response, soup = _xml(child["url"])
    rows = []
    malformed = []
    seen = set()
    for position, item in enumerate(sitemap_rows(soup), start=1):
        url = normalize_url(item["url"], {Config.source_name})
        if not url:
            raise RuntimeError("Christian Science Monitor year sitemap has a non-publisher URL")
        if url in seen:
            raise RuntimeError("Christian Science Monitor year sitemap repeats a canonical URL")
        seen.add(url)
        published_day = _path_day(url)
        if not published_day or published_day.year != year:
            malformed.append({
                "url": url,
                "position": position,
                "reason": "publisher_path_lacks_valid_YYYY_MMDD_article_date",
            })
            continue
        rows.append({
            "url": url,
            "path_date": published_day.isoformat(),
            "sitemap_sequence": child["sequence"],
            "sitemap_position": position,
            "sitemap_lastmod": item.get("lastmod"),
        })
    if not rows:
        raise RuntimeError("Christian Science Monitor year sitemap has no dated article URLs")
    return rows, {
        "sequence": child["sequence"],
        "url": child["url"],
        "index_url": child["index_url"],
        "index_lastmod": child.get("index_lastmod"),
        "entries": len(seen),
        "dated_article_entries": len(rows),
        "malformed_path_entries": len(malformed),
        "malformed_path_examples": malformed[:10],
        "url_set_sha256": url_set_sha256(seen),
        "dated_url_set_sha256": url_set_sha256(row["url"] for row in rows),
        "response_sha256": response_sha256(response),
        "response_bytes": len(response.content),
    }


def _signature(rows: list[dict]) -> list[tuple[str, str, int, int]]:
    return [
        (row["url"], row["path_date"], row["sitemap_sequence"], row["sitemap_position"])
        for row in rows
    ]


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or start.year != end.year or (end - start).days > 183:
        raise ValueError("Christian Science Monitor window must be within one year and at most 184 days")

    index_response, children = _year_sitemaps(start.year)
    all_rows = []
    shard_summaries = []
    for child in children:
        rows, summary = _scan_year_shard(child, start.year)
        all_rows.extend(rows)
        shard_summaries.append(summary)
    if len({row["url"] for row in all_rows}) != len(all_rows):
        raise RuntimeError("Christian Science Monitor year shards overlap")

    selected_rows = [
        row for row in all_rows
        if start <= date.fromisoformat(row["path_date"]) <= end
    ]
    selected = [row["url"] for row in selected_rows]
    if not selected or len(selected) != len(set(selected)):
        raise RuntimeError("Christian Science Monitor target inventory is empty or duplicated")

    before = [row for row in all_rows if date.fromisoformat(row["path_date"]) < start]
    after = [row for row in all_rows if date.fromisoformat(row["path_date"]) > end]
    if not before or not after:
        raise RuntimeError("Christian Science Monitor year sitemap did not close both date boundaries")
    prior = max(before, key=lambda row: (row["path_date"], row["url"]))
    following = min(after, key=lambda row: (row["path_date"], row["url"]))

    repeat_index_response, repeat_children = _year_sitemaps(start.year)
    if [(row["sequence"], row["url"]) for row in children] != [
        (row["sequence"], row["url"]) for row in repeat_children
    ]:
        raise RuntimeError("Christian Science Monitor sitemap index changed between reads")
    repeated_rows = []
    repeated_summaries = []
    for child in repeat_children:
        rows, summary = _scan_year_shard(child, start.year)
        repeated_rows.extend(rows)
        repeated_summaries.append(summary)
    if _signature(all_rows) != _signature(repeated_rows):
        raise RuntimeError("Christian Science Monitor year sitemap changed between reads")

    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in selected_rows})
    month_counts = Counter(row["path_date"][:7] for row in selected_rows)
    evidence.update({
        "method": "stable_complete_publisher_year_sitemap_sequence_filtered_by_path_date",
        "sitemap_index": INDEX,
        "year": start.year,
        "index_sha256": response_sha256(index_response),
        "repeat_index_sha256": response_sha256(repeat_index_response),
        "year_sitemap_sequence_complete": True,
        "year_sitemap_count": len(children),
        "year_sitemaps": shard_summaries,
        "repeated_year_sitemaps": repeated_summaries,
        "inventory_stable": True,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "selected": len(selected),
        "selected_month_counts": dict(sorted(month_counts.items())),
        "selected_oldest_path_date": min(row["path_date"] for row in selected_rows),
        "selected_newest_path_date": max(row["path_date"] for row in selected_rows),
        "selected_url_set_sha256": url_set_sha256(selected),
        "boundary_before_start": prior,
        "boundary_after_end": following,
        "publisher_crawl_delay_seconds_respected": MIN_REQUEST_INTERVAL_SECONDS,
        "sitemap_path_date_is_candidate_only_article_page_date_is_authoritative": True,
        "sitemap_lastmod_used_as_publication_date": False,
        "legacy_database_writer_executed": False,
        "coverage_complete": True,
    })
    return selected


def _aware_datetime(raw: object, label: str) -> datetime:
    value = clean(raw)
    if not value:
        raise RuntimeError("Christian Science Monitor lacks %s" % label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Christian Science Monitor has an invalid %s" % label) from exc
    if parsed.tzinfo is None:
        raise RuntimeError("Christian Science Monitor %s lacks timezone" % label)
    return parsed


def _publication_identity(
    *,
    path_day: date,
    metadata_raw: object,
    jsonld_published_raw: object,
    visible_raw: object,
    jsonld_modified_raw: object,
) -> dict:
    """Resolve publication and display timestamps without conflating roles.

    Christian Science Monitor updates the visible ``time`` element when an
    article is modified.  The publisher keeps the original publication instant
    in both ``article:published_time`` and ``NewsArticle.datePublished`` and
    records the update in ``NewsArticle.dateModified``.  Treat the URL's local
    calendar day plus those two publication signals as the publication
    identity.  A later visible day is accepted only when dateModified supports
    that exact day and is no earlier than publication.
    """
    metadata = _aware_datetime(metadata_raw, "article:published_time")
    jsonld_published = _aware_datetime(
        jsonld_published_raw, "NewsArticle datePublished"
    )
    visible = _aware_datetime(visible_raw, "visible time date")
    delta_seconds = abs(
        (
            metadata.astimezone(timezone.utc)
            - jsonld_published.astimezone(timezone.utc)
        ).total_seconds()
    )
    # The OpenGraph timestamp is minute-granular while JSON-LD carries seconds.
    if delta_seconds >= 60:
        raise RuntimeError(
            "Christian Science Monitor metadata and JSON-LD publication times disagree"
        )
    if metadata.date() != path_day:
        raise RuntimeError(
            "Christian Science Monitor publication timestamp disagrees with its URL date"
        )

    modified = None
    if visible.date() == path_day:
        visible_role = "publication_day"
    else:
        modified = _aware_datetime(
            jsonld_modified_raw, "NewsArticle dateModified"
        )
        if (
            visible.date() != modified.date()
            or modified.astimezone(timezone.utc)
            < jsonld_published.astimezone(timezone.utc)
        ):
            raise RuntimeError(
                "Christian Science Monitor visible time is not supported by dateModified"
            )
        visible_role = "modification_day"

    return {
        "metadata": metadata,
        "jsonld_published": jsonld_published,
        "visible": visible,
        "jsonld_modified": modified,
        "publication_signal_delta_seconds": delta_seconds,
        "visible_time_role": visible_role,
    }


def _publisher_image_only_comic(
    *, soup, target: str, visible_title: str, item: dict
) -> dict | None:
    """Return narrow first-party evidence for an image-only comic item.

    This deliberately requires the publisher's comic label, a body container
    containing one publisher image, no ordinary prose after promo/credit
    elements are removed, and no structured article body.  A missing body by
    itself never becomes an exclusion.
    """
    slug = urlparse(target).path.rstrip("/").rsplit("/", 1)[-1].casefold()
    if "comic" not in slug or "comic" not in visible_title.casefold():
        return None
    if clean(item.get("articleBody")):
        return None
    container = soup.select_one(".story .trinity-audio-read[data-field='body']")
    if container is None:
        return None
    clone = type(soup)(str(container), "html.parser")
    for selector in (
        "script", "style", ".trinity-skip-it", ".details", ".login-prompt",
        ".megawhy-block", "[class*='subscribe']", "[class*='promo']",
    ):
        for node in clone.select(selector):
            node.decompose()
    ordinary_text = clean(clone.get_text(" ", strip=True)) or ""
    images = container.select("picture[data-field='images'] img[src]")
    if ordinary_text or len(images) != 1:
        return None
    image_url = urljoin(target, images[0].get("src"))
    image_parts = urlparse(image_url)
    if image_parts.scheme != "https" or image_parts.hostname != "images.csmonitor.com":
        return None
    if not image_parts.path.startswith("/csm/"):
        return None
    return {
        "publisher_image_url": image_url,
        "publisher_dom_selector": (
            ".story .trinity-audio-read[data-field='body'] "
            "picture[data-field='images'] img[src]"
        ),
        "body_nonpromotional_text_characters": 0,
        "publisher_body_image_count": 1,
        "structured_article_body_empty": True,
    }


def scrape_url(url: str) -> dict:
    target = normalize_url(url, {Config.source_name})
    path_day = _path_day(target) if target else None
    if not target or not path_day:
        raise RuntimeError("Christian Science Monitor URL lacks a valid publisher path date")

    response = _throttled_fetch(target)
    article, soup = extract_article(
        url=target,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=("main h1.headline", ".story h1.headline", "h1.headline"),
        body_selectors=(".story .trinity-audio-read", ".trinity-audio-read"),
        remove_selectors=(
            "#inbody_nl_widget", ".newsletter-email-signup", ".ad-login",
            ".login-prompt", "#meter-expanded-msg", ".story-feature-bar",
            "[class*='newsletter']", "[class*='subscribe']", "[class*='login']",
            "[class*='meter']", "[class*='promo']", "[class*='related']",
        ),
        parser="csmonitor_visible_headline_published_meta_trinity_story_body",
        response=response,
    )

    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = normalize_url(canonical_node.get("href"), {Config.source_name}) if canonical_node else None
    if canonical != target:
        raise RuntimeError("Christian Science Monitor canonical URL does not match the request")

    meta_node = soup.select_one('meta[property="article:published_time"][content]')
    visible_node = soup.select_one("time[datetime]")
    item = article_jsonld(soup) or {}
    meta_raw = clean(meta_node.get("content")) if meta_node else None
    visible_raw = clean(visible_node.get("datetime")) if visible_node else None
    visible_text = clean(visible_node.get_text(" ", strip=True)) if visible_node else None
    jsonld_raw = clean(item.get("datePublished"))
    modified_raw = clean(item.get("dateModified"))
    publication = _publication_identity(
        path_day=path_day,
        metadata_raw=meta_raw,
        jsonld_published_raw=jsonld_raw,
        visible_raw=visible_raw,
        jsonld_modified_raw=modified_raw,
    )
    meta = publication["metadata"]

    discovered = _DISCOVERY_ROWS.get(target)
    if discovered and discovered["path_date"] != path_day.isoformat():
        raise RuntimeError("Christian Science Monitor page date disagrees with sitemap discovery")

    visible_title = clean(article.get("title"))
    jsonld_title = clean(item.get("headline"))
    og_title = clean(first_text(soup, ('meta[property="og:title"]',))[0])
    if not visible_title or visible_title != jsonld_title:
        raise RuntimeError("Christian Science Monitor visible and JSON-LD headlines disagree")

    html_language = clean(soup.html.get("lang")) if soup.html else None
    jsonld_language = clean(item.get("inLanguage"))
    if not html_language or html_language.split("-", 1)[0].casefold() != "en":
        raise RuntimeError("Christian Science Monitor HTML language is not English")
    if jsonld_language and jsonld_language.split("-", 1)[0].casefold() != "en":
        raise RuntimeError("Christian Science Monitor JSON-LD language is not English")

    article["date_publish"] = meta.replace(tzinfo=None)
    article["language"] = "en"
    article["field_sources"]["date_publish"] = (
        "URL path day plus article:published_time and same-instant NewsArticle datePublished; "
        "visible time is role-checked against NewsArticle dateModified"
    )
    article["field_sources"]["language"] = "html[lang] cross-checked with JSON-LD when present"
    article["field_evidence"].update({
        "canonical_url": canonical,
        "path_publication_day": path_day.isoformat(),
        "sitemap_discovery": discovered,
        "visible_publication_text": visible_text,
        "visible_publication_date_raw": visible_raw,
        "metadata_publication_raw": meta_raw,
        "jsonld_publication_raw": jsonld_raw,
        "publication_signal_delta_seconds": publication["publication_signal_delta_seconds"],
        "jsonld_date_modified_raw": modified_raw,
        "visible_time_role": publication["visible_time_role"],
        "visible_time_supported_by_jsonld_date_modified": (
            publication["visible_time_role"] == "modification_day"
        ),
        "visible_title": visible_title,
        "jsonld_title": jsonld_title,
        "opengraph_title": og_title,
        "html_language": html_language,
        "jsonld_language": jsonld_language,
        "newsletter_and_login_blocks_removed": True,
        "sitemap_lastmod_used_as_publication_date": False,
        "modification_timestamp_used_as_publication_date": False,
    })
    if article.get("parser_error") == "missing:maintext" and not article.get("maintext"):
        image_only = _publisher_image_only_comic(
            soup=soup,
            target=target,
            visible_title=visible_title,
            item=item,
        )
        if image_only:
            article["field_evidence"]["publisher_images"] = [{
                "url": image_only["publisher_image_url"],
                "alt": None,
                "selector": image_only["publisher_dom_selector"],
            }]
            article["field_evidence"]["structured_article_body_empty"] = True
            article["field_evidence"]["body_nonpromotional_text_characters"] = 0
            article["collection"] = "SKIP"
            article["parser_error"] = None
            article["exclusion_reason"] = "excluded_image_only_publisher_item"
            article["exclusion_evidence"] = {
                "policy_type": "publisher_image_only",
                "publisher_item_type": "image_led_cartoon",
                **image_only,
            }
    return article
