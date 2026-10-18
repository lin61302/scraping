"""Network-only helpers for Zimbabwe observation adapters.

The helpers deliberately contain no database client, duplicate lookup, or
production write path. Sitemap and listing timestamps are discovery signals;
article pages remain authoritative for publication date whenever they expose
an absolute publication timestamp.
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import date, datetime
from html import unescape
from typing import Iterable, Iterator, Optional
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse

import dateparser
import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MLP-MLEED-observation/1.0; +https://mlp.org)"
}
MEDIA = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)(?:\?|$)", re.I)
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|checking your browser|just a moment|verify you are human",
    re.I,
)


def clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result or None


def _challenge(response: requests.Response) -> bool:
    head = response.text[:15000]
    match = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    return bool(match and CHALLENGE.search(clean(match.group(1)) or ""))


def _candidate_urls(url: str) -> list[str]:
    """Try the publisher's HTTP redirect before canonical HTTPS.

    ZimLive intermittently challenges direct HTTPS requests while allowing its
    own HTTP-to-HTTPS redirect. Other publishers simply redirect normally.
    """
    parsed = urlparse(url)
    result = [url]
    if parsed.scheme == "https":
        result.insert(0, urlunparse(("http", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)))
    if parsed.hostname and parsed.hostname.startswith("www."):
        bare = parsed.netloc[4:]
        result.append(urlunparse(("http", bare, parsed.path, parsed.params, parsed.query, parsed.fragment)))
    return list(dict.fromkeys(result))


def fetch(url: str, timeout: int = 35) -> requests.Response:
    errors = []
    candidates = _candidate_urls(url)
    if "zimlive.com" in (urlparse(url).hostname or ""):
        parsed = urlparse(url)
        for host in ("zimlive.com", "www.zimlive.com"):
            for scheme in ("http", "https"):
                candidates.append(urlunparse((scheme, host, parsed.path, parsed.params, parsed.query, parsed.fragment)))
        candidates = list(dict.fromkeys(candidates))
    user_agents = (HEADERS["User-Agent"], "Googlebot")
    attempts = max(4, len(candidates) * 2 if "zimlive.com" in (urlparse(url).hostname or "") else 4)
    for attempt in range(attempts):
        candidate = candidates[attempt % len(candidates)]
        session = requests.Session()
        session.headers.update({"User-Agent": user_agents[attempt % len(user_agents)]})
        try:
            response = session.get(candidate, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            if not response.content:
                raise ValueError("empty response")
            if _challenge(response):
                raise ValueError("challenge response")
            return response
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
            if attempt < attempts - 1:
                time.sleep(0.4 + random.random() * 0.3)
    raise RuntimeError(f"fetch failed for {url}: {'; '.join(errors)}")


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
    for pattern in ("%d %b %Y at %H:%Mhrs", "%d.%m.%Y %H:%M", "%B %d, %Y"):
        try:
            return datetime.strptime(value.split("|")[0].strip(), pattern)
        except ValueError:
            pass
    # Relative labels depend on the request clock and cannot be durable field
    # evidence. Callers may use an exact sitemap timestamp as an explicit
    # fallback for those pages.
    if re.search(r"\b(?:mins?|minutes?|hrs?|hours?|days?)\s+ago\b", value, re.I):
        return None
    order = "YMD" if re.match(r"^20\d{2}[-/.]", value) else "DMY"
    parsed = dateparser.parse(
        value,
        languages=["en"],
        settings={
            "DATE_ORDER": order,
            "PREFER_LOCALE_DATE_ORDER": False,
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    return parsed.replace(tzinfo=None) if parsed else None


def sitemap_rows(soup: BeautifulSoup) -> list[tuple[str, Optional[datetime]]]:
    nodes = soup.find_all("sitemap") if soup.find("sitemapindex") else soup.find_all("url")
    rows = []
    for node in nodes:
        loc = node.find("loc", recursive=False)
        if loc is None:
            continue
        lastmod = node.find("lastmod", recursive=False)
        rows.append((loc.get_text(strip=True), parse_datetime(lastmod.get_text(strip=True)) if lastmod else None))
    return rows


def normalize_url(raw: str, domain: str, deny: Iterable[str] = ()) -> Optional[str]:
    try:
        parsed = urlparse(urljoin(f"https://{domain}/", raw))
        host = (parsed.hostname or "").lower()
        if host not in {domain, f"www.{domain}"}:
            return None
        if not parsed.path or parsed.path == "/" or MEDIA.search(parsed.path):
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
        if types & {"Article", "NewsArticle", "ReportageNewsArticle"}:
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
            ".author, [class*='related'], [class*='recommend']"
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
        # Bulawayo24's story container is plain text around break tags.
        text = clean(node.get_text("\n", strip=True))
        if text and len(text) >= 100:
            return text, selector
    return None, None


def wordpress_rest_article(url: str, *, source: str, post: Optional[dict] = None) -> tuple[dict, list[dict]]:
    """Read canonical fields from a publisher's first-party WordPress API."""
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    if not slug:
        raise ValueError(f"article URL has no slug: {url}")
    response = None
    retrieval_url = None
    if post is None:
        api_urls = [
            f"http://{source}/wp-json/wp/v2/posts?slug={quote(slug)}&_embed=wp:term",
            f"http://{source}/?rest_route=/wp/v2/posts&slug={quote(slug)}&_embed=wp:term",
        ]
        errors = []
        for api_url in api_urls:
            try:
                response = fetch(api_url)
                break
            except Exception as exc:
                errors.append(f"{api_url}: {exc}")
        if response is None:
            raise RuntimeError(f"WordPress API fetch failed for {url}: {'; '.join(errors)}")
        try:
            rows = response.json()
        except ValueError as exc:
            raise RuntimeError(f"WordPress API did not return JSON for {url}") from exc
        if not isinstance(rows, list) or len(rows) != 1:
            count = len(rows) if isinstance(rows, list) else "non-list"
            raise RuntimeError(f"WordPress API returned {count} posts for {url}")
        post = rows[0]
        retrieval_url = response.url
    else:
        retrieval_url = post.get("_discovery_url")
    if post.get("slug") != slug:
        raise RuntimeError(f"WordPress API slug mismatch for {url}: {post.get('slug')}")

    title_html = (post.get("title") or {}).get("rendered") or ""
    title = clean(BeautifulSoup(unescape(title_html), "html.parser").get_text(" ", strip=True))
    content_obj = post.get("content") or {}
    content_html = content_obj.get("rendered") or ""
    content_soup = BeautifulSoup(f"<article>{content_html}</article>", "html.parser")
    body, _ = article_body(content_soup, ("article",))
    published_raw = clean(post.get("date"))
    published = parse_datetime(published_raw)

    terms = []
    for group in (post.get("_embedded") or {}).get("wp:term", []):
        if isinstance(group, list):
            terms.extend(item for item in group if isinstance(item, dict))
    categories = [item for item in terms if item.get("taxonomy") == "category"]
    category_names = [clean(item.get("name")) for item in categories if clean(item.get("name"))]
    article = {
        "url": url,
        "source_domain": source,
        "country_code": "ZWE",
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": "en",
        "parser": "pure_adapter_wordpress_rest",
        "collection": None,
        "parser_error": None,
        "content_protected": bool(content_obj.get("protected")),
        "content_truncated": bool(post.get("amh_truncated")),
        "page_categories": category_names,
        "field_sources": {
            "title": "wp_rest.title.rendered",
            "date_publish": "wp_rest.date",
            "maintext": "wp_rest.content.rendered",
        },
        "field_evidence": {
            "titles": [{"kind": "wp_rest_title", "source": "wp_rest.title.rendered", "raw": title}] if title else [],
            "dates": [{"kind": "published", "source": "wp_rest.date", "raw": published_raw}] if published_raw else [],
            "bodies": [{"kind": "wp_rest_content", "text": body[:20000]}] if body else [],
            "http_status": response.status_code if response is not None else 200,
            "retrieval_url": retrieval_url,
            "post_id": post.get("id"),
        },
    }
    return article, categories


