"""Network-only helpers for Armenia observation adapters.

Article pages are the authority for title, publication date, and main text.
Listing and sitemap timestamps are used only to select candidate URLs.  This
module intentionally has no database imports or write path.
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
    r"access denied|captcha|cloudflare|checking your browser|just a moment|"
    r"verify you are human|web application firewall",
    re.I,
)


class AccessBlockedError(RuntimeError):
    """The publisher returned a denial or challenge page."""


def clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    return result or None


def fetch(url: str, timeout: int = 40) -> requests.Response:
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


def parse_datetime(raw: object) -> Optional[datetime]:
    value = clean(raw)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    # Several Armenian themes append views/categories after a pipe.  Those
    # labels contain numbers and can make a correct visible timestamp
    # ambiguous to a general parser, so parse only the publisher's date span.
    value = value.split("|", 1)[0].strip()
    order = "YMD" if re.match(r"^20\d{2}[-/.]", value) else "DMY"
    parsed = dateparser.parse(
        value,
        languages=["hy", "ru", "en"],
        settings={
            "DATE_ORDER": order,
            "PREFER_LOCALE_DATE_ORDER": False,
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    return parsed.replace(tzinfo=None) if parsed else None


def xml_document(url: str) -> BeautifulSoup:
    soup = BeautifulSoup(fetch(url).content, "xml")
    root = soup.find()
    if root is None or root.name not in {"sitemapindex", "urlset"}:
        raise ValueError(f"not sitemap XML: {url}; root={getattr(root, 'name', None)}")
    return soup


def sitemap_entries(soup: BeautifulSoup) -> list[tuple[str, Optional[date]]]:
    nodes = soup.find_all("sitemap") if soup.find("sitemapindex") else soup.find_all("url")
    rows = []
    for node in nodes:
        loc = node.find("loc", recursive=False)
        if not loc:
            continue
        modified = node.find("lastmod", recursive=False)
        parsed = parse_datetime(modified.get_text(strip=True)) if modified else None
        rows.append((loc.get_text(strip=True), parsed.date() if parsed else None))
    return rows


def normalize_url(raw: str, domains: str | Iterable[str], deny: Iterable[str] = ()) -> Optional[str]:
    allowed = {domains} if isinstance(domains, str) else set(domains)
    try:
        first = sorted(allowed)[0]
        parsed = urlparse(urljoin(f"https://{first}/", raw))
        host = (parsed.hostname or "").lower()
        expanded = allowed | {f"www.{value}" for value in allowed}
        if host not in expanded or not parsed.path or MEDIA.search(parsed.path):
            return None
        path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/") or "/"
        normalized = urlunparse(("https", parsed.netloc.lower(), path, "", parsed.query, ""))
        if any(item.casefold() in normalized.casefold() for item in deny):
            return None
        return normalized
    except Exception:
        return None


def wordpress_sitemap_window(
    *, index_url: str, domains: str | Iterable[str], start: date, end: date,
    evidence: dict, child_pattern: str = r"/wp-sitemap-posts-post-\d+\.xml$",
    deny: Iterable[str] = (),
) -> list[str]:
    """Walk newest WordPress post sitemaps back through the requested edge."""
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    children = [
        url for url, _ in sitemap_entries(xml_document(index_url))
        if re.search(child_pattern, url)
    ]
    if not children:
        raise RuntimeError(f"no post sitemaps in {index_url}")
    rows, checked, boundary = [], [], False
    for child in reversed(children):
        entries = sitemap_entries(xml_document(child))
        dates = [modified for _, modified in entries if modified]
        checked.append({
            "url": child,
            "entries": len(entries),
            "newest": max(dates).isoformat() if dates else None,
            "oldest": min(dates).isoformat() if dates else None,
        })
        rows.extend(entries)
        if dates and min(dates) <= lower:
            boundary = True
            break
    if not boundary:
        raise RuntimeError(f"sitemap walk did not reach requested start for {index_url}")
    result, seen = [], set()
    for raw, modified in rows:
        if modified is not None and not (lower <= modified <= upper):
            continue
        url = normalize_url(raw, domains, deny)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    evidence.update({
        "strategy": "live_wordpress_sitemap_backwalk",
        "sitemap_index": index_url,
        "post_sitemaps_total": len(children),
        "sitemaps_checked": checked,
        "boundary_reached": boundary,
        "coverage_complete": boundary,
        "candidate_lastmod_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "discovered_urls_after_filter": len(result),
    })
    return result


def jsonld_objects(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(tag.string or tag.get_text() or "")
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
        kinds = item.get("@type")
        kinds = set(kinds if isinstance(kinds, list) else [kinds])
        if kinds & {"Article", "NewsArticle", "ReportageNewsArticle"}:
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
    soup: BeautifulSoup, selectors: Iterable[str], *, preserve_figures: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    for selector in selectors:
        original = soup.select_one(selector)
        if not original:
            continue
        node = BeautifulSoup(str(original), "html.parser")
        removable = (
            "script, style, iframe, form, nav, aside, footer, "
            ".ad, .ads, .advertisement, .related, .share, .social, .tags, .newsletter, "
            ".author, [class*='related'], [class*='recommend'], [class*='share'], "
            "[class*='social']"
        )
        if not preserve_figures:
            removable += ", figure, figcaption"
        for bad in node.select(removable):
            bad.decompose()
        paragraphs = []
        text_nodes = "p, h2, h3, li"
        if preserve_figures:
            text_nodes += ", figcaption"
        for item in node.select(text_nodes):
            value = clean(item.get_text(" ", strip=True))
            if value and len(value) >= 20 and value not in paragraphs:
                paragraphs.append(value)
        text = "\n\n".join(paragraphs)
        if not text:
            # Some publisher themes wrap prose in plain nested divs instead of
            # paragraph elements. The enclosing source-specific selector is
            # still narrow, so its cleaned text is a safer fallback than
            # treating a real article as empty.
            fallback = clean(node.get_text(" ", strip=True))
            text = fallback if fallback and len(fallback) >= 20 else ""
        if text:
            return text, selector
    return None, None


def apply_youtube_only_exclusion(article: dict) -> bool:
    """Classify a verified publisher video item as an intentional policy skip."""
    body = clean(article.get("maintext"))
    if body and len(body) <= 120 and {"facebook", "telegram"} <= set(body.casefold().split()):
        article["maintext"] = None
        article["field_sources"]["maintext"] = None
        article["field_evidence"]["bodies"] = []
        body = None
    if body or not article.get("title") or not article.get("date_publish"):
        return False
    videos = (article.get("field_evidence") or {}).get("publisher_videos") or []
    embed_url = None
    video_id = None
    for value in videos:
        raw = value.get("embed_url") if isinstance(value, dict) else value
        raw = clean(raw)
        if raw and raw.startswith("//"):
            raw = "https:" + raw
        match = re.match(r"https://www\.youtube\.com/embed/([A-Za-z0-9_-]+)", raw or "")
        if match:
            embed_url = raw
            video_id = match.group(1)
            break
    if not embed_url or not video_id:
        return False
    evidence = {
        "provider": "youtube",
        "selector": "iframe[src*='youtube.com/embed/']",
        "embed_url": embed_url,
        "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "content_text_empty": True,
    }
    article["field_evidence"]["publisher_videos"] = [evidence]
    article["collection"] = "SKIP"
    article["parser_error"] = None
    article["content_classification"] = "publisher_video_only"
    article["exclusion_reason"] = "excluded_video_only_publisher_item"
    article["exclusion_evidence"] = {"policy_type": "publisher_video_only", **evidence}
    return True


def extract_article(
    url: str, *, source: str, language: str,
    body_selectors: Iterable[str],
    title_selectors: Iterable[str] = ("article h1", "main h1", "h1"),
    visible_date_selectors: Iterable[str] = ("article time", "time"),
    prefer_visible_date: bool = False,
    evidence_body_selectors: Iterable[str] = ("article", "main"),
    preserve_figures: bool = False,
) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    article_ld = _jsonld_article(soup)
    title, title_source = _first_text(soup, title_selectors)
    if not title and article_ld:
        title = clean(article_ld.get("headline"))
        title_source = "jsonld.headline" if title else None
    body, body_source = _body(soup, body_selectors, preserve_figures=preserve_figures)
    if not body and article_ld:
        body = clean(article_ld.get("articleBody"))
        body_source = "jsonld.articleBody" if body else None

    visible = []
    for selector in visible_date_selectors:
        for node in soup.select(selector)[:5]:
            raw = clean(node.get("datetime") or node.get("content") or node.get_text(" ", strip=True))
            if raw and all(item[0] != raw for item in visible):
                visible.append((raw, selector))
    ld_raw = clean(article_ld.get("datePublished")) if article_ld else None
    meta = soup.select_one(
        'meta[property="article:published_time"], meta[itemprop="datePublished"], meta[name="datePublished"]'
    )
    meta_raw = clean(meta.get("content") or meta.get("datetime")) if meta else None
    ordered = [(ld_raw, "jsonld.datePublished"), (meta_raw, "meta.datePublished"), *visible]
    if prefer_visible_date:
        ordered = [*visible, (ld_raw, "jsonld.datePublished"), (meta_raw, "meta.datePublished")]
    published = published_source = None
    for raw, source_name in ordered:
        parsed = parse_datetime(raw)
        if parsed:
            published, published_source = parsed, source_name
            break

    titles = []
    if title:
        titles.append({"kind": "visible_h1", "source": title_source, "raw": title})
    for node, kind, source_name in (
        (soup.select_one('meta[property="og:title"]'), "og_title", 'meta[property="og:title"]'),
        (soup.select_one('meta[name="twitter:title"]'), "twitter_title", 'meta[name="twitter:title"]'),
    ):
        raw = clean(node.get("content")) if node else None
        if raw and all(item["raw"] != raw for item in titles):
            titles.append({"kind": kind, "source": source_name, "raw": raw})
    if article_ld:
        raw = clean(article_ld.get("headline"))
        if raw and all(item["raw"] != raw for item in titles):
            titles.append({"kind": "jsonld_headline", "source": "jsonld.headline", "raw": raw})

    dates = []
    for raw, kind, source_name in (
        (ld_raw, "published", "jsonld.datePublished"),
        (meta_raw, "published", "meta.datePublished"),
        *[(raw, "visible_time", selector) for raw, selector in visible],
    ):
        if raw and all(item["raw"] != raw for item in dates):
            dates.append({"kind": kind, "source": source_name, "raw": raw})
    bodies = []
    evidence_body, evidence_source = _body(
        soup, evidence_body_selectors, preserve_figures=preserve_figures,
    )
    if evidence_body and len(evidence_body) >= 120:
        bodies.append({"kind": evidence_source or "page_body", "text": evidence_body[:20000]})
    if not bodies and body:
        bodies.append({"kind": body_source or "selected_body", "text": body[:20000]})
    publisher_videos = []
    for node in soup.select("video, iframe[src*='youtube.com'], iframe[src*='youtu.be'], iframe[src*='vimeo.com']"):
        signal = clean(node.get("src") or node.get("data-src") or node.name)
        if signal and signal not in publisher_videos:
            publisher_videos.append(signal)

    return {
        "url": url,
        "source_domain": source,
        "country_code": "ARM",
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": language,
        "parser": "pure_adapter",
        "collection": None,
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
            "publisher_videos": publisher_videos[:10],
            "http_status": response.status_code,
        },
    }
