"""Network-only helpers for Malaysian observation adapters.

No helper in this module imports a database client, reads production state, or
writes a cache.  Sitemap and listing dates are candidate-selection signals;
every stored publication date is parsed again from the article page.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import dateparser
import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
MEDIA = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)(?:\?|$)", re.I)
CHALLENGE = re.compile(
    r"access denied|captcha|checking your browser|just a moment|"
    r"verify you are human|web application firewall|"
    r"attention required.{0,40}cloudflare|cloudflare.{0,40}(?:ray id|error \d{3}|security check)",
    re.I,
)


class AccessBlockedError(RuntimeError):
    """The publisher returned a denial or challenge instead of content."""


def clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result or None


def fetch(url: str, timeout: int = 45) -> requests.Response:
    errors = []
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=timeout)
            response.raise_for_status()
            if not response.content:
                raise ValueError("empty response")
            match = re.search(r"<title[^>]*>(.*?)</title>", response.text[:18000], re.I | re.S)
            title = clean(match.group(1)) if match else None
            if title and CHALLENGE.search(title):
                raise AccessBlockedError(f"challenge response: {title[:120]}")
            return response
        except (requests.RequestException, ValueError, AccessBlockedError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt < 2:
                time.sleep(1 + attempt)
    if any("403" in item or "429" in item or "AccessBlockedError" in item for item in errors):
        raise AccessBlockedError(f"access_blocked for {url}: {'; '.join(errors)}")
    raise RuntimeError(f"fetch failed for {url}: {'; '.join(errors)}")


def xml_document(url: str) -> BeautifulSoup:
    soup = BeautifulSoup(fetch(url).content, "xml")
    root = soup.find()
    if root is None or root.name not in {"sitemapindex", "urlset"}:
        raise ValueError(f"not sitemap XML: {url}; root={getattr(root, 'name', None)}")
    return soup


def parse_datetime(raw: object, languages: Iterable[str] = ("en", "ms")) -> Optional[datetime]:
    value = clean(raw)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    order = "YMD" if re.match(r"^\s*20\d{2}[-/.]", value) else "DMY"
    parsed = dateparser.parse(
        value,
        languages=list(languages),
        settings={
            "DATE_ORDER": order,
            "PREFER_LOCALE_DATE_ORDER": False,
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    return parsed.replace(tzinfo=None) if parsed else None


def sitemap_entries(soup: BeautifulSoup) -> list[tuple[str, Optional[date]]]:
    nodes = soup.find_all("sitemap") if soup.find("sitemapindex") else soup.find_all("url")
    entries = []
    for node in nodes:
        # Direct-child lookup is essential: Google News/image sitemap extensions
        # contain nested <loc> elements which are not article URLs.
        loc = node.find("loc", recursive=False)
        if loc is None:
            continue
        lastmod = node.find("lastmod", recursive=False)
        parsed = parse_datetime(lastmod.get_text(strip=True)) if lastmod else None
        entries.append((loc.get_text(strip=True), parsed.date() if parsed else None))
    return entries


def normalize_url(raw: str, domain: str, deny: Iterable[str] = ()) -> Optional[str]:
    try:
        parsed = urlparse(urljoin(f"https://{domain}/", raw))
        host = (parsed.hostname or "").lower()
        if host not in {domain, f"www.{domain}"}:
            return None
        if not parsed.path or parsed.path == "/" or MEDIA.search(parsed.path):
            return None
        path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
        normalized = urlunparse(("https", parsed.netloc.lower(), path, "", "", ""))
        if any(value.casefold() in normalized.casefold() for value in deny):
            return None
        return normalized
    except Exception:
        return None


def unique_urls(entries: Iterable[tuple[str, Optional[date]]], domain: str, deny=()) -> list[str]:
    result, seen = [], set()
    for raw, _ in entries:
        url = normalize_url(raw, domain, deny)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def url_date(raw: str) -> Optional[date]:
    match = re.search(r"/(20\d{2})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?:/|$)", raw)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def wordpress_sitemap_window(
    *, index_url: str, domain: str, start: date, end: date, evidence: dict,
    child_pattern: str = r"/post-sitemap\d*\.xml$", deny: Iterable[str] = (),
    newest_overlap: int = 3,
) -> list[str]:
    """Select live WordPress child sitemaps and validate every selected child."""
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    index_entries = [
        item for item in sitemap_entries(xml_document(index_url))
        if re.search(child_pattern, item[0])
    ]
    if not index_entries:
        raise RuntimeError(f"no matching child sitemaps in {index_url}")
    selected = {url: modified for url, modified in index_entries if modified is None or modified >= lower}
    selected.update(dict(index_entries[-newest_overlap:]))
    evidence.update({
        "strategy": "live_wordpress_sitemap_index",
        "sitemap_index": index_url,
        "child_pattern": child_pattern,
        "child_sitemaps_total": len(index_entries),
        "child_sitemaps_selected": len(selected),
        "sitemaps_checked": [],
        "sitemap_failures": [],
        "candidate_lastmod_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "coverage_complete": False,
    })
    entries = []
    for sitemap_url, modified in selected.items():
        try:
            items = sitemap_entries(xml_document(sitemap_url))
            evidence["sitemaps_checked"].append({
                "url": sitemap_url,
                "index_lastmod": modified.isoformat() if modified else None,
                "entries": len(items),
            })
            entries.extend(items)
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} selected sitemaps failed")
    candidates = [item for item in entries if item[1] is None or lower <= item[1] <= upper]
    urls = unique_urls(candidates, domain, deny)
    evidence.update({
        "entries_before_window_filter": len(entries),
        "entries_without_lastmod": sum(modified is None for _, modified in entries),
        "candidate_entries_in_window": len(candidates),
        "discovered_urls_after_filter": len(urls),
        "coverage_complete": True,
    })
    return urls


def jsonld_objects(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        stack = [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                stack.extend(value for value in item.values() if isinstance(value, (dict, list)))


def _jsonld_article(soup: BeautifulSoup) -> Optional[dict]:
    for item in jsonld_objects(soup):
        value = item.get("@type")
        types = set(value if isinstance(value, list) else [value])
        if types & {"Article", "NewsArticle", "ReportageNewsArticle"}:
            return item
    return None


def _first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = clean(node.get("content") or node.get_text(" ", strip=True))
            if value:
                return value, selector
    return None, None


def _body(
    soup: BeautifulSoup, selectors: Iterable[str],
    drop_selectors: Iterable[str] = (),
) -> tuple[Optional[str], Optional[str]]:
    for selector in selectors:
        original = soup.select_one(selector)
        if not original:
            continue
        node = BeautifulSoup(str(original), "html.parser")
        for bad in node.select(
            "script, style, iframe, form, nav, aside, footer, figure, figcaption, "
            ".ad, .ads, .advertisement, .related, .share, .social, .tags, .newsletter, "
            ".author, .pp-posts-container, .comments-area, #comments, .elementor-global, "
            "[class*='related'], [class*='recommend'], [class*='share']"
            + (", " + ", ".join(drop_selectors) if drop_selectors else "")
        ):
            bad.decompose()
        paragraphs = []
        for item in node.select("p, h2, h3"):
            value = clean(item.get_text(" ", strip=True))
            if value and len(value) >= 20 and value not in paragraphs:
                paragraphs.append(value)
        text = "\n\n".join(paragraphs)
        if text:
            return text, selector
    return None, None


def extract_article(
    url: str, *, source: str, language: str,
    body_selectors: Iterable[str],
    title_selectors: Iterable[str] = ("article h1", "main h1", "h1"),
    visible_date_selectors: Iterable[str] = ("article time", "time"),
    prefer_visible_date: bool = False,
    evidence_body_selectors: Iterable[str] = ("article", "main"),
    embedded_body: Optional[tuple[str, str, str]] = None,
    body_drop_selectors: Iterable[str] = (),
) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    article_ld = _jsonld_article(soup)
    title, title_source = _first_text(soup, title_selectors)
    if not title and article_ld:
        title = clean(article_ld.get("headline"))
        title_source = "jsonld.headline" if title else None
    body, body_source = _body(soup, body_selectors, body_drop_selectors)
    embedded_text = embedded_source = None
    if not body and embedded_body:
        selector, attribute, key = embedded_body
        node = soup.select_one(selector)
        raw = node.get(attribute) if node else None
        try:
            payload = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get(key):
            embedded_soup = BeautifulSoup(f"<div>{payload[key]}</div>", "html.parser")
            embedded_text, _ = _body(embedded_soup, ("div",))
            if embedded_text:
                body, body_source = embedded_text, f"{selector}[{attribute}].{key}"
                embedded_source = body_source
    if not body and article_ld:
        candidate = clean(article_ld.get("articleBody"))
        if candidate:
            body, body_source = candidate, "jsonld.articleBody"

    visible_candidates = []
    for selector in visible_date_selectors:
        for node in soup.select(selector)[:5]:
            raw = clean(node.get("datetime") or node.get("content") or node.get_text(" ", strip=True))
            if raw and all(item[0] != raw for item in visible_candidates):
                visible_candidates.append((raw, selector))
    visible_raw, visible_source = visible_candidates[0] if visible_candidates else (None, None)
    ld_raw = clean(article_ld.get("datePublished")) if article_ld else None
    meta = soup.select_one(
        'meta[property="article:published_time"], meta[itemprop="datePublished"], meta[name="datePublished"]'
    )
    meta_raw = clean(meta.get("content") or meta.get("datetime")) if meta else None
    ordered = ((ld_raw, "jsonld.datePublished"), (meta_raw, "meta.datePublished"), (visible_raw, visible_source))
    if prefer_visible_date:
        ordered = ((visible_raw, visible_source), (ld_raw, "jsonld.datePublished"), (meta_raw, "meta.datePublished"))
    published = published_source = None
    for raw, source_name in ordered:
        parsed = parse_datetime(raw, (language, "en", "ms"))
        if parsed:
            published, published_source = parsed, source_name
            break

    titles = []
    if title:
        titles.append({"kind": "visible_h1", "source": title_source, "raw": title})
    for raw, kind, source_name in (
        (clean(soup.select_one('meta[property="og:title"]').get("content")) if soup.select_one('meta[property="og:title"]') else None, "og_title", 'meta[property="og:title"]'),
        (clean(soup.select_one('meta[name="twitter:title"]').get("content")) if soup.select_one('meta[name="twitter:title"]') else None, "twitter_title", 'meta[name="twitter:title"]'),
        (clean(article_ld.get("headline")) if article_ld else None, "jsonld_headline", "jsonld.headline"),
    ):
        if raw and all(item["raw"] != raw for item in titles):
            titles.append({"kind": kind, "source": source_name, "raw": raw})

    dates = []
    for raw, kind, source_name in (
        (ld_raw, "published", "jsonld.datePublished"),
        (meta_raw, "published", "meta.datePublished"),
        *[(raw, "visible_time", source_name) for raw, source_name in visible_candidates],
    ):
        if raw and all(item["raw"] != raw for item in dates):
            dates.append({"kind": kind, "source": source_name, "raw": raw})

    bodies = []
    ld_body = clean(article_ld.get("articleBody")) if article_ld else None
    if ld_body and len(ld_body) >= 150:
        bodies.append({"kind": "jsonld_articleBody", "text": ld_body[:20000]})
    evidence_body, evidence_body_source = _body(soup, evidence_body_selectors, body_drop_selectors)
    if evidence_body and len(evidence_body) >= 150:
        bodies.append({"kind": evidence_body_source or "page_body", "text": evidence_body[:20000]})
    if not bodies and body:
        bodies.append({"kind": body_source or "selected_body", "text": body[:20000]})
    elif embedded_text and not any(item.get("kind") == embedded_source for item in bodies):
        bodies.append({"kind": embedded_source, "text": embedded_text[:20000]})

    collection = "opinion-articles" if "/opinion/" in url.casefold() else None
    return {
        "url": url,
        "source_domain": source,
        "country_code": "MYS",
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": language,
        "parser": "pure_adapter",
        "collection": collection,
        "parser_error": None,
        "field_sources": {
            "title": title_source,
            "date_publish": published_source,
            "maintext": body_source,
        },
        "field_evidence": {
            "titles": titles,
            "dates": dates,
            "bodies": bodies,
            "http_status": response.status_code,
        },
    }