def wordpress_rest_discovery(
    source: str,
    start: date,
    end: date,
    evidence: dict,
    *,
    per_page: int = 100,
    categories: Optional[list[int]] = None,
    canonical_domain: Optional[str] = None,
    post_cache: Optional[dict[str, dict]] = None,
    embed_terms: bool = False,
) -> list[str]:
    """Page through a publisher's exact publication-date API interval."""
    params = {
        "after": f"{start.isoformat()}T00:00:00",
        "before": f"{end.isoformat()}T23:59:59",
        "per_page": per_page,
        "orderby": "date",
        "order": "desc",
    }
    if categories:
        params["categories"] = ",".join(str(item) for item in categories)
    if embed_terms:
        params["_embed"] = "wp:term"
    if post_cache is not None:
        post_cache.clear()
    api_root = f"http://{source}/wp-json/wp/v2/posts"
    evidence.update({
        "discovery_endpoint": api_root,
        "publication_date_window": {"start": start.isoformat(), "end": end.isoformat()},
        "pages_checked": [],
        "page_failures": [],
        "category_ids": categories or [],
        "canonical_domain": canonical_domain or source,
    })
    result = []
    seen = set()
    page = 1
    expected_total = None
    expected_pages = None
    while page <= 200:
        page_url = f"{api_root}?{urlencode({**params, 'page': page})}"
        try:
            response = fetch(page_url)
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("WordPress API did not return a list")
        except Exception as exc:
            evidence["page_failures"].append({"page": page, "url": page_url, "error": str(exc)})
            break
        if expected_total is None:
            expected_total = int(response.headers.get("X-WP-Total", len(rows)))
            expected_pages = int(response.headers.get("X-WP-TotalPages", 1))
            evidence["publisher_total"] = expected_total
            evidence["publisher_pages"] = expected_pages
        evidence["pages_checked"].append({
            "page": page,
            "url": page_url,
            "rows": len(rows),
            "newest": rows[0].get("date") if rows else None,
            "oldest": rows[-1].get("date") if rows else None,
        })
        for post in rows:
            published = parse_datetime(post.get("date"))
            if published is None or not (start <= published.date() <= end):
                raise RuntimeError(f"WordPress API returned an out-of-range publication date on page {page}")
            raw_url = post.get("link") or ""
            if (urlparse(raw_url).hostname or "").lower() not in {
                source, f"www.{source}", canonical_domain or source,
            }:
                raw_url = (post.get("yoast_head_json") or {}).get("canonical") or raw_url
            url = normalize_url(raw_url, canonical_domain or source)
            if not url:
                raise RuntimeError(f"WordPress API returned a non-source article URL on page {page}")
            if url not in seen:
                seen.add(url)
                result.append(url)
                if post_cache is not None:
                    cached = dict(post)
                    cached["_discovery_url"] = page_url
                    post_cache[url] = cached
        if page >= expected_pages:
            break
        page += 1
    if evidence["page_failures"]:
        raise RuntimeError(f"{len(evidence['page_failures'])} WordPress API pages failed")
    if expected_total is None or len(result) != expected_total:
        raise RuntimeError(f"WordPress API total mismatch: expected {expected_total}, unique {len(result)}")
    evidence["discovered_urls_after_filter"] = len(result)
    evidence["boundary_reached"] = True
    return result


