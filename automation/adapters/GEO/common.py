"""Network-only helpers for Georgian observation adapters.

This module has no database dependency, persistent cache, or production write
path. Publication dates always come from article-page publication signals;
sitemap ``lastmod`` values are used only to choose discovery candidates.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from typing import Iterable, Iterator, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import dateparser
import requests
from bs4 import BeautifulSoup


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MLP-MLEED-observation/1.0)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
MEDIA = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)(?:\?|$)", re.I)
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|just a moment|verify you are human|web application firewall",
    re.I,
)


def fetch(url: str, timeout: int = 30) -> requests.Response:
    """Fetch one page with bounded retries and reject empty/error responses."""
    errors = []
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=timeout)
            response.raise_for_status()
            if not response.content:
                raise ValueError("empty response")
            return response
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"fetch failed for {url}: {'; '.join(errors)}")


def clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result or None


def normalize_url(raw: str, domain: str, deny: Iterable[str] = ()) -> Optional[str]:
    try:
        parsed = urlparse(urljoin(f"https://{domain}/", raw))
        host = (parsed.hostname or "").lower()
        if host not in {domain, f"www.{domain}"}:
            return None
        if not parsed.path or parsed.path == "/" or MEDIA.search(parsed.path):
            return None
        normalized = urlunparse(("https", parsed.netloc.lower(), parsed.path.rstrip("/") + "/", "", "", ""))
        if any(part.casefold() in normalized.casefold() for part in deny):
            return None
        return normalized
    except Exception:
        return None


def parse_datetime(raw: object, *, day_first: bool = False) -> Optional[datetime]:
    """Parse a publisher timestamp while preserving its local calendar time."""
    if not raw:
        return None
    value = clean(raw)
    if not value:
        return None
    # ISO timestamps always begin with the year; forcing DMY makes dateparser
    # reject otherwise valid values such as 2026-09-16T15:27:00+0400.
    order = "YMD" if re.match(r"^\s*20\d{2}[-/.]", value) else "DMY" if day_first else "YMD"
    settings = {
        "DATE_ORDER": order,
        "PREFER_LOCALE_DATE_ORDER": False,
        "RETURN_AS_TIMEZONE_AWARE": False,
    }
    parsed = dateparser.parse(value, settings=settings)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=None)


def xml_document(url: str) -> BeautifulSoup:
    soup = BeautifulSoup(fetch(url).content, "xml")
    root = soup.find()
    if root is None or root.name not in {"sitemapindex", "urlset"}:
        raise ValueError(f"not a sitemap XML document: {url}; root={getattr(root, 'name', None)}")
    return soup


def sitemap_entries(soup: BeautifulSoup) -> list[tuple[str, Optional[date]]]:
    nodes = soup.find_all("sitemap") if soup.find("sitemapindex") else soup.find_all("url")
    entries = []
    for node in nodes:
        loc = node.find("loc", recursive=False)
        if loc is None:
            continue
        lastmod = node.find("lastmod", recursive=False)
        modified = parse_datetime(lastmod.get_text(strip=True)).date() if lastmod else None
        entries.append((loc.get_text(strip=True), modified))
    return entries


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
                for key in ("@graph", "mainEntity"):
                    nested = value.get(key)
                    if isinstance(nested, (dict, list)):
                        stack.append(nested)


def published_signal(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    """Return an article publication timestamp, never a modification signal."""
    for selector in (
        'meta[property="article:published_time"][content]',
        'meta[itemprop="datePublished"][content]',
    ):
        node = soup.select_one(selector)
        value = clean(node.get("content")) if node else None
        if value:
            return value, selector
    for item in jsonld_objects(soup):
        value = clean(item.get("datePublished"))
        if value:
            return value, "jsonld.datePublished"
    for selector in ('time.entry-date[datetime]', 'time.published[datetime]', 'time[datetime]'):
        node = soup.select_one(selector)
        value = clean(node.get("datetime") or node.get_text(" ", strip=True)) if node else None
        if value:
            return value, selector
    return None, None


def first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        value = clean(node.get("content") or node.get_text(" ", strip=True))
        if value:
            return value, selector
    return None, None


def article_body(soup: BeautifulSoup, selectors: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    for selector in selectors:
        original = soup.select_one(selector)
        if not original:
            continue
        node = BeautifulSoup(str(original), "html.parser")
        for bad in node.select(
            "script, style, iframe, form, nav, aside, footer, .ad, .advertisement, "
            ".related, .share, .social, .tags, .tag, .jeg_post_tags"
        ):
            bad.decompose()
        parts = []
        for item in node.select("p, h2"):
            value = clean(item.get_text(" ", strip=True))
            if value and len(value) >= 20:
                parts.append(value)
        body = "\n".join(parts) if parts else clean(node.get_text(" ", strip=True))
        if body:
            return body, selector
    return None, None


def page_evidence(
    soup: BeautifulSoup,
    *,
    title: Optional[str],
    title_selector: Optional[str],
    date_raw: Optional[str],
    date_source: Optional[str],
    body: Optional[str],
    body_selector: Optional[str],
    status: int,
) -> dict:
    """Return evidence in ``compare_evidence``'s portable signal format."""
    titles = []
    if title:
        titles.append({"kind": "visible_h1", "source": title_selector, "raw": title})
    for selector, kind in (
        ('meta[property="og:title"]', "og_title"),
        ('meta[name="twitter:title"]', "twitter_title"),
    ):
        node = soup.select_one(selector)
        value = clean(node.get("content")) if node else None
        if value and all(item["raw"] != value for item in titles):
            titles.append({"kind": kind, "source": selector, "raw": value})
    dates = []
    if date_raw:
        dates.append({"kind": "published", "source": date_source, "raw": date_raw})
    bodies = []
    if body:
        bodies.append({"kind": body_selector or "article_body", "text": body[:20000]})
    return {"titles": titles, "dates": dates, "bodies": bodies, "http_status": status}


def parser_error(title: Optional[str], body: Optional[str], published: Optional[datetime]) -> Optional[str]:
    sample = f"{title or ''} {body or ''}"[:500]
    if CHALLENGE.search(sample):
        return "challenge_or_error_page"
    missing = [name for name, value in (("title", title), ("maintext", body), ("date_publish", published)) if not value]
    return f"missing:{','.join(missing)}" if missing else None
