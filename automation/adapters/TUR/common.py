"""Network-only discovery and extraction helpers for Turkish sources.

No MongoDB dependency or production insertion path exists in this module.
Sitemap dates select sitemap files; they are never accepted as article dates.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MLP-MLEED-observation/1.0)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_CLOUD_SESSION = None
MEDIA = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|svg|pdf|mp4|mp3)(?:\?|$)", re.I)
CHALLENGE = re.compile(r"just a moment|verify you are human|access denied|cloudflare|service temporarily unavailable", re.I)


def _challenge_response(response: requests.Response) -> bool:
    head = (response.text or "")[:12000]
    match = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    title = clean(match.group(1)) if match else ""
    return bool(title and CHALLENGE.search(title))


def fetch(url: str, timeout: int = 30) -> requests.Response:
    global _CLOUD_SESSION
    errors = []
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=timeout)
            if response.status_code == 200 and response.content and not _challenge_response(response):
                return response
            errors.append(f"requests status={response.status_code} challenge={_challenge_response(response)}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        # Retry access failures with a persistent browser-like TLS session.
        try:
            if _CLOUD_SESSION is None:
                import cloudscraper
                _CLOUD_SESSION = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "linux", "mobile": False}
                )
            cloud = _CLOUD_SESSION.get(url, timeout=timeout)
            if cloud.status_code == 200 and cloud.content and not _challenge_response(cloud):
                return cloud
            errors.append(f"cloudscraper status={cloud.status_code} challenge={_challenge_response(cloud)}")
        except Exception as cloud_exc:
            errors.append(f"cloudscraper {type(cloud_exc).__name__}: {cloud_exc}")
        if attempt < 2:
                import time
                time.sleep(1 + attempt)
    raise RuntimeError(f"fetch failed for {url}: {'; '.join(errors)}")


def xml_root(url: str) -> BeautifulSoup:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "xml")
    root = soup.find()
    if root is None or root.name not in {"urlset", "sitemapindex"}:
        raise ValueError(f"not a sitemap XML document: {url}; root={getattr(root, 'name', None)}")
    return soup


def sitemap_entries(soup: BeautifulSoup) -> List[Tuple[str, Optional[date]]]:
    entries = soup.find_all("sitemap") if soup.find("sitemapindex") else soup.find_all("url")
    result = []
    for entry in entries:
        loc = entry.find("loc", recursive=False)
        if loc is None:
            continue
        url = loc.get_text(strip=True)
        lastmod = entry.find("lastmod", recursive=False)
        d = parse_iso_date(lastmod.get_text(strip=True)) if lastmod else None
        result.append((url, d))
    return result


def parse_iso_date(raw: str) -> Optional[date]:
    try:
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return None


def normalize_url(url: str, domain: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host != domain and host != f"www.{domain}":
            return None
        if MEDIA.search(parsed.path) or not parsed.path or parsed.path == "/":
            return None
        return urlunparse((parsed.scheme or "https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))
    except Exception:
        return None


def unique_article_urls(entries: Iterable[Tuple[str, Optional[date]]], domain: str, deny: Iterable[str] = ()) -> List[str]:
    seen = set()
    result = []
    for raw, _ in entries:
        url = normalize_url(raw, domain)
        if not url or any(pattern in url.lower() for pattern in deny) or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result or None


def parse_datetime(raw: object, languages: Optional[List[str]] = None) -> Optional[datetime]:
    if not raw:
        return None
    # dateparser with an explicit Turkish language can reject otherwise valid
    # ISO 8601 offsets. Article metadata commonly uses this form, so parse it
    # deterministically before falling back to localized visible text.
    candidate = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        if parsed.tzinfo:
            # The production schema stores naive publication timestamps. Keep
            # the publisher's displayed wall time so a 00:05 Turkish article
            # remains on the visible publication day instead of shifting to
            # the previous UTC date.
            return parsed.replace(tzinfo=None)
        return parsed
    except ValueError:
        pass
    try:
        import dateparser
        parsed = dateparser.parse(candidate, languages=languages, settings={"RETURN_AS_TIMEZONE_AWARE": False})
        if parsed is None:
            return None
        if parsed.tzinfo:
            return parsed.replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def jsonld_objects(soup: BeautifulSoup) -> Iterable[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack = [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                stack.extend(v for k, v in item.items() if k in {"@graph", "mainEntity"} and isinstance(v, (list, dict)))


def published_date(soup: BeautifulSoup, visible_selectors: Iterable[str] = ()) -> Tuple[Optional[datetime], Optional[str], Optional[str]]:
    # Prefer a visibly rendered publication date when the site has a stable selector.
    for selector in visible_selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        raw = node.get("datetime") or node.get("content") or node.get_text(" ", strip=True)
        dt = parse_datetime(raw, ["tr"])
        if dt:
            return dt, f"visible:{selector}", clean(raw)
    for selector in (
        'meta[property="article:published_time"]',
        'meta[name="datePublished"]',
        'meta[itemprop="datePublished"]',
        'time[itemprop="datePublished"]',
    ):
        node = soup.select_one(selector)
        if node:
            raw = node.get("content") or node.get("datetime") or node.get_text(" ", strip=True)
            dt = parse_datetime(raw, ["tr"])
            if dt:
                return dt, f"meta:{selector}", clean(raw)
    for obj in jsonld_objects(soup):
        raw = obj.get("datePublished")
        dt = parse_datetime(raw, ["tr"])
        if dt:
            return dt, "jsonld:datePublished", clean(raw)
    return None, None, None


def article_body(soup: BeautifulSoup, selectors: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    fallback = None
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        node = BeautifulSoup(str(node), "html.parser")
        root_size = len(clean(node.get_text(" ", strip=True)) or "")
        for bad in node.select("script, style, nav, aside, footer, iframe, .ad, .advertisement, .related, .share, .social"):
            # Some publisher pages contain malformed closing tags that make a
            # footer appear to wrap almost the complete article. Removing such
            # a node would erase real text, so only drop bounded descendants.
            bad_size = len(clean(bad.get_text(" ", strip=True)) or "")
            if root_size and bad_size / root_size >= 0.7:
                continue
            bad.decompose()
        parts = [clean(p.get_text(" ", strip=True)) for p in node.select("p")]
        parts = [p for p in parts if p and len(p) >= 20]
        body = "\n".join(parts) if parts else clean(node.get_text(" ", strip=True))
        if body:
            fallback = fallback or (body, selector)
            words = len(re.findall(r"\b\w+\b", body, re.UNICODE))
            boilerplate_only = "her türlü telif hakkı mega ajans" in body.casefold()
            if words >= 40 and not boilerplate_only:
                return body, selector
    jsonld_bodies = []
    for obj in jsonld_objects(soup):
        body = clean(obj.get("articleBody"))
        if body:
            jsonld_bodies.append(body)
    if jsonld_bodies:
        body = max(jsonld_bodies, key=len)
        if not fallback or len(body) > len(fallback[0]):
            return body, "jsonld:articleBody"
    return fallback or (None, None)


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
    """Return independent page signals in the observer's portable format."""
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


