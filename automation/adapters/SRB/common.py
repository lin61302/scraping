"""Network-only helpers shared by Serbian observation adapters.

The helpers deliberately contain no database client, duplicate lookup, or
production write path. Sitemap and listing dates are discovery signals only;
``extract_article`` parses publication time again from the publisher page and
emits independent evidence for title, publication date, and body.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from typing import Iterable, Iterator, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import cloudscraper
import dateparser
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MLP-MLEED-observation/1.0; +https://mlp.org)"
}
MEDIA = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)(?:\?|$)", re.I)
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|checking your browser|just a moment|"
    r"verify you are human|website is currently not available|too many requests",
    re.I,
)
SERBIAN_MONTHS = {
    "januar": 1, "januara": 1, "jan": 1,
    "februar": 2, "februara": 2, "feb": 2,
    "mart": 3, "marta": 3, "mar": 3,
    "april": 4, "aprila": 4, "apr": 4,
    "maj": 5, "maja": 5,
    "jun": 6, "juna": 6,
    "jul": 7, "jula": 7,
    "avgust": 8, "avgusta": 8, "avg": 8,
    "septembar": 9, "septembra": 9, "sep": 9,
    "oktobar": 10, "oktobra": 10, "okt": 10,
    "novembar": 11, "novembra": 11, "nov": 11,
    "decembar": 12, "decembra": 12, "dec": 12,
}


class AccessBlockedError(RuntimeError):
    """The publisher returned a challenge, denial, or unusable response."""


def clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result or None


def _scraper():
    scraper = cloudscraper.create_scraper()
    scraper.headers.update(HEADERS)
    return scraper


def fetch(url: str, timeout: int = 35, attempts: int = 4):
    errors = []
    candidates = [url]
    for attempt in range(attempts):
        candidate = candidates[attempt % len(candidates)]
        scraper = None
        response = None
        try:
            scraper = _scraper()
            response = scraper.get(candidate, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            if not response.content:
                raise AccessBlockedError(f"empty response with status {response.status_code}")
            head = response.text[:20000]
            title_match = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
            title = clean(title_match.group(1)) if title_match else ""
            if (title and CHALLENGE.search(title)) or CHALLENGE.search(head[:1000]):
                raise AccessBlockedError(f"challenge response: {(title or head[:100])[:120]}")
            # `.content` and `.text` above have materialized the body. Close the
            # response and its one-shot cloudscraper session before returning
            # the still-readable cached response, otherwise large observations
            # accumulate CLOSE_WAIT sockets until the process exhausts fds.
            response.close()
            scraper.close()
            return response
        except Exception as exc:
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
            if response is not None:
                response.close()
            if scraper is not None:
                scraper.close()
            if attempt < attempts - 1:
                time.sleep(0.5 + attempt * 0.5)
    if any(token in " ".join(errors).casefold() for token in ("403", "429", "challenge", "blocked")):
        raise AccessBlockedError(f"access_blocked for {url}: {'; '.join(errors)}")
    raise RuntimeError(f"fetch failed for {url}: {'; '.join(errors)}")


def blocked_discovery(probe_urls: Iterable[str], evidence: dict) -> list[str]:
    evidence.update({"strategy": "access_probe", "probes": [], "coverage_complete": False})
    for url in probe_urls:
        try:
            response = fetch(url, timeout=20)
            evidence["probes"].append({"url": url, "result": "accessible", "status": response.status_code, "bytes": len(response.content)})
        except AccessBlockedError as exc:
            evidence["probes"].append({"url": url, "result": "blocked", "error": str(exc)})
        except Exception as exc:
            evidence["probes"].append({"url": url, "result": "unavailable", "error": str(exc)})
    results = {row["result"] for row in evidence["probes"]}
    evidence["access_status"] = "blocked_by_waf" if "blocked" in results else "endpoint_unavailable"
    return []


def xml_document(url: str) -> BeautifulSoup:
    soup = BeautifulSoup(fetch(url).content, "xml")
    root = soup.find()
    if root is None or root.name not in {"sitemapindex", "urlset"}:
        raise ValueError(f"not sitemap XML: {url}; root={getattr(root, 'name', None)}")
    return soup


def parse_datetime(raw: object) -> Optional[datetime]:
    value = clean(raw)
    if not value:
        return None
    value = value.replace("godine", "").replace("god.", "").strip()
    if re.match(r"^20\d{2}-\d\d-\d\d(?:T|\s)", value):
        try:
            parsed = dateutil_parser.isoparse(value)
            return parsed.replace(tzinfo=None)
        except (TypeError, ValueError):
            pass
    numeric = re.search(
        r"\b(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(20\d{2})\.?(?:\s*(?:u|,)?\s*(\d{1,2}):?(\d{2})?)?",
        value,
        re.I,
    )
    if numeric:
        day, month, year = map(int, numeric.group(1, 2, 3))
        hour = int(numeric.group(4) or 0)
        minute = int(numeric.group(5) or 0)
        try:
            return datetime(year, month, day, hour, minute)
        except ValueError:
            return None
    named = re.search(
        r"\b(\d{1,2})\.?\s+(januar(?:a)?|februar(?:a)?|mart(?:a)?|april(?:a)?|maj(?:a)?|"
        r"jun(?:a)?|jul(?:a)?|avgust(?:a)?|septembar(?:a)?|oktobar(?:a)?|novembar(?:a)?|"
        r"decembar(?:a)?|jan|feb|mar|apr|avg|sep|okt|nov|dec)\s+(20\d{2})\.?(?:\s*(?:u|,)?\s*(\d{1,2}):(\d{2}))?",
        value,
        re.I,
    )
    if named:
        day = int(named.group(1))
        month = SERBIAN_MONTHS[named.group(2).casefold()]
        year = int(named.group(3))
        return datetime(year, month, day, int(named.group(4) or 0), int(named.group(5) or 0))
    parsed = dateparser.parse(
        value,
        languages=["sr", "hr", "en"],
        settings={"DATE_ORDER": "DMY", "PREFER_LOCALE_DATE_ORDER": False, "RETURN_AS_TIMEZONE_AWARE": False},
    )
    return parsed.replace(tzinfo=None) if parsed else None


def sitemap_rows(soup: BeautifulSoup) -> list[tuple[str, Optional[datetime]]]:
    nodes = soup.find_all("sitemap") if soup.find("sitemapindex") else soup.find_all("url")
    result = []
    for node in nodes:
        loc = node.find("loc", recursive=False)
        if loc is None:
            continue
        lastmod = node.find("lastmod", recursive=False)
        result.append((loc.get_text(strip=True), parse_datetime(lastmod.get_text(strip=True)) if lastmod else None))
    return result


def normalize_url(raw: str, domains: str | Iterable[str], deny: Iterable[str] = ()) -> Optional[str]:
    allowed = {domains} if isinstance(domains, str) else set(domains)
    try:
        first = sorted(allowed)[0]
        parsed = urlparse(urljoin(f"https://{first}/", raw))
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        allowed_bare = {
            lowered[4:] if lowered.startswith("www.") else lowered
            for lowered in (domain.lower() for domain in allowed)
        }
        if host not in allowed_bare or not parsed.path or parsed.path == "/" or MEDIA.search(parsed.path):
            return None
        normalized = urlunparse(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))
        if any(fragment.casefold() in normalized.casefold() for fragment in deny):
            return None
        return normalized
    except Exception:
        return None


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
        if types & {"Article", "NewsArticle", "ReportageNewsArticle", "BlogPosting"}:
            return item
    return None


def first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
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
            "script, style, iframe, form, nav, aside, footer, figure, figcaption, "
            ".ad, .ads, .advertisement, .related, .share, .social, .tags, .newsletter, "
            ".author, .post-disclaimer, [class*='related'], [class*='recommend'], [class*='share']"
        ):
            bad.decompose()
        paragraphs = []
        for item in node.select("p, h2, li"):
            value = clean(item.get_text(" ", strip=True))
            if value and len(value) >= 20 and value not in paragraphs:
                paragraphs.append(value)
        text = "\n\n".join(paragraphs)
        if text:
            return text, selector
        text = clean(node.get_text("\n", strip=True))
        if text and len(text) >= 120:
            return text, selector
    return None, None


def extract_article(
    url: str,
    *,
    source: str,
    title_selectors: Iterable[str] = ("article h1", "main h1", "h1"),
    body_selectors: Iterable[str],
    visible_date_selectors: Iterable[str] = (),
    visible_date_text_selectors: Iterable[str] = (),
    visible_date_pattern: Optional[str] = None,
    evidence_body_selectors: Iterable[str] = (),
    environmental: bool = False,
    language: str = "sr",
) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_source = first_text(soup, title_selectors)
    body, body_source = article_body(soup, body_selectors)
    article_ld = _jsonld_article(soup)

    date_signals = []
    ld_published = clean(article_ld.get("datePublished")) if article_ld else None
    ld_modified = clean(article_ld.get("dateModified")) if article_ld else None
    if ld_published:
        date_signals.append((ld_published, "published", "jsonld.datePublished"))
    for selector in visible_date_selectors:
        for node in soup.select(selector)[:4]:
            raw = clean(node.get("datetime") or node.get("content") or node.get_text(" ", strip=True))
            if raw and all(item[0] != raw for item in date_signals):
                date_signals.append((raw, "published", selector))
    for selector in visible_date_text_selectors:
        for node in soup.select(selector)[:4]:
            raw = clean(node.get_text(" ", strip=True))
            if raw and all(item[0] != raw for item in date_signals):
                date_signals.append((raw, "visible_time", selector))
    if visible_date_pattern:
        page_text = clean(soup.get_text(" ", strip=True)) or ""
        match = re.search(visible_date_pattern, page_text, re.I)
        if match:
            raw = clean(match.group(0))
            if raw and all(item[0] != raw for item in date_signals):
                date_signals.append((raw, "visible_time", f"visible_regex:{visible_date_pattern}"))
    if ld_modified and all(item[0] != ld_modified for item in date_signals):
        date_signals.append((ld_modified, "modified", "jsonld.dateModified"))
    published = published_source = None
    for raw, kind, source_name in date_signals:
        if kind == "modified":
            continue
        parsed = parse_datetime(raw)
        if parsed:
            published, published_source = parsed, source_name
            break

    titles = []
    if title:
        titles.append({"kind": "visible_h1", "source": title_source, "raw": title})
    og = soup.select_one('meta[property="og:title"]')
    for raw, kind, source_name in (
        (clean(og.get("content")) if og else None, "og_title", 'meta[property="og:title"]'),
        (clean(article_ld.get("headline")) if article_ld else None, "jsonld_headline", "jsonld.headline"),
    ):
        if raw and all(item["raw"] != raw for item in titles):
            titles.append({"kind": kind, "source": source_name, "raw": raw})
    dates = [
        {"kind": kind, "source": source_name, "raw": raw}
        for raw, kind, source_name in date_signals
    ]
    bodies = []
    ld_body = clean(article_ld.get("articleBody")) if article_ld else None
    if ld_body and len(ld_body) >= 200:
        bodies.append({"kind": "jsonld_articleBody", "text": ld_body[:20000]})
    evidence_body, evidence_source = article_body(soup, evidence_body_selectors or body_selectors)
    if evidence_body and len(evidence_body) >= 200:
        bodies.append({"kind": evidence_source or "page_body", "text": evidence_body[:20000]})
    if not bodies and body:
        bodies.append({"kind": body_source or "selected_body", "text": body[:20000]})

    return {
        "url": url,
        "source_domain": source,
        "country_code": "SRB",
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": language,
        "environmental": environmental,
        "parser": "pure_adapter",
        "collection": None,
        "parser_error": None,
        "field_sources": {"title": title_source, "date_publish": published_source, "maintext": body_source},
        "field_evidence": {
            "titles": titles,
            "dates": dates,
            "bodies": bodies,
            "http_status": response.status_code,
        },
    }


def discover_category_pages(
    *,
    base_url: str,
    page_url,
    link_selector: str,
    domains: str | Iterable[str],
    start: date,
    end: date,
    evidence: dict,
    card_selector: str,
    max_pages: int = 300,
    accept_url=None,
    fetch_timeout: int = 35,
    fetch_attempts: int = 4,
) -> list[str]:
    """Crawl a newest-first category until two wholly old dated pages."""
    selected: dict[str, Optional[date]] = {}
    pages = []
    consecutive_old = 0
    for page in range(1, max_pages + 1):
        url = page_url(page)
        soup = BeautifulSoup(fetch(url, timeout=fetch_timeout, attempts=fetch_attempts).content, "html.parser")
        rows = []
        for card in soup.select(card_selector):
            link = card.select_one(link_selector)
            if not link:
                continue
            article_url = normalize_url(link.get("href", ""), domains)
            raw = clean(card.get_text(" ", strip=True)) or ""
            published = parse_datetime(raw)
            if article_url and (accept_url is None or accept_url(article_url)):
                rows.append((article_url, published.date() if published else None))
        # Fall back when the link itself is the card.
        if not rows and card_selector == link_selector:
            for link in soup.select(link_selector):
                article_url = normalize_url(link.get("href", ""), domains)
                if article_url:
                    rows.append((article_url, parse_datetime(link.get_text(" ", strip=True))))
        if not rows:
            break
        dated = [day for _, day in rows if day]
        pages.append({
            "url": url,
            "rows": len(rows),
            "dated_rows": len(dated),
            "oldest": min(dated).isoformat() if dated else None,
            "newest": max(dated).isoformat() if dated else None,
        })
        for article_url, day in rows:
            if day is None or start <= day <= end:
                selected[article_url] = day
        consecutive_old = consecutive_old + 1 if dated and max(dated) < start else 0
        if consecutive_old >= 2:
            break
    evidence.update({
        "strategy": "cutoff_driven_category_pagination",
        "listing": base_url,
        "pages_checked": pages,
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": consecutive_old >= 2,
    })
    return sorted(selected, key=lambda url: (selected[url] or date.min, url))
