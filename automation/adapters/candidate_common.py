"""Pure helpers for source adapters that still require live route review.

This module deliberately contains no database, filesystem-write, browser, or
subprocess surface.  It supports bounded HTTP observation, robots checks,
strict sitemap/category inventory parsing, and publisher-page field checks.
Candidate adapters must keep their live gates closed until current first-party
route, article, robots, and publisher-policy evidence has been reviewed.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from adapters.INT.common import article_jsonld, clean, extract_article


USER_AGENT = "MLP source observer/1.0 (bounded research QA)"
MIN_REQUEST_INTERVAL_SECONDS = 1.0
MAX_WINDOW_DAYS = 365
MAX_SITEMAP_BYTES = 16 * 1024 * 1024
MAX_LISTING_BYTES = 4 * 1024 * 1024
MAX_ARTICLE_BYTES = 5 * 1024 * 1024
MEDIA_PATH = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)$", re.I)
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|checking your browser|just a moment|"
    r"verify you are human|web application firewall",
    re.I,
)
ARTICLE_TYPES = {"Article", "NewsArticle", "ReportageNewsArticle"}

_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_STARTED = 0.0


class AccessBlockedError(RuntimeError):
    """The publisher denied, challenged, or disallowed a bounded request."""


class RouteProfileUnverifiedError(RuntimeError):
    """Current evidence has not approved the candidate live route profile."""


class ArticleProfileUnverifiedError(RuntimeError):
    """Current evidence has not approved the candidate article field profile."""


class _ParsedResponse:
    def __init__(self, url: str, content: bytes, status_code: int = 200):
        self.url = url
        self.content = content
        self.status_code = status_code
        self.scrape_transport = "offline_parser"


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def url_set_sha256(urls: Iterable[str]) -> str:
    payload = "".join("%s\n" % item for item in sorted(set(urls)))
    return sha256(payload.encode("utf-8"))


def _host_allowed(url: str, allowed_hosts: Iterable[str]) -> bool:
    parsed = urlparse(url)
    allowed = {item.casefold() for item in allowed_hosts}
    try:
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() in allowed
        and not parsed.username
        and not parsed.password
        and port in {None, 443}
    )


def bounded_request(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    max_bytes: int,
    timeout: int = 45,
) -> requests.Response:
    """Perform one paced, no-retry, no-redirect request with strict caps."""
    global _LAST_REQUEST_STARTED
    if not _host_allowed(url, allowed_hosts):
        raise ValueError("candidate adapter refused a non-allowlisted URL")
    with _REQUEST_LOCK:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_STARTED)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_STARTED = time.monotonic()
    response = requests.get(
        url,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.1",
        },
    )
    try:
        if response.status_code in {401, 403, 407, 429, 451}:
            raise AccessBlockedError("publisher returned HTTP %s" % response.status_code)
        if 300 <= response.status_code < 400:
            raise AccessBlockedError("publisher returned an unreviewed redirect")
        response.raise_for_status()
        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                if int(raw_length) > max_bytes:
                    raise RuntimeError("publisher response exceeds the configured byte cap")
            except ValueError:
                raise RuntimeError("publisher returned an invalid Content-Length")
        chunks: list[bytes] = []
        observed = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            observed += len(chunk)
            if observed > max_bytes:
                raise RuntimeError("publisher response exceeds the configured byte cap")
            chunks.append(chunk)
        payload = b"".join(chunks)
        if not payload:
            raise RuntimeError("publisher returned an empty response")
        # Preserve the existing Response contract for adapter parsers after the
        # bounded streaming read has closed the connection.
        response._content = payload
        response._content_consumed = True
        head = payload[:24000].decode("utf-8", "replace")
        title = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
        if title and CHALLENGE.search(clean(title.group(1)) or ""):
            raise AccessBlockedError("publisher returned a challenge page")
        response.scrape_transport = "requests_stream_bounded"
        return response
    finally:
        response.close()


def robots_for(
    robots_url: str,
    fetch: Callable[..., requests.Response],
) -> tuple[requests.Response, RobotFileParser]:
    response = fetch(robots_url, max_bytes=256 * 1024)
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.content.decode("utf-8", "replace").splitlines())
    return response, parser


def require_allowed(robots: RobotFileParser, url: str) -> None:
    if not robots.can_fetch(USER_AGENT, url):
        raise AccessBlockedError("publisher robots policy disallows the requested route")


def canonicalize_url(
    raw: object,
    *,
    allowed_hosts: Iterable[str],
    canonical_host: str,
    deny_paths: Iterable[str] = (),
) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    try:
        parsed = urlparse(raw.strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None
    path = re.sub(r"/{2,}", "/", parsed.path or "")
    folded = path.casefold()
    if (
        parsed.scheme not in {"http", "https"}
        or (parsed.hostname or "").casefold() not in {h.casefold() for h in allowed_hosts}
        or port not in {None, 80, 443}
        or parsed.username
        or parsed.password
        or not path
        or path == "/"
        or MEDIA_PATH.search(path)
        or any(folded.startswith(value.casefold()) for value in deny_paths)
    ):
        return None
    return urlunparse(("https", canonical_host, path.rstrip("/"), "", "", ""))


def parse_datetime(raw: object) -> Optional[datetime]:
    value = clean(raw)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(payload: bytes) -> tuple[str, list[dict[str, Optional[str]]]]:
    if payload.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
                payload = stream.read(MAX_SITEMAP_BYTES + 1)
        except (OSError, EOFError) as exc:
            raise RuntimeError("publisher returned malformed gzip sitemap") from exc
        if len(payload) > MAX_SITEMAP_BYTES:
            raise RuntimeError("publisher gzip sitemap exceeds the decompressed byte cap")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise RuntimeError("publisher returned malformed sitemap XML") from exc
    kind = _local_name(root.tag)
    if kind not in {"urlset", "sitemapindex"}:
        raise RuntimeError("publisher sitemap root is neither urlset nor sitemapindex")
    item_name = "url" if kind == "urlset" else "sitemap"
    rows = []
    for node in root:
        if _local_name(node.tag) != item_name:
            continue
        values = {
            _local_name(child.tag): clean(child.text)
            for child in node
            if _local_name(child.tag) in {"loc", "lastmod"}
        }
        if not values.get("loc"):
            raise RuntimeError("publisher sitemap contains a row without loc")
        rows.append({"url": values["loc"], "lastmod": values.get("lastmod")})
    if not rows:
        raise RuntimeError("publisher sitemap is empty")
    return kind, rows


def discover_sitemap_window(
    *,
    source_name: str,
    start: date,
    end: date,
    evidence: dict,
    robots_url: str,
    sitemap_urls: Iterable[str],
    allowed_hosts: Iterable[str],
    canonical_host: str,
    fetch: Callable[..., requests.Response],
    deny_paths: Iterable[str] = (),
) -> list[str]:
    if start > end or (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError("candidate source window is invalid or exceeds 366 inclusive days")
    sitemap_urls = tuple(sitemap_urls)
    if not sitemap_urls or len(sitemap_urls) > 8:
        raise ValueError("candidate source requires one to eight reviewed sitemap endpoints")
    robots_response, robots = robots_for(robots_url, fetch)
    receipts = []
    appearances: dict[str, list[dict]] = {}
    all_dates: list[date] = []
    raw_entries = 0
    for sitemap_url in sitemap_urls:
        require_allowed(robots, sitemap_url)
        response = fetch(sitemap_url, max_bytes=MAX_SITEMAP_BYTES)
        kind, rows = parse_sitemap(response.content)
        if kind != "urlset":
            raise RuntimeError("reviewed candidate sitemap endpoint is not a urlset")
        receipts.append({
            "url": sitemap_url,
            "status": response.status_code,
            "bytes": len(response.content),
            "sha256": sha256(response.content),
            "rows": len(rows),
        })
        raw_entries += len(rows)
        for position, row in enumerate(rows, start=1):
            normalized = canonicalize_url(
                row["url"],
                allowed_hosts=allowed_hosts,
                canonical_host=canonical_host,
                deny_paths=deny_paths,
            )
            parsed = parse_datetime(row.get("lastmod"))
            if normalized is None or parsed is None:
                raise RuntimeError("candidate sitemap has a noncanonical URL or missing/invalid lastmod")
            modified = parsed.date()
            all_dates.append(modified)
            appearances.setdefault(normalized, []).append({
                "sitemap": sitemap_url,
                "position": position,
                "lastmod": row["lastmod"],
            })
    lower_boundary = min(all_dates)
    upper_boundary = max(all_dates)
    if lower_boundary >= start:
        raise RuntimeError("candidate sitemap inventory does not cross the lower boundary")
    if upper_boundary < end:
        raise RuntimeError("candidate sitemap inventory does not reach the target end")
    pad = start - timedelta(days=2)
    urls = sorted(
        url for url, rows in appearances.items()
        if any((parse_datetime(row["lastmod"]) or datetime.min).date() >= pad for row in rows)
    )
    if not urls:
        raise RuntimeError("candidate sitemap inventory selected no URLs")
    evidence.update({
        "mode": "pure_observation_no_production_writes",
        "method": "reviewed_explicit_sitemap_shards_with_article_page_date_validation",
        "source": source_name,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "sitemap_endpoints": receipts,
        "robots_url": robots_url,
        "robots_sha256": sha256(robots_response.content),
        "raw_entries": raw_entries,
        "unique_entries": len(appearances),
        "selected": len(urls),
        "selected_url_set_sha256": url_set_sha256(urls),
        "inventory_lower_boundary": lower_boundary.isoformat(),
        "inventory_upper_boundary": upper_boundary.isoformat(),
        "sitemap_lastmod_used_as_publication_date": False,
        "article_page_date_is_authoritative": True,
        "out_of_acceptance_window_rows_are_audit_only": True,
        "coverage_complete": True,
        "first_pass_verified_through": None,
        "strict_verified_through": None,
        "production_writes": False,
    })
    return urls


def discover_category_window(
    *,
    source_name: str,
    start: date,
    end: date,
    evidence: dict,
    robots_url: str,
    listing_template: str,
    allowed_hosts: Iterable[str],
    canonical_host: str,
    fetch: Callable[..., requests.Response],
    item_selector: str,
    link_selector: str,
    date_selectors: Iterable[str],
    max_pages: int = 120,
) -> list[str]:
    if start > end or (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError("candidate source window is invalid or exceeds 366 inclusive days")
    robots_response, robots = robots_for(robots_url, fetch)
    selected: dict[str, date] = {}
    page_receipts = []
    lower_boundary_reached = False
    newest: Optional[date] = None
    previous_oldest: Optional[date] = None
    for page in range(1, max_pages + 1):
        url = listing_template.format(page=page)
        require_allowed(robots, url)
        response = fetch(url, max_bytes=MAX_LISTING_BYTES)
        soup = BeautifulSoup(response.content, "html.parser")
        rows = []
        for item in soup.select(item_selector):
            link = item.select_one(link_selector)
            normalized = canonicalize_url(
                link.get("href") if link else None,
                allowed_hosts=allowed_hosts,
                canonical_host=canonical_host,
            )
            raw_date = None
            for selector in date_selectors:
                node = item.select_one(selector)
                if node:
                    raw_date = clean(node.get("datetime") or node.get("content") or node.get_text(" ", strip=True))
                    if raw_date:
                        break
            parsed = parse_datetime(raw_date)
            if normalized is None or parsed is None:
                raise RuntimeError("candidate category row lacks canonical URL or ISO publication date")
            rows.append((normalized, parsed.date()))
        if not rows:
            raise RuntimeError("candidate category ended before crossing the lower boundary")
        dates = [value for _, value in rows]
        if dates != sorted(dates, reverse=True):
            raise RuntimeError("candidate category page is not newest-first")
        if previous_oldest is not None and max(dates) > previous_oldest:
            raise RuntimeError("candidate category pagination is not monotonically newest-first")
        previous_oldest = min(dates)
        newest = max(dates) if newest is None else max(newest, max(dates))
        page_receipts.append({
            "page": page,
            "url": url,
            "status": response.status_code,
            "bytes": len(response.content),
            "sha256": sha256(response.content),
            "rows": len(rows),
            "newest": max(dates).isoformat(),
            "oldest": min(dates).isoformat(),
        })
        for article_url, published in rows:
            if published >= start - timedelta(days=2):
                selected[article_url] = published
        if min(dates) < start:
            lower_boundary_reached = True
            break
    if not lower_boundary_reached:
        raise RuntimeError("candidate category did not cross the lower boundary within the page cap")
    if newest is None or newest < end:
        raise RuntimeError("candidate category did not reach the target end")
    urls = sorted(selected)
    if not urls:
        raise RuntimeError("candidate category selected no URLs")
    evidence.update({
        "mode": "pure_observation_no_production_writes",
        "method": "reviewed_newest_first_category_with_article_page_date_validation",
        "source": source_name,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "listing_template": listing_template,
        "pages": page_receipts,
        "robots_url": robots_url,
        "robots_sha256": sha256(robots_response.content),
        "selected": len(urls),
        "selected_url_set_sha256": url_set_sha256(urls),
        "listing_date_used_as_publication_date": False,
        "article_page_date_is_authoritative": True,
        "out_of_acceptance_window_rows_are_audit_only": True,
        "coverage_complete": True,
        "first_pass_verified_through": None,
        "strict_verified_through": None,
        "production_writes": False,
    })
    return urls


def _jsonld_types(item: dict) -> set[str]:
    value = item.get("@type")
    if isinstance(value, list):
        return {str(item) for item in value}
    return {str(value)} if value else set()


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _word_set(value: object) -> set[str]:
    return set(re.findall(r"[^\W_]+", (clean(value) or "").casefold(), re.UNICODE))


def parse_article_html(
    *,
    url: str,
    payload: bytes,
    source_name: str,
    country_code: str,
    allowed_hosts: Iterable[str],
    canonical_host: str,
    expected_languages: Iterable[str],
    title_selectors: Iterable[str],
    body_selectors: Iterable[str],
    remove_selectors: Iterable[str] = (),
    structured_article_required: bool = True,
    response_url: Optional[str] = None,
    status_code: int = 200,
    transport: str = "offline_parser",
) -> dict:
    target = canonicalize_url(url, allowed_hosts=allowed_hosts, canonical_host=canonical_host)
    if target != url.rstrip("/"):
        raise ValueError("candidate parser refused a noncanonical article URL")
    final_url = response_url or url
    parsed_response = _ParsedResponse(final_url, payload, status_code)
    parsed_response.scrape_transport = transport
    article, soup = extract_article(
        url=target,
        domain=source_name,
        country_code=country_code,
        title_selectors=title_selectors,
        body_selectors=body_selectors,
        remove_selectors=remove_selectors,
        parser="candidate_visible_fields_cross_checked_with_structured_metadata",
        response=parsed_response,
    )
    errors = []
    if article.get("parser_error"):
        errors.append(str(article["parser_error"]))
    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = canonicalize_url(
        canonical_node.get("href") if canonical_node else None,
        allowed_hosts=allowed_hosts,
        canonical_host=canonical_host,
    )
    final_canonical = canonicalize_url(
        final_url, allowed_hosts=allowed_hosts, canonical_host=canonical_host,
    )
    if canonical != target:
        errors.append("canonical_vs_requested_url_disagreement")
    if final_canonical != target:
        errors.append("response_vs_requested_url_disagreement")
    item = article_jsonld(soup) or {}
    kinds = _jsonld_types(item)
    if structured_article_required and not kinds.intersection(ARTICLE_TYPES):
        errors.append("missing_article_jsonld")
    headline = clean(item.get("headline") or item.get("name"))
    title = clean(article.get("title"))
    og_node = soup.select_one('meta[property="og:title"][content]')
    og_title = clean(og_node.get("content")) if og_node else None
    if structured_article_required and not headline:
        errors.append("missing_jsonld_headline")
    elif headline and title and headline.casefold() != title.casefold():
        errors.append("visible_vs_jsonld_title_disagreement")
    if not structured_article_required and not headline:
        if not og_title:
            errors.append("missing_og_title_fallback")
        elif title and not (
            og_title.casefold() == title.casefold()
            or og_title.casefold().startswith(title.casefold() + " - ")
        ):
            errors.append("visible_vs_og_title_disagreement")
    meta_node = soup.select_one('meta[property="article:published_time"][content]')
    visible_node = soup.select_one('time[datetime][itemprop="datePublished"], article time[datetime]')
    raw_dates = {
        "meta_article_published_time": clean(meta_node.get("content")) if meta_node else None,
        "visible_time": clean(visible_node.get("datetime")) if visible_node else None,
        "jsonld_date_published": clean(item.get("datePublished")),
    }
    parsed_dates = {name: parse_datetime(raw) for name, raw in raw_dates.items() if raw}
    if any(value is None for value in parsed_dates.values()):
        errors.append("invalid_page_publication_datetime")
    valid_dates = [(name, value) for name, value in parsed_dates.items() if value is not None]
    if len(valid_dates) < 2:
        errors.append("insufficient_independent_publication_dates")
    instants = [_instant(value) for _, value in valid_dates]
    if instants and any(abs((value - instants[0]).total_seconds()) > 1 for value in instants[1:]):
        errors.append("page_publication_dates_disagree")
    authoritative = valid_dates[0][1] if valid_dates else None
    article["date_publish"] = _instant(authoritative).replace(tzinfo=None) if authoritative else None
    html_language = clean((soup.html or {}).get("lang") if soup.html else None)
    jsonld_language = clean(item.get("inLanguage"))
    languages = [value.split("-", 1)[0].lower() for value in (html_language, jsonld_language) if value]
    expected = {item.lower() for item in expected_languages}
    if not languages:
        errors.append("missing_language_evidence")
        article["language"] = None
    elif any(value not in expected for value in languages) or len(set(languages)) != 1:
        errors.append("unexpected_or_conflicting_language")
        article["language"] = languages[0]
    else:
        article["language"] = languages[0]
    visible_words = _word_set(article.get("maintext"))
    structured_body = clean(item.get("articleBody"))
    structured_words = _word_set(structured_body)
    overlap = None
    if visible_words and structured_words:
        overlap = len(visible_words & structured_words) / len(visible_words | structured_words)
        if overlap < 0.35:
            errors.append("visible_vs_structured_body_disagreement")
    if len(clean(article.get("maintext")) or "") < 120:
        errors.append("insufficient_visible_maintext")
    article["parser_error"] = ";".join(dict.fromkeys(errors)) or None
    article["field_sources"]["date_publish"] = "two or more agreeing page publication fields"
    article["field_sources"]["language"] = (
        "html[lang] cross-checked with JSON-LD inLanguage"
        if jsonld_language else "html[lang]"
    )
    article["field_evidence"].update({
        "canonical": canonical,
        "jsonld_types": sorted(kinds),
        "jsonld_headline": headline,
        "og_title": og_title,
        "structured_article_required": structured_article_required,
        "publication_dates": raw_dates,
        "valid_publication_date_fields": [name for name, _ in valid_dates],
        "html_language": html_language,
        "jsonld_language": jsonld_language,
        "visible_structured_body_word_jaccard": overlap,
        "production_writes": False,
    })
    return article