def extract_article(url: str, source: str, body_selectors: Iterable[str], visible_date_selectors: Iterable[str] = (), title_selectors: Iterable[str] = ()) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title = None
    title_selector = None
    for selector in title_selectors or ("h1",):
        node = soup.select_one(selector)
        if node:
            title = clean(node.get_text(" ", strip=True))
            if title:
                title_selector = selector
                break
    body, body_selector = article_body(soup, body_selectors)
    published, date_selector, raw_date = published_date(soup, visible_date_selectors)
    parser = "site_selectors"
    if not title or not body or not published:
        try:
            from newsplease import NewsPlease
            candidate = NewsPlease.from_html(response.text, url=url)
            title = title or clean(candidate.title)
            body = body or clean(candidate.maintext)
            published = published or candidate.date_publish
            parser = "site_selectors+newsplease"
        except Exception:
            pass
    if published and published.tzinfo:
        published = published.replace(tzinfo=None)
    problem = None
    if CHALLENGE.search((title or "")[:180]) or CHALLENGE.search((body or "")[:180]):
        problem = "challenge_or_error_page"
    return {
        "url": url,
        "source_domain": source,
        "country_code": "TUR",
        "title": title,
        "maintext": body,
        "date_publish": published,
        "parser": parser,
        "parser_error": problem,
        "field_evidence": page_evidence(
            soup,
            title=title,
            title_selector=title_selector,
            date_raw=raw_date,
            date_source=date_selector,
            body=body,
            body_selector=body_selector,
            status=response.status_code,
        ),
    }


def inclusive_days(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
