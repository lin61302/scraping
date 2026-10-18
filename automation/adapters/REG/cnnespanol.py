"""Pure, production-write-disabled CNN Español observation adapter.

Historical metadata identifies monthly category sitemaps below
``/sitemap/article/`` and says that both news and video rows were collected.
The local evidence does not contain a current robots response, sitemap payload,
or article page.  The adapter therefore ships with its live discovery gate
closed.  A later, separately reviewed first-party route capture must establish
the exact ``news`` and ``video`` route profile before ``discover_urls`` can be
enabled.

Sitemap membership, ``lastmod`` values, and URL path dates are candidate
inventory evidence only.  Publication dates come from the publisher article
page and must agree across at least two page fields.  The module has no
database imports, credentials, browser automation, cache writes, filesystem
writes, subprocess calls, or production mutation path.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Iterable, Optional
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests

from adapters.INT.common import article_jsonld, clean, extract_article


class Config:
    source_name = "cnnespanol.cnn.com"
    country_code = "REG"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ROUTE_PROFILE_EVIDENCE = "legacy_static_metadata_only"
FIRST_PASS_START = date(2026, 8, 1)
FIRST_PASS_END = date(2026, 9, 30)
RECORDED_NOTION_BOUNDARY = date(2026, 8, 13)
ROBOTS_URL = "https://cnnespanol.cnn.com/robots.txt"
SITEMAP_KINDS = ("news", "video")
MONTHLY_SITEMAP_TEMPLATE = (
    "https://cnnespanol.cnn.com/sitemap/article/{kind}/{year}/{month:02d}"
)
USER_AGENT = "MLP source observer/1.0 (bounded research QA)"
MIN_REQUEST_INTERVAL_SECONDS = 1.0
MAX_WINDOW_DAYS = 92
MAX_SITEMAP_BYTES = 16 * 1024 * 1024
MAX_ARTICLE_BYTES = 5 * 1024 * 1024
MEDIA_PATH = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)$", re.I)
PATH_DAY = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})(?:/|$)")
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|checking your browser|just a moment|"
    r"verify you are human|web application firewall",
    re.I,
)
ARTICLE_TYPES = {"Article", "NewsArticle", "ReportageNewsArticle"}
VIDEO_TYPES = {"VideoObject"}

_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_STARTED = 0.0
_ROBOTS_LOCK = threading.Lock()
_ROBOTS_CACHE: Optional[tuple[requests.Response, RobotFileParser]] = None
_DISCOVERY_ROWS: dict[str, dict] = {}


class AccessBlockedError(RuntimeError):
    """The publisher denied, challenged, or rate-limited the request."""


class RouteProfileUnverifiedError(RuntimeError):
    """Current first-party evidence has not approved the legacy route profile."""


class _ParsedResponse:
    """Small requests-compatible response used by the pure HTML parser."""

    def __init__(self, url: str, content: bytes, status_code: int = 200):
        self.url = url
        self.content = content
        self.status_code = status_code
        self.scrape_transport = "offline_parser"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _url_set_sha256(urls: Iterable[str]) -> str:
    payload = "".join("%s\n" % item for item in sorted(set(urls)))
    return _sha256(payload.encode("utf-8"))


def _request(url: str, *, max_bytes: int, timeout: int = 45) -> requests.Response:
    """Make one paced, same-host GET with a strict response cap and no retry."""
    global _LAST_REQUEST_STARTED
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != Config.source_name
        or parsed.username
        or parsed.password
    ):
        raise ValueError("CNN Español adapter refused a non-allowlisted URL")
    with _REQUEST_LOCK:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_STARTED)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_STARTED = time.monotonic()
    response = requests.get(
        url,
        timeout=timeout,
        allow_redirects=False,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.1",
            "Accept-Language": "es,en;q=0.5",
        },
    )
    if response.status_code in {401, 403, 407, 429, 451}:
        raise AccessBlockedError(
            "CNN Español returned HTTP %s; stop this bounded attempt" % response.status_code
        )
    if 300 <= response.status_code < 400:
        raise AccessBlockedError(
            "CNN Español returned an unreviewed redirect (%s)" % response.status_code
        )
    response.raise_for_status()
    if not response.content:
        raise RuntimeError("CNN Español returned an empty response")
    if len(response.content) > max_bytes:
        raise RuntimeError("CNN Español response exceeds the configured byte cap")
    head = response.content[:24000].decode("utf-8", "replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    if title and CHALLENGE.search(clean(title.group(1)) or ""):
        raise AccessBlockedError("CNN Español returned a challenge page")
    response.scrape_transport = "requests"
    return response


def _robots_for_source() -> tuple[requests.Response, RobotFileParser]:
    global _ROBOTS_CACHE
    with _ROBOTS_LOCK:
        if _ROBOTS_CACHE is not None:
            return _ROBOTS_CACHE
    response = _request(ROBOTS_URL, max_bytes=256 * 1024)
    parser = RobotFileParser()
    parser.set_url(ROBOTS_URL)
    parser.parse(response.content.decode("utf-8", "replace").splitlines())
    with _ROBOTS_LOCK:
        if _ROBOTS_CACHE is None:
            _ROBOTS_CACHE = (response, parser)
        return _ROBOTS_CACHE


def _require_allowed(parser: RobotFileParser, url: str) -> None:
    if not parser.can_fetch(USER_AGENT, url):
        raise AccessBlockedError("CNN Español robots policy disallows the requested route")


def _month_keys(start: date, end: date) -> list[tuple[int, int]]:
    if start > end or (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError("CNN Español window must span at most 93 inclusive days")
    cursor = date(start.year, start.month, 1)
    finish = date(end.year, end.month, 1)
    result = []
    while cursor <= finish:
        result.append((cursor.year, cursor.month))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    if len(result) > 3:
        raise ValueError("CNN Español discovery is limited to three calendar months")
    return result


def _monthly_sitemap_url(kind: str, year: int, month: int) -> str:
    if kind not in SITEMAP_KINDS or not 1 <= month <= 12:
        raise ValueError("invalid CNN Español monthly sitemap key")
    return MONTHLY_SITEMAP_TEMPLATE.format(kind=kind, year=year, month=month)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_urlset(payload: bytes) -> list[dict[str, Optional[str]]]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise RuntimeError("CNN Español returned malformed sitemap XML") from exc
    if _local_name(root.tag) != "urlset":
        raise RuntimeError("CNN Español monthly sitemap root is not urlset")
    rows = []
    for node in root:
        if _local_name(node.tag) != "url":
            continue
        values = {
            _local_name(child.tag): clean(child.text)
            for child in node
            if _local_name(child.tag) in {"loc", "lastmod"}
        }
        if not values.get("loc"):
            raise RuntimeError("CNN Español sitemap contains a row without loc")
        rows.append({"url": values["loc"], "lastmod": values.get("lastmod")})
    if not rows:
        raise RuntimeError("CNN Español monthly sitemap is empty")
    return rows


def _canonicalize_article_url(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    try:
        parsed = urlparse(raw.strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None
    path = re.sub(r"/{2,}", "/", parsed.path or "")
    forbidden = (
        "/sitemap/", "/category/", "/tag/", "/author/", "/search/",
        "/wp-admin/", "/wp-login", "/feed/",
    )
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != Config.source_name
        or port not in {None, 80, 443}
        or parsed.username
        or parsed.password
        or not path
        or path == "/"
        or path.startswith(forbidden)
        or MEDIA_PATH.search(path)
    ):
        return None
    return urlunparse(("https", Config.source_name, path.rstrip("/"), "", "", ""))


def _path_day(url: str) -> Optional[date]:
    match = PATH_DAY.search(urlparse(url).path)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _read_month_map(
    kind: str,
    year: int,
    month: int,
    robots: RobotFileParser,
) -> tuple[list[dict[str, Optional[str]]], dict]:
    url = _monthly_sitemap_url(kind, year, month)
    _require_allowed(robots, url)
    response = _request(url, max_bytes=MAX_SITEMAP_BYTES)
    rows = _parse_urlset(response.content)
    return rows, {
        "kind": kind,
        "month": "%04d-%02d" % (year, month),
        "url": url,
        "http_status": response.status_code,
        "response_bytes": len(response.content),
        "response_sha256": _sha256(response.content),
        "raw_entries": len(rows),
    }


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    """Discover reviewed monthly sitemap rows after the route gate is approved."""
    months = _month_keys(start, end)
    if not ROUTE_PROFILE_APPROVED:
        evidence.update({
            "mode": "blocked_prelaunch_no_production_writes",
            "method": "legacy_monthly_category_sitemap_profile_pending_first_party_review",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "requested_whole_months": ["%04d-%02d" % item for item in months],
            "candidate_sitemap_kinds": list(SITEMAP_KINDS),
            "candidate_template": MONTHLY_SITEMAP_TEMPLATE,
            "route_profile_evidence": ROUTE_PROFILE_EVIDENCE,
            "route_profile_approved": False,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
        raise RouteProfileUnverifiedError(
            "CNN Español live discovery is disabled until current robots, sitemap route, and category evidence are captured"
        )

    robots_response, robots = _robots_for_source()
    appearances: dict[str, list[dict]] = defaultdict(list)
    endpoints = []
    raw_entries = 0
    invalid_rows = []
    for year, month in months:
        month_key = "%04d-%02d" % (year, month)
        for kind in SITEMAP_KINDS:
            rows, receipt = _read_month_map(kind, year, month, robots)
            endpoints.append(receipt)
            raw_entries += len(rows)
            for position, row in enumerate(rows, start=1):
                url = _canonicalize_article_url(row["url"])
                if not url:
                    invalid_rows.append({
                        "sitemap": receipt["url"],
                        "position": position,
                        "raw_url": row["url"],
                    })
                    continue
                appearances[url].append({
                    "sitemap_kind": kind,
                    "sitemap_month": month_key,
                    "sitemap_position": position,
                    "sitemap_lastmod": row.get("lastmod"),
                })
    if invalid_rows:
        raise RuntimeError("CNN Español monthly sitemap contains noncanonical article rows")
    urls = sorted(appearances)
    if not urls:
        raise RuntimeError("CNN Español requested monthly inventory is empty")

    duplicate_counts = {
        url: len(rows) for url, rows in appearances.items() if len(rows) > 1
    }
    inventory_rows = []
    sitemap_kind_counts = Counter()
    sitemap_month_counts = Counter()
    for url in urls:
        seen = appearances[url]
        for row in seen:
            sitemap_kind_counts[row["sitemap_kind"]] += 1
            sitemap_month_counts[row["sitemap_month"]] += 1
        path_day = _path_day(url)
        inventory_rows.append({
            "url": url,
            "appearances": seen,
            "appearance_count": len(seen),
            "path_day": path_day.isoformat() if path_day else None,
            "path_day_authoritative": False,
            "sitemap_lastmod_authoritative": False,
        })
    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in inventory_rows})
    evidence.update({
        "mode": "pure_observation_no_production_writes",
        "method": "publisher_monthly_news_and_video_sitemaps_with_page_date_validation",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "requested_whole_months": ["%04d-%02d" % item for item in months],
        "recorded_notion_boundary": RECORDED_NOTION_BOUNDARY.isoformat(),
        "route_profile_evidence": ROUTE_PROFILE_EVIDENCE,
        "route_profile_approved": True,
        "sitemap_kinds": list(SITEMAP_KINDS),
        "sitemap_endpoints": endpoints,
        "robots_url": ROBOTS_URL,
        "robots_sha256": _sha256(robots_response.content),
        "raw_entries": raw_entries,
        "unique_urls": len(urls),
        "duplicate_canonical_urls": len(duplicate_counts),
        "duplicate_extra_entries": sum(count - 1 for count in duplicate_counts.values()),
        "duplicate_examples": [
            {"url": url, "appearances": count}
            for url, count in sorted(duplicate_counts.items())[:20]
        ],
        "sitemap_kind_raw_counts": dict(sorted(sitemap_kind_counts.items())),
        "sitemap_month_raw_counts": dict(sorted(sitemap_month_counts.items())),
        "selected_url_set_sha256": _url_set_sha256(urls),
        "inventory_rows": inventory_rows,
        "sitemap_membership_is_candidate_only": True,
        "sitemap_lastmod_used_as_publication_date": False,
        "url_path_day_used_as_publication_date": False,
        "article_page_date_is_authoritative": True,
        "coverage_complete": True,
        "first_pass_verified_through": None,
        "strict_verified_through": None,
        "legacy_database_writer_executed": False,
        "production_writes": False,
    })
    return urls


def _aware_datetime(raw: object) -> Optional[datetime]:
    value = clean(raw)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _jsonld_types(item: dict) -> set[str]:
    kinds = item.get("@type")
    if isinstance(kinds, list):
        return {str(value) for value in kinds}
    return {str(kinds)} if kinds else set()


def _word_set(value: object) -> set[str]:
    return set(re.findall(r"[^\W_]+", (clean(value) or "").casefold(), re.UNICODE))


def _parse_article_html(
    url: str,
    payload: bytes,
    *,
    status_code: int = 200,
    response_url: Optional[str] = None,
    transport: str = "offline_parser",
) -> dict:
    target = _canonicalize_article_url(url)
    if target != url.rstrip("/"):
        raise ValueError("CNN Español parser refused a noncanonical article URL")
    final_url = response_url or url
    parsed_response = _ParsedResponse(final_url, payload, status_code)
    parsed_response.scrape_transport = transport
    article, soup = extract_article(
        url=target,
        domain=Config.source_name,
        country_code=Config.country_code,
        title_selectors=(
            "h1.headline__text", "h1.pg-headline", "h1.article-title", "main h1", "h1",
        ),
        body_selectors=(
            ".article__content", ".zn-body__paragraph", ".article-body", "article .storyfull__body",
        ),
        remove_selectors=(
            ".ad", ".advertisement", ".related", ".recommended", ".newsletter",
            ".social", ".share", ".video-player", "[class*='related']",
        ),
        parser="cnnespanol_visible_fields_cross_checked_with_structured_metadata",
        response=parsed_response,
    )
    errors = []
    if article.get("parser_error"):
        errors.append(str(article["parser_error"]))

    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = _canonicalize_article_url(canonical_node.get("href")) if canonical_node else None
    final_canonical = _canonicalize_article_url(final_url)
    if canonical != target:
        errors.append("canonical_vs_requested_url_disagreement")
    if final_canonical != target:
        errors.append("response_vs_requested_url_disagreement")

    item = article_jsonld(soup) or {}
    kinds = _jsonld_types(item)
    if not kinds.intersection(ARTICLE_TYPES | VIDEO_TYPES):
        errors.append("missing_article_or_video_jsonld")
    content_kind = "video" if kinds & VIDEO_TYPES else "article"

    headline = clean(item.get("headline") or item.get("name"))
    title = clean(article.get("title"))
    if not headline:
        errors.append("missing_jsonld_headline")
    elif title and headline.casefold() != title.casefold():
        errors.append("visible_vs_jsonld_title_disagreement")

    meta_node = soup.select_one('meta[property="article:published_time"][content]')
    visible_node = soup.select_one(
        'time[datetime][data-last-publish-date], time[datetime][itemprop="datePublished"], article time[datetime]'
    )
    raw_dates = {
        "meta_article_published_time": clean(meta_node.get("content")) if meta_node else None,
        "visible_time": clean(visible_node.get("datetime")) if visible_node else None,
        "jsonld_date_published": clean(item.get("datePublished") or item.get("uploadDate")),
    }
    parsed_dates = {name: _aware_datetime(raw) for name, raw in raw_dates.items() if raw}
    invalid_date_fields = [name for name, value in parsed_dates.items() if value is None]
    if invalid_date_fields:
        errors.append("invalid_page_publication_datetime")
    valid_dates = [(name, value) for name, value in parsed_dates.items() if value is not None]
    if len(valid_dates) < 2:
        errors.append("insufficient_independent_publication_dates")
    instants = [value.astimezone(timezone.utc) for _, value in valid_dates]
    if instants and any(abs((value - instants[0]).total_seconds()) > 1 for value in instants[1:]):
        errors.append("page_publication_dates_disagree")
    authoritative = valid_dates[0][1] if valid_dates else None
    article["date_publish"] = authoritative.replace(tzinfo=None) if authoritative else None
    path_day = _path_day(target)
    if path_day and authoritative and path_day != authoritative.date():
        errors.append("url_path_vs_page_publication_date_disagreement")

    html_language = clean((soup.html or {}).get("lang") if soup.html else None)
    jsonld_language = clean(item.get("inLanguage"))
    languages = [value.split("-", 1)[0].lower() for value in (html_language, jsonld_language) if value]
    if not languages:
        errors.append("missing_spanish_language_evidence")
        article["language"] = None
    elif any(value != "es" for value in languages) or len(set(languages)) != 1:
        errors.append("unexpected_or_conflicting_language")
        article["language"] = languages[0]
    else:
        article["language"] = "es"

    visible_words = _word_set(article.get("maintext"))
    structured_body = clean(item.get("articleBody") or item.get("transcript"))
    structured_words = _word_set(structured_body)
    overlap = None
    if visible_words and structured_words:
        overlap = len(visible_words & structured_words) / len(visible_words | structured_words)
        if overlap < 0.35:
            errors.append("visible_vs_structured_body_disagreement")
    if content_kind == "video":
        errors.append("video_text_eligibility_policy_unresolved")

    article["parser_error"] = ";".join(dict.fromkeys(errors)) or None
    article["field_sources"]["date_publish"] = "two or more agreeing page publication fields"
    article["field_sources"]["language"] = "html[lang] cross-checked with JSON-LD inLanguage"
    article["field_evidence"].update({
        "canonical": canonical,
        "content_kind": content_kind,
        "jsonld_types": sorted(kinds),
        "jsonld_headline": headline,
        "publication_dates": raw_dates,
        "valid_publication_date_fields": [name for name, _ in valid_dates],
        "path_day": path_day.isoformat() if path_day else None,
        "path_day_authoritative": False,
        "html_language": html_language,
        "jsonld_language": jsonld_language,
        "visible_structured_body_word_jaccard": overlap,
        "production_writes": False,
    })
    return article


def scrape_url(url: str) -> dict:
    target = _canonicalize_article_url(url)
    if target != url.rstrip("/"):
        raise ValueError("CNN Español adapter refused a noncanonical article URL")
    robots_response, robots = _robots_for_source()
    _require_allowed(robots, target)
    response = _request(target, max_bytes=MAX_ARTICLE_BYTES)
    article = _parse_article_html(
        target,
        response.content,
        status_code=response.status_code,
        response_url=response.url,
        transport=getattr(response, "scrape_transport", "requests"),
    )
    article["field_evidence"]["robots_sha256"] = _sha256(robots_response.content)
    discovered = _DISCOVERY_ROWS.get(target)
    if discovered:
        article["field_evidence"]["sitemap_appearances"] = discovered["appearances"]
    return article
