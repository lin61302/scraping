"""Pure, commit-disabled France 24 multilingual sitemap adapter.

The adapter is intentionally access-policy aware.  It checks the publisher's
``robots.txt`` with the truthful ``ChatGPT-User`` identity before reading a
sitemap or article page.  As of the 2026-10-13 inspection, France 24 explicitly
disallows that identity, so live discovery fails closed before any sitemap is
requested.  The parsing and completeness contract is kept executable through
network-free fixtures for use if the publisher later grants access or supplies
an approved feed.

There are no database imports, credentials, cache writes, output writers, or
production-write paths in this module.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Iterator, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET


class Config:
    source_name = "france24.com"
    country_code = "INT"
    is_env = False
    access_block_circuit_threshold = 1


SOURCE_ID = "mlp:INT:france24.com"
FIRST_PASS_START = date(2026, 8, 1)
FIRST_PASS_END = date(2026, 9, 30)
HOST = "www.france24.com"
LOCALES = ("en", "fr", "es", "ar")
LOCALE_LANGUAGES = {"en": "en", "fr": "fr", "es": "es", "ar": "ar"}
INDEX_URLS = {locale: "https://%s/sitemaps/%s/index.xml" % (HOST, locale) for locale in LOCALES}
USER_AGENT = "ChatGPT-User"
MIN_REQUEST_INTERVAL_SECONDS = 0.8
MAX_SITEMAPS_PER_LOCALE = 64
MAX_SITEMAP_DEPTH = 2
MAX_SITEMAP_BYTES = 12 * 1024 * 1024
MAX_ARTICLE_BYTES = 4 * 1024 * 1024
PATH_DAY = re.compile(r"/(20\d{6})-")
ARTICLE_TYPES = {"Article", "NewsArticle", "ReportageNewsArticle"}
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|checking your browser|just a moment|"
    r"verify you are human|web application firewall",
    re.I,
)

_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_ROBOTS_CACHE: Optional[tuple["_Response", RobotFileParser]] = None
_ROBOTS_LOCK = threading.Lock()
_DISCOVERY_ROWS: dict[str, dict] = {}


class AccessBlockedError(RuntimeError):
    """Publisher policy, authentication, challenge, or rate-limit blocker."""


class _Response:
    def __init__(self, requested_url: str, final_url: str, status: int, content: bytes):
        self.requested_url = requested_url
        self.url = final_url
        self.status_code = status
        self.content = content
        self.scrape_transport = "urllib"


def _clean(value: object) -> Optional[str]:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", html.unescape(str(value)).replace("\xa0", " ")).strip()
    return cleaned or None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fetch(url: str, *, max_bytes: int, timeout: int = 45) -> _Response:
    """Fetch one allowlisted first-party resource with explicit pacing."""
    global _LAST_REQUEST_AT
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != HOST:
        raise ValueError("France 24 adapter refused non-allowlisted URL: %s" % url)
    allowed_path = (
        parsed.path == "/robots.txt"
        or parsed.path.startswith("/sitemaps/")
        or any(parsed.path.startswith("/%s/" % locale) for locale in LOCALES)
    )
    if not allowed_path:
        raise ValueError("France 24 adapter refused non-reviewed path: %s" % url)
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.1",
            "Accept-Language": "en-US,en;q=0.8",
        },
        method="GET",
    )
    try:
        with _REQUEST_LOCK:
            delay = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_AT)
            if delay > 0:
                time.sleep(delay)
            try:
                stream = urlopen(request, timeout=timeout)
            finally:
                _LAST_REQUEST_AT = time.monotonic()
        with stream:
            status = int(getattr(stream, "status", 200))
            final_url = stream.geturl()
            final = urlparse(final_url)
            if final.scheme != "https" or final.hostname != HOST:
                raise AccessBlockedError("unexpected cross-host redirect for %s" % url)
            declared = stream.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise RuntimeError("response exceeds byte cap for %s" % url)
            content = stream.read(max_bytes + 1)
    except HTTPError as exc:
        if exc.code in {401, 403, 407, 429, 451}:
            raise AccessBlockedError("publisher access blocked (%s) for %s" % (exc.code, url)) from exc
        raise RuntimeError("publisher HTTP %s for %s" % (exc.code, url)) from exc
    except URLError as exc:
        raise RuntimeError("publisher request failed for %s: %s" % (url, exc.reason)) from exc
    if len(content) > max_bytes:
        raise RuntimeError("response exceeds byte cap for %s" % url)
    if status in {401, 403, 407, 429, 451}:
        raise AccessBlockedError("publisher access blocked (%s) for %s" % (status, url))
    if status != 200 or not content:
        raise RuntimeError("unexpected publisher response %s for %s" % (status, url))
    head = content[:24000].decode("utf-8", "replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    if title_match and CHALLENGE.search(_clean(title_match.group(1)) or ""):
        raise AccessBlockedError("publisher challenge page for %s" % url)
    return _Response(url, final_url, status, content)


def _robots_for() -> tuple[_Response, RobotFileParser]:
    global _ROBOTS_CACHE
    with _ROBOTS_LOCK:
        if _ROBOTS_CACHE is not None:
            return _ROBOTS_CACHE
    url = "https://%s/robots.txt" % HOST
    response = _fetch(url, max_bytes=256 * 1024)
    parser = RobotFileParser()
    parser.set_url(url)
    parser.parse(response.content.decode("utf-8", "replace").splitlines())
    with _ROBOTS_LOCK:
        if _ROBOTS_CACHE is None:
            _ROBOTS_CACHE = (response, parser)
        return _ROBOTS_CACHE


def _require_allowed(robots: RobotFileParser, url: str) -> None:
    if not robots.can_fetch(USER_AGENT, url):
        raise AccessBlockedError("publisher robots policy disallows %s for %s" % (url, USER_AGENT))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _sitemap_rows(payload: bytes) -> tuple[str, list[dict[str, Optional[str]]]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError("invalid publisher sitemap XML") from exc
    root_name = _local_name(root.tag)
    if root_name not in {"sitemapindex", "urlset"}:
        raise ValueError("unexpected publisher sitemap root: %s" % root_name)
    child_name = "sitemap" if root_name == "sitemapindex" else "url"
    rows = []
    for node in root:
        if _local_name(node.tag) != child_name:
            continue
        values = {_local_name(child.tag): _clean(child.text) for child in node}
        if not values.get("loc"):
            raise RuntimeError("France 24 sitemap row lacks loc")
        rows.append({"url": values["loc"], "lastmod": values.get("lastmod")})
    if not rows:
        raise RuntimeError("France 24 sitemap is empty")
    return root_name, rows


def _path_day(url: str) -> Optional[date]:
    match = PATH_DAY.search(urlparse(url).path)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _locale_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != HOST:
        return None
    first = parsed.path.strip("/").split("/", 1)[0]
    return first if first in LOCALES else None


def _normalize_article_url(raw: str, locale: Optional[str] = None) -> Optional[str]:
    parsed = urlparse(raw)
    detected = _locale_from_url(raw)
    if not detected or (locale and detected != locale) or not _path_day(raw):
        return None
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    return urlunparse(("https", HOST, path, "", "", ""))


def _normalize_sitemap_url(raw: str, locale: str) -> Optional[str]:
    parsed = urlparse(raw)
    prefix = "/sitemaps/%s/" % locale
    if (
        parsed.scheme != "https"
        or parsed.hostname != HOST
        or not parsed.path.startswith(prefix)
        or not parsed.path.endswith(".xml")
    ):
        return None
    path = re.sub(r"/{2,}", "/", parsed.path)
    return urlunparse(("https", HOST, path, "", "", ""))


def _whole_months(start: date, end: date) -> list[str]:
    if start > end or start.day != 1 or (end + timedelta(days=1)).day != 1:
        raise ValueError("France 24 discovery requires whole calendar months")
    months = []
    cursor = start
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    if not 1 <= len(months) <= 3:
        raise ValueError("France 24 discovery is bounded to one to three whole months")
    return months


def _walk_locale_sitemaps(locale: str, robots: RobotFileParser) -> tuple[list[dict], list[dict]]:
    index_url = INDEX_URLS[locale]
    queue = [(index_url, 0)]
    seen: set[str] = set()
    article_rows: list[dict] = []
    documents: list[dict] = []
    while queue:
        url, depth = queue.pop(0)
        if url in seen:
            raise RuntimeError("France 24 sitemap graph repeats %s" % url)
        if len(seen) >= MAX_SITEMAPS_PER_LOCALE:
            raise RuntimeError("France 24 sitemap graph exceeds per-locale safety cap")
        seen.add(url)
        _require_allowed(robots, url)
        response = _fetch(url, max_bytes=MAX_SITEMAP_BYTES)
        root_name, rows = _sitemap_rows(response.content)
        documents.append({
            "url": url,
            "depth": depth,
            "root": root_name,
            "entries": len(rows),
            "response_bytes": len(response.content),
            "response_sha256": _sha256(response.content),
        })
        if root_name == "sitemapindex":
            if depth >= MAX_SITEMAP_DEPTH:
                raise RuntimeError("France 24 sitemap graph exceeds depth cap")
            children = []
            for row in rows:
                child = _normalize_sitemap_url(row["url"] or "", locale)
                if not child:
                    raise RuntimeError("France 24 index contains an out-of-scope sitemap")
                children.append(child)
            if len(children) != len(set(children)):
                raise RuntimeError("France 24 index repeats a child sitemap")
            queue.extend((child, depth + 1) for child in children)
        else:
            for row in rows:
                normalized = _normalize_article_url(row["url"] or "", locale)
                if not normalized:
                    # Keep non-dated/non-article rows visible in completeness
                    # evidence; they are never silently promoted as articles.
                    article_rows.append({
                        "url": row["url"],
                        "locale": locale,
                        "path_day": None,
                        "sitemap_url": url,
                        "sitemap_lastmod": row.get("lastmod"),
                        "selected": False,
                    })
                    continue
                article_rows.append({
                    "url": normalized,
                    "locale": locale,
                    "path_day": _path_day(normalized),
                    "sitemap_url": url,
                    "sitemap_lastmod": row.get("lastmod"),
                    "selected": False,
                })
    return article_rows, documents


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    """Read the complete reviewed EN/FR/ES/AR sitemap graph for whole months."""
    months = _whole_months(start, end)
    robots_response, robots = _robots_for()
    # Current policy fails here before any sitemap request.
    for index_url in INDEX_URLS.values():
        _require_allowed(robots, index_url)

    previous_month = (start - timedelta(days=1)).strftime("%Y-%m")
    following_month = (end + timedelta(days=1)).strftime("%Y-%m")
    selected_rows: list[dict] = []
    locale_evidence = []
    all_rows = []
    for locale in LOCALES:
        rows, documents = _walk_locale_sitemaps(locale, robots)
        dated = [row for row in rows if row["path_day"]]
        path_month_counts = Counter(row["path_day"].strftime("%Y-%m") for row in dated)
        selected = [row for row in dated if start <= row["path_day"] <= end]
        selected_month_counts = Counter(row["path_day"].strftime("%Y-%m") for row in selected)
        missing = [month for month in months if selected_month_counts[month] < 1]
        if missing:
            raise RuntimeError("France 24 %s inventory lacks requested months: %s" % (locale, ",".join(missing)))
        if path_month_counts[previous_month] < 1 or path_month_counts[following_month] < 1:
            raise RuntimeError("France 24 %s inventory does not close both month boundaries" % locale)
        for row in selected:
            row["selected"] = True
        selected_rows.extend(selected)
        all_rows.extend(rows)
        locale_evidence.append({
            "locale": locale,
            "index_url": INDEX_URLS[locale],
            "sitemap_documents": documents,
            "sitemap_rows": len(rows),
            "dated_article_rows": len(dated),
            "undated_or_nonarticle_rows": len(rows) - len(dated),
            "selected": len(selected),
            "selected_month_counts": dict(sorted(selected_month_counts.items())),
            "boundary_previous_month": path_month_counts[previous_month],
            "boundary_following_month": path_month_counts[following_month],
        })
    urls = [row["url"] for row in selected_rows]
    if len(urls) != len(set(urls)):
        raise RuntimeError("France 24 multilingual inventory repeats canonical URLs")
    urls.sort(key=lambda item: (_path_day(item), item))
    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in selected_rows})
    evidence.update({
        "method": "complete_first_party_multilingual_sitemap_graph",
        "source_id": SOURCE_ID,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "requested_whole_months": months,
        "reviewed_locales": list(LOCALES),
        "robots_url": robots_response.url,
        "robots_sha256": _sha256(robots_response.content),
        "robots_user_agent": USER_AGENT,
        "locales": locale_evidence,
        "selected": len(urls),
        "selected_url_set_sha256": hashlib.sha256(
            "".join("%s\n" % item for item in urls).encode("utf-8")
        ).hexdigest(),
        "sitemap_path_day_is_inventory_only": True,
        "article_page_date_is_authoritative": True,
        "sitemap_lastmod_used_as_publication_date": False,
        "legacy_database_writer_executed": False,
        "coverage_complete": True,
    })
    return urls


def _iter_json_objects(value: object) -> Iterator[dict]:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            yield item
            stack.extend(child for child in item.values() if isinstance(child, (dict, list)))


class _ArticleParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, object]] = []
        self.html_language: Optional[str] = None
        self.canonical: Optional[str] = None
        self.meta_published: Optional[str] = None
        self.visible_time: Optional[str] = None
        self.h1_parts: list[str] = []
        self.body_blocks: list[str] = []
        self.jsonld_payloads: list[str] = []
        self._h1_depth: Optional[int] = None
        self._article_depth: Optional[int] = None
        self._block_depth: Optional[int] = None
        self._block_parts: list[str] = []
        self._script_depth: Optional[int] = None
        self._script_parts: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, Optional[str]]]) -> dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        values = self._attrs(attrs)
        classes = values.get("class", "").lower()
        excluded = any(token in classes for token in ("related", "recommend", "newsletter", "share", "social", "advert"))
        inherited = bool(self.stack and self.stack[-1]["excluded"])
        self.stack.append({"tag": tag, "excluded": excluded or inherited})
        depth = len(self.stack)
        if tag == "html" and values.get("lang"):
            self.html_language = values["lang"]
        if tag == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonical = values.get("href") or self.canonical
        if tag == "meta" and values.get("property", "").lower() == "article:published_time":
            self.meta_published = values.get("content") or self.meta_published
        if tag == "h1" and self._h1_depth is None:
            self._h1_depth = depth
        if tag == "article" and self._article_depth is None:
            self._article_depth = depth
        if self._article_depth is not None and depth > self._article_depth and tag in {"p", "h2", "h3"} and not self.stack[-1]["excluded"]:
            self._block_depth = depth
            self._block_parts = []
        if tag == "time" and values.get("datetime") and self.visible_time is None:
            self.visible_time = values["datetime"]
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._script_depth = depth
            self._script_parts = []
        if tag in self.VOID:
            self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._h1_depth is not None:
            self.h1_parts.append(data)
        if self._block_depth is not None and self.stack and not self.stack[-1]["excluded"]:
            self._block_parts.append(data)
        if self._script_depth is not None:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        depth = len(self.stack)
        if self._block_depth == depth and tag in {"p", "h2", "h3"}:
            value = _clean(" ".join(self._block_parts))
            if value and len(value) >= 20:
                self.body_blocks.append(value)
            self._block_depth = None
            self._block_parts = []
        if self._h1_depth == depth and tag == "h1":
            self._h1_depth = None
        if self._script_depth == depth and tag == "script":
            self.jsonld_payloads.append("".join(self._script_parts))
            self._script_depth = None
            self._script_parts = []
        if self._article_depth == depth and tag == "article":
            self._article_depth = None
        if self.stack:
            self.stack.pop()


def _parse_datetime(raw: object) -> Optional[datetime]:
    value = _clean(raw)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _language(raw: object) -> Optional[str]:
    value = _clean(raw)
    return value.split("-", 1)[0].lower() if value else None


def _word_set(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", value.casefold(), re.UNICODE))


def _parse_article_html(url: str, payload: bytes, *, status_code: int = 200, response_url: Optional[str] = None) -> dict:
    locale = _locale_from_url(url)
    normalized_url = _normalize_article_url(url, locale)
    if locale is None or normalized_url is None:
        raise ValueError("France 24 article URL is not canonical")
    expected_language = LOCALE_LANGUAGES[locale]
    parser = _ArticleParser()
    parser.feed(payload.decode("utf-8", "replace"))
    parser.close()

    jsonld = None
    for raw in parser.jsonld_payloads:
        try:
            candidate = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for item in _iter_json_objects(candidate):
            kinds = item.get("@type")
            kinds = set(kinds if isinstance(kinds, list) else [kinds])
            if kinds & ARTICLE_TYPES:
                jsonld = item
                break
        if jsonld:
            break
    jsonld = jsonld or {}
    title = _clean(" ".join(parser.h1_parts))
    headline = _clean(jsonld.get("headline"))
    maintext = "\n\n".join(dict.fromkeys(parser.body_blocks)) or None
    jsonld_body = _clean(jsonld.get("articleBody"))
    canonical = _normalize_article_url(parser.canonical or "", locale)
    page_date = _parse_datetime(parser.visible_time)
    meta_date = _parse_datetime(parser.meta_published)
    jsonld_date = _parse_datetime(jsonld.get("datePublished"))
    html_language = _language(parser.html_language)
    jsonld_language = _language(jsonld.get("inLanguage"))
    errors = []
    for name, value in (
        ("visible_h1", title), ("visible_publication_time", page_date),
        ("visible_maintext", maintext), ("newsarticle_jsonld", bool(jsonld)),
        ("jsonld_headline", headline), ("jsonld_datePublished", jsonld_date),
        ("jsonld_articleBody", jsonld_body), ("meta_published_time", meta_date),
        ("html_language", html_language), ("jsonld_language", jsonld_language),
        ("canonical", canonical),
    ):
        if not value:
            errors.append("missing_%s" % name)
    if title and headline and title != headline:
        errors.append("visible_h1_vs_jsonld_headline_disagreement")
    if canonical and canonical != normalized_url:
        errors.append("canonical_vs_requested_url_disagreement")
    dates = [value for value in (page_date, meta_date, jsonld_date) if value]
    if len(dates) == 3 and len({_utc(item) for item in dates}) != 1:
        errors.append("published_date_evidence_disagreement")
    authoritative_date = page_date or meta_date or jsonld_date
    path_day = _path_day(normalized_url)
    if path_day and authoritative_date and path_day != authoritative_date.date():
        errors.append("url_path_vs_page_publication_date_disagreement")
    if html_language and html_language != expected_language:
        errors.append("html_language_vs_locale_path_disagreement")
    if jsonld_language and jsonld_language != expected_language:
        errors.append("jsonld_language_vs_locale_path_disagreement")
    overlap = None
    if maintext and jsonld_body:
        visible_words = _word_set(maintext)
        jsonld_words = _word_set(jsonld_body)
        overlap = len(visible_words & jsonld_words) / max(1, len(visible_words | jsonld_words))
        if len(maintext) < 200 or len(jsonld_body) < 200 or overlap < 0.50:
            errors.append("visible_body_vs_jsonld_articleBody_insufficient_overlap")
    return {
        "url": normalized_url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "date_publish": authoritative_date,
        "maintext": maintext,
        "language": html_language or jsonld_language or expected_language,
        "parser": "france24_visible_article_cross_checked_with_newsarticle_jsonld",
        "collection": None,
        "parser_error": ";".join(dict.fromkeys(errors)) or None,
        "field_sources": {
            "title": "first visible h1",
            "date_publish": "first time[datetime], article:published_time, NewsArticle.datePublished",
            "maintext": "article p,h2,h3",
            "language": "html[lang], NewsArticle.inLanguage, locale path",
        },
        "field_evidence": {
            "http_status": status_code,
            "response_url": response_url or url,
            "response_sha256": _sha256(payload),
            "response_bytes": len(payload),
            "transport": "urllib",
            "canonical": canonical,
            "locale": locale,
            "expected_language": expected_language,
            "html_language": parser.html_language,
            "jsonld_language": jsonld.get("inLanguage"),
            "visible_paragraph_count": len(parser.body_blocks),
            "visible_jsonld_body_word_jaccard": overlap,
            "titles": [
                {"kind": "visible_h1", "source": "h1", "raw": title},
                {"kind": "jsonld_headline", "source": "NewsArticle.headline", "raw": headline},
            ],
            "dates": [
                {"kind": "visible_published", "source": "time[datetime]", "raw": parser.visible_time},
                {"kind": "meta_published", "source": "meta[property=article:published_time]", "raw": parser.meta_published},
                {"kind": "jsonld_published", "source": "NewsArticle.datePublished", "raw": jsonld.get("datePublished")},
            ],
        },
    }


def scrape_url(url: str) -> dict:
    locale = _locale_from_url(url)
    if locale is None or _normalize_article_url(url, locale) != url.rstrip("/"):
        raise ValueError("France 24 adapter refused noncanonical article URL")
    robots_response, robots = _robots_for()
    _require_allowed(robots, url)
    response = _fetch(url, max_bytes=MAX_ARTICLE_BYTES)
    article = _parse_article_html(url, response.content, status_code=response.status_code, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = _sha256(robots_response.content)
    discovery = _DISCOVERY_ROWS.get(article["url"])
    if discovery:
        article["field_evidence"]["discovery"] = discovery
    return article