def extract_article(
    url: str,
    *,
    source: str,
    body_selectors: Iterable[str],
    visible_date_selectors: Iterable[str],
    title_selectors: Iterable[str] = ("article h1", "main h1", "h1"),
    evidence_body_selectors: Iterable[str] = ("article", "main"),
    fallback_date: Optional[datetime] = None,
) -> tuple[dict, BeautifulSoup]:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_source = first_text(soup, title_selectors)
    body, body_source = article_body(soup, body_selectors)
    article_ld = _jsonld_article(soup)

    visible_candidates = []
    for selector in visible_date_selectors:
        for node in soup.select(selector)[:4]:
            raw = clean(node.get("datetime") or node.get("content") or node.get_text(" ", strip=True))
            if raw and all(existing[0] != raw for existing in visible_candidates):
                visible_candidates.append((raw, selector))

    ld_raw = clean(article_ld.get("datePublished")) if article_ld else None
    meta = soup.select_one(
        'meta[property="article:published_time"], meta[itemprop="datePublished"], meta[name="datePublished"]'
    )
    meta_raw = clean(meta.get("content") or meta.get("datetime")) if meta else None
    published = None
    published_source = None
    for raw, source_name in [
        *[(raw, selector) for raw, selector in visible_candidates],
        (ld_raw, "jsonld.datePublished"),
        (meta_raw, "meta.datePublished"),
    ]:
        parsed = parse_datetime(raw)
        if parsed:
            published, published_source = parsed, source_name
            break
    if published is None and fallback_date is not None:
        published, published_source = fallback_date.replace(tzinfo=None), "discovery.sitemap_lastmod"

    titles = []
    if title:
        titles.append({"kind": "visible_h1", "source": title_source, "raw": title})
    for raw, kind, source_name in (
        (clean(soup.select_one('meta[property="og:title"]').get("content")) if soup.select_one('meta[property="og:title"]') else None, "og_title", 'meta[property="og:title"]'),
        (clean(article_ld.get("headline")) if article_ld else None, "jsonld_headline", "jsonld.headline"),
    ):
        if raw and all(item["raw"] != raw for item in titles):
            titles.append({"kind": kind, "source": source_name, "raw": raw})

    dates = []
    visible_date_evidence = []
    for raw, selector in visible_candidates:
        parsed = parse_datetime(raw)
        if parsed:
            # Normalize DMY page labels before the generic evidence checker;
            # retain the literal page value for human review.
            visible_date_evidence.append((parsed.isoformat(), "visible_time", selector, raw))
    for raw, kind, source_name, display_raw in [
        *visible_date_evidence,
        (ld_raw, "published", "jsonld.datePublished", None),
        (meta_raw, "published", "meta.datePublished", None),
        (fallback_date.isoformat() if fallback_date else None, "created", "discovery.sitemap_lastmod", None),
    ]:
        if raw and all(item["raw"] != raw for item in dates):
            item = {"kind": kind, "source": source_name, "raw": raw}
            if display_raw:
                item["display_raw"] = display_raw
            dates.append(item)

    bodies = []
    ld_body = clean(article_ld.get("articleBody")) if article_ld else None
    if ld_body and len(ld_body) >= 200:
        bodies.append({"kind": "jsonld_articleBody", "text": ld_body[:20000]})
    evidence_body, evidence_body_source = article_body(soup, evidence_body_selectors)
    if evidence_body and len(evidence_body) >= 200:
        bodies.append({"kind": evidence_body_source or "page_body", "text": evidence_body[:20000]})
    if not bodies and body:
        bodies.append({"kind": body_source or "selected_body", "text": body[:20000]})

    article = {
        "url": url,
        "source_domain": source,
        "country_code": "ZWE",
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": "en",
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
            "http_status": response.status_code,
        },
    }
    return article, soup
