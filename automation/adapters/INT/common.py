"""Network-only helpers for international and regional source observers.

This module has no database imports, credentials, cache writes, or production
write path.  Sitemap timestamps are retained as inventory evidence only;
publication dates always come from the publisher article page.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import date, datetime
from typing import Iterable, Iterator, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MEDIA = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)(?:\?|$)", re.I)
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|checking your browser|just a moment|"
    r"verify you are human|web application firewall",
    re.I,
)
_LOCAL = threading.local()


class AccessBlockedError(RuntimeError):
    """The publisher returned an access denial or challenge page."""


def clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    return result or None


def _session() -> requests.Session:
    session = getattr(_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _LOCAL.session = session
    return session


def fetch(url: str, timeout: int = 45) -> requests.Response:
    errors = []
    for attempt in range(3):
        try:
            response = _session().get(url, timeout=timeout)
            response.raise_for_status()
            if not response.content:
                raise ValueError("empty response")
            match = re.search(r"<title[^>]*>(.*?)</title>", response.text[:18000], re.I | re.S)
            title = clean(match.group(1)) if match else None
            if title and CHALLENGE.search(title):
                raise AccessBlockedError("challenge response: %s" % title[:120])
            response.scrape_transport = "requests"
            return response
        except (requests.RequestException, ValueError, AccessBlockedError) as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
            if attempt < 2:
                time.sleep(1 + attempt)
    if any("403" in item or "429" in item or "AccessBlockedError" in item for item in errors):
        raise AccessBlockedError("access_blocked for %s: %s" % (url, "; ".join(errors)))
    raise RuntimeError("fetch failed for %s: %s" % (url, "; ".join(errors)))


def response_sha256(response: requests.Response) -> str:
    return hashlib.sha256(response.content).hexdigest()


def normalize_url(raw: str, domains: Iterable[str]) -> Optional[str]:
    allowed = {item.lower() for item in domains}
    try:
        base = sorted(allowed)[0]
        parsed = urlparse(urljoin("https://%s/" % base, raw))
        host = (parsed.hostname or "").lower()
        expanded = allowed | {"www.%s" % item for item in allowed}
        if host not in expanded or not parsed.path or parsed.path == "/" or MEDIA.search(parsed.path):
            return None
        path = re.sub(r"/{2,}", "/", parsed.path)
        return urlunparse(("https", parsed.netloc.lower(), path, "", "", ""))
    except Exception:
        return None


def parse_datetime(raw: object) -> Optional[datetime]:
    value = clean(raw)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            import dateparser
            parsed = dateparser.parse(
                value,
                languages=["en"],
                settings={"DATE_ORDER": "DMY", "PREFER_LOCALE_DATE_ORDER": False},
            )
        except Exception:
            parsed = None
    return parsed.replace(tzinfo=None) if parsed and parsed.tzinfo else parsed


def xml_response(url: str) -> tuple[requests.Response, BeautifulSoup]:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "xml")
    root = soup.find()
    if root is None or root.name not in {"sitemapindex", "urlset"}:
        raise ValueError("not sitemap XML: %s; root=%s" % (url, getattr(root, "name", None)))
    return response, soup


def sitemap_rows(soup: BeautifulSoup) -> list[dict]:
    nodes = soup.find_all("sitemap") if soup.find("sitemapindex") else soup.find_all("url")
    rows = []
    for node in nodes:
        loc = node.find("loc", recursive=False)
        if not loc:
            continue
        lastmod = node.find("lastmod", recursive=False)
        rows.append({
            "url": loc.get_text(strip=True),
            "lastmod": clean(lastmod.get_text(strip=True)) if lastmod else None,
        })
    return rows


def jsonld_objects(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(tag.string or tag.get_text() or "")
        except (TypeError, ValueError):
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, dict):
                yield value
                stack.extend(item for item in value.values() if isinstance(item, (dict, list)))


def article_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    video = None
    for item in jsonld_objects(soup):
        kinds = item.get("@type")
        kinds = set(kinds if isinstance(kinds, list) else [kinds])
        if kinds & {"Article", "NewsArticle", "ReportageNewsArticle"}:
            return item
        if video is None and "VideoObject" in kinds:
            video = item
    return video


def first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = clean(node.get("content") or node.get_text(" ", strip=True))
            if value:
                return value, selector
    return None, None


def published_datetime(soup: BeautifulSoup) -> tuple[Optional[datetime], Optional[str], Optional[str]]:
    for selector in (
        'meta[property="article:published_time"]',
        'meta[itemprop="datePublished"]',
        'meta[name="datePublished"]',
        'time[itemprop="datePublished"]',
    ):
        node = soup.select_one(selector)
        if node:
            raw = clean(node.get("content") or node.get("datetime") or node.get_text(" ", strip=True))
            parsed = parse_datetime(raw)
            if parsed:
                return parsed, selector, raw
    item = article_jsonld(soup) or {}
    raw = clean(item.get("datePublished") or item.get("uploadDate"))
    parsed = parse_datetime(raw)
    if parsed:
        return parsed, "jsonld.datePublished", raw
    return None, None, None


def article_body(
    soup: BeautifulSoup,
    selectors: Iterable[str],
    remove_selectors: Iterable[str] = (),
) -> tuple[Optional[str], Optional[str], int]:
    generic_remove = (
        "script", "style", "iframe", "form", "nav", "aside", "footer", "figure", "figcaption",
        ".ad", ".ads", ".advertisement", ".related", ".share", ".social", ".tags", ".newsletter",
        "[class*='related']", "[class*='recommend']", "[class*='newsletter']", "[class*='subscribe']",
    )
    for selector in selectors:
        original = soup.select_one(selector)
        if not original:
            continue
        node = BeautifulSoup(str(original), "html.parser")
        for bad_selector in (*generic_remove, *remove_selectors):
            for bad in node.select(bad_selector):
                bad.decompose()
        parts = []
        for item in node.select("p, h2, h3"):
            value = clean(item.get_text(" ", strip=True))
            if not value or len(value) < 20:
                continue
            if any(value in previous for previous in parts):
                continue
            parts = [previous for previous in parts if previous not in value]
            if value not in parts:
                parts.append(value)
        if parts:
            return "\n\n".join(parts), selector, len(parts)
    return None, None, 0


def extract_article(
    *,
    url: str,
    domain: str,
    country_code: str,
    title_selectors: Iterable[str],
    body_selectors: Iterable[str],
    remove_selectors: Iterable[str] = (),
    parser: str,
    response: Optional[requests.Response] = None,
) -> tuple[dict, BeautifulSoup]:
    response = response or fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_selector = first_text(soup, title_selectors)
    body, body_selector, paragraph_count = article_body(soup, body_selectors, remove_selectors)
    published, date_selector, date_raw = published_datetime(soup)
    item = article_jsonld(soup) or {}
    titles = []
    for kind, raw, source in (
        ("visible_h1", title, title_selector),
        ("og_title", first_text(soup, ('meta[property="og:title"]',))[0], 'meta[property="og:title"]'),
        ("jsonld_headline", clean(item.get("headline") or item.get("name")), "jsonld.headline_or_name"),
    ):
        if raw and all(row["raw"] != raw for row in titles):
            titles.append({"kind": kind, "source": source, "raw": raw})
    dates = []
    for kind, raw, source in (
        ("published", date_raw, date_selector),
        ("published", clean(item.get("datePublished") or item.get("uploadDate")), "jsonld.datePublished_or_uploadDate"),
    ):
        if raw and all(row["raw"] != raw for row in dates):
            dates.append({"kind": kind, "source": source, "raw": raw})
    bodies = []
    if body:
        bodies.append({"kind": "visible_body_container", "source": body_selector, "text": body[:20000]})
    language = clean((soup.html or {}).get("lang") if soup.html else None) or clean(item.get("inLanguage")) or "en"
    language = language.split("-", 1)[0].lower()
    missing = [name for name, value in (("title", title), ("date_publish", published), ("maintext", body)) if not value]
    article = {
        "url": url,
        "source_domain": domain,
        "country_code": country_code,
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": language,
        "parser": parser,
        "collection": None,
        "parser_error": "missing:%s" % ",".join(missing) if missing else None,
        "field_sources": {
            "title": title_selector,
            "date_publish": date_selector,
            "maintext": body_selector,
            "language": "html[lang] or JSON-LD inLanguage",
        },
        "field_evidence": {
            "http_status": response.status_code,
            "response_url": response.url,
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
            "transport": getattr(response, "scrape_transport", "requests"),
            "titles": titles,
            "dates": dates,
            "bodies": bodies,
            "paragraph_count": paragraph_count,
            "html_language": clean((soup.html or {}).get("lang") if soup.html else None),
        },
    }
    return article, soup


def url_set_sha256(urls: Iterable[str]) -> str:
    payload = "".join("%s\n" % item for item in sorted(set(urls)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
