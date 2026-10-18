"""Network-only helpers for Pakistan observation adapters.

This package deliberately has no MongoDB import, duplicate lookup, cache write,
or promotion path. Sitemap timestamps select candidates only. Publication dates
are parsed again from each article page and accompanied by independent page
signals for the observer's semantic field checks.
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


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MLP-MLEED-observation/1.0; +https://mlp.org)"
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
MEDIA = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)(?:\?|$)", re.I)
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|checking your browser|just a moment|verify you are human|"
    r"request unsuccessful|web application firewall",
    re.I,
)


class AccessBlockedError(RuntimeError):
    """The publisher returned a challenge, denial, or unusable empty response."""


def clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result or None


def fetch(url: str, timeout: int = 35) -> requests.Response:
    errors = []
    for attempt in range(3):
        try:
            request_headers = {"User-Agent": "Googlebot"} if "nation.com.pk" in (urlparse(url).hostname or "") else None
            response = SESSION.get(url, timeout=timeout, headers=request_headers)
            response.raise_for_status()
            if not response.content:
                if response.status_code in {202, 403, 429, 503}:
                    raise AccessBlockedError(
                        f"blocked response: status={response.status_code}, empty body"
                    )
                raise ValueError("empty response")
            title = ""
            if b"<title" in response.content[:15000].lower():
                match = re.search(r"<title[^>]*>(.*?)</title>", response.text[:15000], re.I | re.S)
                title = clean(match.group(1)) or "" if match else ""
            if title and CHALLENGE.search(title):
                raise AccessBlockedError(f"challenge response: {title[:120]}")
            return response
        except (requests.RequestException, ValueError, AccessBlockedError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt < 2:
                time.sleep(1 + attempt)
    if any("AccessBlockedError" in error or "403" in error or "429" in error for error in errors):
        raise AccessBlockedError(f"access_blocked for {url}: {'; '.join(errors)}")
    raise RuntimeError(f"fetch failed for {url}: {'; '.join(errors)}")


def blocked_discovery(probe_urls: Iterable[str], evidence: dict) -> list[str]:
    """Probe expected listing endpoints and classify access failures explicitly.

    Blocked adapters return an empty discovery set with machine-readable evidence.
    The promotion gate already rejects zero-URL observations, while the status is
    preserved for dashboards instead of being misreported as normal inactivity.
    """
    evidence.update({
        "strategy": "access_probe",
        "access_status": "unknown",
        "probes": [],
        "coverage_complete": False,
    })
    for url in probe_urls:
        try:
            response = fetch(url, timeout=25)
            evidence["probes"].append({
                "url": url,
                "status": response.status_code,
                "bytes": len(response.content),
                "result": "accessible",
            })
        except AccessBlockedError as exc:
            evidence["probes"].append({"url": url, "result": "blocked", "error": str(exc)})
        except Exception as exc:
            evidence["probes"].append({"url": url, "result": "unavailable", "error": str(exc)})
    results = {item["result"] for item in evidence["probes"]}
    evidence["access_status"] = (
        "blocked_by_waf" if "blocked" in results else
        "endpoint_unavailable" if "unavailable" in results else
        "accessible_but_adapter_not_implemented"
    )
    return []


def xml_document(url: str) -> BeautifulSoup:
    soup = BeautifulSoup(fetch(url).content, "xml")
    root = soup.find()
    if root is None or root.name not in {"sitemapindex", "urlset"}:
        raise ValueError(f"not a sitemap XML document: {url}; root={getattr(root, 'name', None)}")
    return soup


def parse_datetime(raw: object) -> Optional[datetime]:
    value = clean(raw)
    if not value:
        return None
    # dateparser fills unspecified seconds with the current clock value. Parse
    # the common visible Pakistani newsroom formats exactly so stored timestamps
    # remain deterministic across runs.
    match = re.search(
        r"\b(\d{2})/(\d{2})/(20\d{2})\s*(?:(?:\||às)\s*)?(\d{1,2})(?:h|:)(\d{2})\b",
        value,
        re.I,
    )
    if match:
        day, month, year, hour, minute = map(int, match.groups())
        return datetime(year, month, day, hour, minute)
    # Preserve the publisher's displayed wall date. The control schema stores
    # naive timestamps and the validation window is expressed as publisher dates.
    order = "YMD" if re.match(r"^20\d{2}[-/.]", value) else "DMY"
    parsed = dateparser.parse(
        value,
        languages=None,
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
        normalized = urlunparse(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))
        if any(part.casefold() in normalized.casefold() for part in deny):
            return None
        return normalized
    except Exception:
        return None


def unique_urls(
    entries: Iterable[tuple[str, Optional[date]]],
    domain: str,
    deny: Iterable[str],
) -> list[str]:
    result = []
    seen = set()
    for raw, _ in entries:
        url = normalize_url(raw, domain, deny)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


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
                for value in item.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)


def _first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        value = clean(node.get("content") or node.get_text(" ", strip=True))
        if value:
            return value, selector
    return None, None


def _body(soup: BeautifulSoup, selectors: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    for selector in selectors:
        original = soup.select_one(selector)
        if not original:
            continue
        node = BeautifulSoup(str(original), "html.parser")
        for bad in node.select(
            "script, style, iframe, form, nav, aside, footer, figure, figcaption, "
            ".ad, .ads, .advertisement, .related, .share, .social, .tags, .newsletter, "
            ".author, [class*='related'], [class*='recommend'], [class*='share']"
        ):
            bad.decompose()
        paragraphs = []
        for item in node.select("p, h2"):
            value = clean(item.get_text(" ", strip=True))
            if value and len(value) >= 20 and value not in paragraphs:
                paragraphs.append(value)
        text = "\n\n".join(paragraphs)
        if text:
            return text, selector
    return None, None


def _jsonld_article(soup: BeautifulSoup) -> Optional[dict]:
    for item in jsonld_objects(soup):
        value = item.get("@type")
        types = set(value if isinstance(value, list) else [value])
        if types & {"Article", "NewsArticle", "ReportageNewsArticle", "BlogPosting"}:
            return item
    return None


def extract_article(
    url: str,
    *,
    source: str,
    body_selectors: Iterable[str],
    title_selectors: Iterable[str] = ("article h1", "main h1", "h1"),
    visible_date_selectors: Iterable[str] = (),
    visible_date_text_selectors: Iterable[str] = (),
    visible_date_pattern: Optional[str] = None,
    prefer_visible_date: bool = True,
    evidence_body_selectors: Iterable[str] = ("article", "main"),
    reject_paragraph_patterns: Iterable[str] = (),
    language: str = "en",
    category_selectors: Iterable[str] = (),
    excluded_categories: Iterable[str] = (),
) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_source = _first_text(soup, title_selectors)
    body, body_source = _body(soup, body_selectors)
    article_ld = _jsonld_article(soup)

    visible_candidates = []
    visible_raw = visible_source = None
    for selector in visible_date_text_selectors:
        for node in soup.select(selector)[:4]:
            raw = clean(node.get_text(" ", strip=True))
            if raw and all(item[0] != raw for item in visible_candidates):
                visible_candidates.append((raw, selector))
        if visible_candidates:
            visible_raw, visible_source = visible_candidates[0]
            break
    for selector in visible_date_selectors if not visible_candidates else ():
        for node in soup.select(selector)[:4]:
            raw = clean(node.get("datetime") or node.get("content") or node.get_text(" ", strip=True))
            if raw and all(item[0] != raw for item in visible_candidates):
                visible_candidates.append((raw, selector))
        if visible_candidates:
            visible_raw, visible_source = visible_candidates[0]
            break
    if not visible_raw and visible_date_pattern:
        page_text = clean(soup.get_text(" ", strip=True)) or ""
        match = re.search(visible_date_pattern, page_text, re.I)
        if match:
            visible_raw, visible_source = match.group(0), f"visible_regex:{visible_date_pattern}"
            visible_candidates.append((visible_raw, visible_source))
    ld_raw = clean(article_ld.get("datePublished")) if article_ld else None
    meta = soup.select_one('meta[property="article:published_time"], meta[itemprop="datePublished"], meta[name="datePublished"]')
    meta_raw = clean(meta.get("content") or meta.get("datetime")) if meta else None
    ordered = ((visible_raw, visible_source), (ld_raw, "jsonld.datePublished"), (meta_raw, "meta.datePublished"))
    if not prefer_visible_date:
        ordered = ((ld_raw, "jsonld.datePublished"), (meta_raw, "meta.datePublished"), (visible_raw, visible_source))
    published = published_source = None
    for raw, source_name in ordered:
        parsed = parse_datetime(raw)
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
    date_signals = [
        (raw, "visible_time" if index == 0 else "modified", source_name)
        for index, (raw, source_name) in enumerate(visible_candidates)
    ]
    date_signals.extend(((ld_raw, "published", "jsonld.datePublished"), (meta_raw, "published", "meta.datePublished")))
    for raw, kind, source_name in date_signals:
        if raw and all(item["raw"] != raw for item in dates):
            dates.append({"kind": kind, "source": source_name, "raw": raw})

    bodies = []
    ld_body = clean(article_ld.get("articleBody")) if article_ld else None
    if ld_body and len(ld_body) >= 200:
        bodies.append({"kind": "jsonld_articleBody", "text": ld_body[:20000]})
    evidence_body, evidence_body_source = _body(soup, evidence_body_selectors)
    if evidence_body and len(evidence_body) >= 200:
        bodies.append({"kind": evidence_body_source or "page_body", "text": evidence_body[:20000]})
    if not bodies and body:
        bodies.append({"kind": body_source or "selected_body", "text": body[:20000]})

    if body and reject_paragraph_patterns:
        rejected = tuple(pattern.casefold() for pattern in reject_paragraph_patterns)
        kept = [
            paragraph for paragraph in body.split("\n\n")
            if not any(pattern in paragraph.casefold() for pattern in rejected)
        ]
        body = "\n\n".join(kept).strip() or None

    category = category_source = None
    for selector in category_selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        category = clean(node.get("content") or node.get_text(" ", strip=True))
        if category:
            category_source = selector
            break
    excluded_set = {clean(item).casefold() for item in excluded_categories if clean(item)}
    is_excluded = bool(category and category.casefold() in excluded_set)
    result = {
        "url": url,
        "source_domain": source,
        "country_code": "PAK",
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": language,
        "parser": "pure_adapter",
        "collection": "SKIP" if is_excluded else None,
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
            "publisher_category": category,
            "publisher_category_source": category_source,
        },
    }
    if is_excluded:
        result.update({
            "exclusion_reason": "excluded_publisher_category",
            "exclusion_evidence": {
                "policy_type": "publisher_category",
                "publisher_category": category,
                "publisher_dom_selector": category_source,
            },
        })
    return result
