"""Pure, commit-disabled Euronews multilingual sitemap adapter.

The adapter only reads publisher first-party robots, sitemap, and article
routes.  It has no database imports, credentials, cache writes, or production
write path.  Monthly sitemap timestamps are inventory evidence only; the
publisher article page supplies the authoritative publication date.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Iterable, Iterator, Optional
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET


class Config:
    source_name = "euronews.com"
    country_code = "REG"
    is_env = False
    access_block_circuit_threshold = 3


USER_AGENT = "ChatGPT-User"
MIN_REQUEST_INTERVAL_SECONDS = 0.55
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|checking your browser|just a moment|"
    r"verify you are human|web application firewall",
    re.I,
)
PATH_DAY = re.compile(r"^/(?:video/)?(20\d{2})/(\d{2})/(\d{2})/")
MONTH_MAP = re.compile(r"^/sitemap/articles-(20\d{2})-(\d{2})\.xml$")
ARTICLE_KINDS = {"Article", "NewsArticle", "ReportageNewsArticle"}

# This fixed inventory mirrors the languages recorded for the source in the
# historical handoff.  New locales must be reviewed before being added.
LOCALE_HOSTS = {
    "en": "www.euronews.com",
    "es": "es.euronews.com",
    "fr": "fr.euronews.com",
    "pt": "pt.euronews.com",
    "de": "de.euronews.com",
    "it": "it.euronews.com",
    "ru": "ru.euronews.com",
    "tr": "tr.euronews.com",
    "el": "gr.euronews.com",
    "hu": "hu.euronews.com",
    "ar": "arabic.euronews.com",
}
HOST_LOCALES = {host: locale for locale, host in LOCALE_HOSTS.items()}
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_ROBOTS_CACHE: dict[str, tuple["_Response", RobotFileParser]] = {}
_ROBOTS_LOCK = threading.Lock()


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
    global _LAST_REQUEST_AT
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in HOST_LOCALES:
        raise ValueError("Euronews adapter refused non-allowlisted URL: %s" % url)
    request = Request(url, headers=HEADERS, method="GET")
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
            if final.scheme != "https" or final.hostname != parsed.hostname:
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


def _robots_for(host: str) -> tuple[_Response, RobotFileParser]:
    with _ROBOTS_LOCK:
        cached = _ROBOTS_CACHE.get(host)
    if cached is not None:
        return cached
    url = "https://%s/robots.txt" % host
    response = _fetch(url, max_bytes=128 * 1024)
    parser = RobotFileParser()
    parser.set_url(url)
    parser.parse(response.content.decode("utf-8", "replace").splitlines())
    result = (response, parser)
    with _ROBOTS_LOCK:
        _ROBOTS_CACHE.setdefault(host, result)
        return _ROBOTS_CACHE[host]


def _require_allowed(robots: RobotFileParser, url: str) -> None:
    if not robots.can_fetch(USER_AGENT, url):
        raise AccessBlockedError("publisher robots policy disallows %s" % url)


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
        if values.get("loc"):
            rows.append({"url": values["loc"], "lastmod": values.get("lastmod")})
    return root_name, rows


def _path_day(url: str) -> Optional[date]:
    match = PATH_DAY.match(urlparse(url).path)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _normalize_article_url(raw: str, host: str) -> Optional[str]:
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.hostname != host or not _path_day(raw):
        return None
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def _month_keys(start: date, end: date) -> list[str]:
    if start > end:
        raise ValueError("start must be on or before end")
    cursor = date(start.year, start.month, 1)
    finish = date(end.year, end.month, 1)
    result = []
    while cursor <= finish:
        result.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    if len(result) > 3:
        raise ValueError("Euronews discovery is bounded to three whole calendar months")
    return result


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    """Discover all reviewed locale URLs for whole months intersecting the range."""
    months = _month_keys(start, end)
    selected: list[str] = []
    locale_evidence = []
    for locale, host in LOCALE_HOSTS.items():
        robots_response, robots = _robots_for(host)
        index_url = "https://%s/sitemap/articles.xml" % host
        _require_allowed(robots, index_url)
        index_response = _fetch(index_url, max_bytes=1024 * 1024)
        root_name, index_rows = _sitemap_rows(index_response.content)
        if root_name != "sitemapindex":
            raise RuntimeError("Euronews article sitemap is not an index for %s" % locale)
        maps = {}
        for row in index_rows:
            parsed = urlparse(row["url"] or "")
            match = MONTH_MAP.match(parsed.path)
            if parsed.scheme == "https" and parsed.hostname == host and match:
                maps["%s-%s" % match.groups()] = row["url"]
        missing = [month for month in months if month not in maps]
        if missing:
            raise RuntimeError("missing Euronews monthly maps for %s: %s" % (locale, ",".join(missing)))
        checked = []
        for month in months:
            map_url = maps[month]
            _require_allowed(robots, map_url)
            response = _fetch(map_url, max_bytes=8 * 1024 * 1024)
            map_root, rows = _sitemap_rows(response.content)
            if map_root != "urlset":
                raise RuntimeError("Euronews monthly sitemap is not a urlset: %s" % map_url)
            normalized = []
            selected_from_map = []
            invalid = []
            outside_named_month = []
            for row in rows:
                url = _normalize_article_url(row["url"] or "", host)
                published = _path_day(url or "")
                if not url or not published:
                    invalid.append(row["url"])
                    continue
                normalized.append(url)
                if published.strftime("%Y-%m") != month:
                    outside_named_month.append(url)
                if start <= published <= end:
                    selected_from_map.append(url)
            if invalid:
                raise RuntimeError("Euronews monthly sitemap has out-of-scope URLs: %s" % map_url)
            if len(normalized) != len(set(normalized)):
                raise RuntimeError("Euronews monthly sitemap has duplicate URLs: %s" % map_url)
            selected.extend(selected_from_map)
            checked.append({
                "month": month,
                "url": map_url,
                "entries": len(normalized),
                "selected_by_url_path_day": len(selected_from_map),
                "outside_named_month_path_days": len(outside_named_month),
                "video_entries": sum(urlparse(item).path.startswith("/video/") for item in normalized),
                "response_bytes": len(response.content),
                "response_sha256": _sha256(response.content),
            })
        locale_evidence.append({
            "locale": locale,
            "host": host,
            "robots_url": robots_response.url,
            "robots_sha256": _sha256(robots_response.content),
            "sitemap_index": index_url,
            "sitemap_index_sha256": _sha256(index_response.content),
            "months": checked,
        })
    selected = sorted(set(selected), key=lambda item: (_path_day(item), item))
    evidence.update({
        "method": "publisher_multilingual_monthly_article_sitemaps_whole_calendar_months",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "included_whole_months": months,
        "reviewed_locale_hosts": LOCALE_HOSTS,
        "locales": locale_evidence,
        "sitemap_lastmod_is_inventory_only": True,
        "article_page_date_is_authoritative": True,
        "video_paths_are_retained_for_page_level_validation": True,
        "selected_count": len(selected),
        "selected_url_set_sha256": hashlib.sha256(
            "".join("%s\n" % item for item in selected).encode("utf-8")
        ).hexdigest(),
        "coverage_complete": True,
    })
    return selected


def _iter_json_objects(value: object) -> Iterator[dict]:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            yield item
            stack.extend(child for child in item.values() if isinstance(child, (dict, list)))


class _ArticleHTMLParser(HTMLParser):
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

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
        self._body_depth: Optional[int] = None
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
        classes = set(values.get("class", "").split())
        excluded = bool(classes & {
            "c-ad", "advertising", "related", "newsletter", "share", "social",
        }) or any(token in values.get("class", "").lower() for token in (
            "related", "recommend", "newsletter", "subscribe", "advert",
        ))
        inherited_excluded = bool(self.stack and self.stack[-1]["excluded"])
        is_void = tag in self.VOID_TAGS
        self.stack.append({"tag": tag, "excluded": excluded or inherited_excluded})
        depth = len(self.stack)
        if tag == "html" and values.get("lang"):
            self.html_language = values["lang"]
        if tag == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonical = values.get("href") or self.canonical
        if tag == "meta" and (
            values.get("property", "").lower() == "article:published_time"
            or values.get("name", "").lower() == "article:published_time"
        ):
            self.meta_published = values.get("content") or self.meta_published
        if tag == "h1" and "c-article-redesign-title" in classes:
            self._h1_depth = depth
        if tag == "div" and "c-article-content" in classes and self._body_depth is None:
            self._body_depth = depth
        if self._body_depth is not None and depth > self._body_depth and tag in {"p", "h2", "h3"}:
            if not self.stack[-1]["excluded"]:
                self._block_depth = depth
                self._block_parts = []
        if tag == "time" and values.get("datetime") and any(
            "c-article-publication-date" in str(frame.get("classes", "")) for frame in self.stack
        ):
            self.visible_time = values["datetime"]
        # Publication time is unique in current fixtures; retain the first
        # datetime as a fallback while still checking it against other fields.
        if tag == "time" and values.get("datetime") and self.visible_time is None:
            self.visible_time = values["datetime"]
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._script_depth = depth
            self._script_parts = []
        self.stack[-1]["classes"] = values.get("class", "")
        if is_void:
            self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID_TAGS:
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
        if self._body_depth == depth and tag == "div":
            self._body_depth = None
        if self.stack:
            # Publisher fixtures are well formed in the relevant containers.
            self.stack.pop()


def _parse_datetime(raw: object) -> Optional[datetime]:
    value = _clean(raw)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc_instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _language(value: object) -> Optional[str]:
    cleaned = _clean(value)
    return cleaned.split("-", 1)[0].lower() if cleaned else None


def _word_set(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", value.casefold(), re.UNICODE))


def _parse_article_html(url: str, payload: bytes, *, status_code: int = 200, response_url: Optional[str] = None) -> dict:
    parsed_url = urlparse(url)
    expected_language = HOST_LOCALES.get(parsed_url.hostname or "")
    if expected_language is None or parsed_url.scheme != "https":
        raise ValueError("Euronews article host is not allowlisted")
    parser = _ArticleHTMLParser()
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
            if kinds & ARTICLE_KINDS:
                jsonld = item
                break
        if jsonld:
            break
    jsonld = jsonld or {}
    title = _clean(" ".join(parser.h1_parts))
    headline = _clean(jsonld.get("headline"))
    maintext = "\n\n".join(dict.fromkeys(parser.body_blocks)) or None
    jsonld_body = _clean(jsonld.get("articleBody"))
    html_language = _language(parser.html_language)
    jsonld_language = _language(jsonld.get("inLanguage"))
    page_date = _parse_datetime(parser.visible_time)
    meta_date = _parse_datetime(parser.meta_published)
    jsonld_date = _parse_datetime(jsonld.get("datePublished"))
    path_day = _path_day(url)
    canonical = _normalize_article_url(parser.canonical or "", parsed_url.hostname or "")
    normalized_url = _normalize_article_url(url, parsed_url.hostname or "")

    errors = []
    for name, value in (
        ("visible_h1", title), ("visible_publication_time", page_date),
        ("visible_maintext", maintext), ("jsonld_newsarticle", bool(jsonld)),
        ("jsonld_headline", headline), ("jsonld_datePublished", jsonld_date),
        ("jsonld_articleBody", jsonld_body), ("html_language", html_language),
        ("jsonld_language", jsonld_language), ("canonical", canonical),
    ):
        if not value:
            errors.append("missing_%s" % name)
    if title and headline and title != headline:
        errors.append("visible_h1_vs_jsonld_headline_disagreement")
    if canonical and normalized_url and canonical != normalized_url:
        errors.append("canonical_vs_requested_url_disagreement")
    available_dates = [value for value in (page_date, meta_date, jsonld_date) if value]
    if meta_date is None:
        errors.append("missing_meta_published_time")
    if len(available_dates) == 3 and len({_utc_instant(item) for item in available_dates}) != 1:
        errors.append("published_date_evidence_disagreement")
    authoritative_date = page_date or meta_date or jsonld_date
    if path_day and authoritative_date and authoritative_date.date() != path_day:
        errors.append("url_path_vs_page_publication_date_disagreement")
    if html_language and html_language != expected_language:
        errors.append("html_language_vs_locale_host_disagreement")
    if jsonld_language and jsonld_language != expected_language:
        errors.append("jsonld_language_vs_locale_host_disagreement")
    overlap = None
    if maintext and jsonld_body:
        visible_words = _word_set(maintext)
        jsonld_words = _word_set(jsonld_body)
        overlap = len(visible_words & jsonld_words) / max(1, len(visible_words | jsonld_words))
        if len(maintext) < 200 or len(jsonld_body) < 200 or overlap < 0.50:
            errors.append("visible_body_vs_jsonld_articleBody_insufficient_overlap")

    return {
        "url": normalized_url or url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "date_publish": authoritative_date,
        "maintext": maintext,
        "language": html_language or jsonld_language or expected_language,
        "parser": "euronews_visible_fields_cross_checked_with_newsarticle_jsonld",
        "collection": None,
        "parser_error": ";".join(dict.fromkeys(errors)) or None,
        "field_sources": {
            "title": "h1.c-article-redesign-title",
            "date_publish": "div.c-article-publication-date time[datetime]",
            "maintext": "div.c-article-content p,h2,h3",
            "language": "html[lang] cross-checked with NewsArticle.inLanguage and locale host",
        },
        "field_evidence": {
            "http_status": status_code,
            "response_url": response_url or url,
            "response_sha256": _sha256(payload),
            "response_bytes": len(payload),
            "transport": "urllib",
            "canonical": canonical,
            "titles": [
                {"kind": "visible_h1", "source": "h1.c-article-redesign-title", "raw": title},
                {"kind": "jsonld_headline", "source": "NewsArticle.headline", "raw": headline},
            ],
            "dates": [
                {"kind": "visible_published", "source": "time[datetime]", "raw": parser.visible_time},
                {"kind": "meta_published", "source": "meta[property=article:published_time]", "raw": parser.meta_published},
                {"kind": "jsonld_published", "source": "NewsArticle.datePublished", "raw": jsonld.get("datePublished")},
            ],
            "html_language": parser.html_language,
            "jsonld_language": jsonld.get("inLanguage"),
            "visible_paragraph_count": len(parser.body_blocks),
            "visible_jsonld_body_word_jaccard": overlap,
        },
    }


def scrape_url(url: str) -> dict:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in HOST_LOCALES or _normalize_article_url(url, host) != url.rstrip("/"):
        raise ValueError("Euronews adapter refused noncanonical article URL")
    robots_response, robots = _robots_for(host)
    _require_allowed(robots, url)
    response = _fetch(url, max_bytes=3 * 1024 * 1024)
    article = _parse_article_html(
        url,
        response.content,
        status_code=response.status_code,
        response_url=response.url,
    )
    article["field_evidence"]["robots_sha256"] = _sha256(robots_response.content)
    return article
