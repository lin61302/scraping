"""Read-only Eco Nordeste news observer using its archive and WordPress API."""
from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from dateutil.tz import gettz

from adapters.BRA.common import clean, fetch, normalize_url


class Config:
    source_name = "agenciaeconordeste.com.br"
    country_code = "BRA"
    is_env = True


BASE = "https://agenciaeconordeste.com.br"
API = f"{BASE}/wp-json/wp/v2/posts"
LISTING = f"{BASE}/ultimas-noticias/"
PAGE_SIZE = 100
SAO_PAULO = gettz("America/Sao_Paulo")
if SAO_PAULO is None:
    raise RuntimeError("America/Sao_Paulo timezone unavailable")

_api_posts: dict[str, dict] = {}


def _months(start: date, end: date):
    month = date(start.year, start.month, 1)
    while month <= end:
        next_month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        yield month, next_month
        month = next_month


def _api_month(month: date, next_month: date, evidence: dict) -> dict[str, dict]:
    params = {
        "after": (datetime.combine(month, datetime.min.time()) - timedelta(seconds=1)).isoformat(),
        "before": datetime.combine(next_month, datetime.min.time()).isoformat(),
        "per_page": PAGE_SIZE, "orderby": "date", "order": "asc",
        "_fields": "id,date,date_gmt,link,title,content",
    }
    for attempt in range(2):
        result = {}
        rows = []
        page = 1
        total = pages = None
        while True:
            response = fetch(f"{API}?{urlencode({**params, 'page': page})}")
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"API returned non-list for {month} page {page}")
            try:
                page_total = int(response.headers["X-WP-Total"])
                page_pages = int(response.headers["X-WP-TotalPages"])
            except (KeyError, ValueError, TypeError) as exc:
                raise RuntimeError(f"API pagination headers unavailable for {month}") from exc
            if total is None:
                total, pages = page_total, page_pages
            rows.append({"page": page, "count": len(payload), "total": page_total, "pages": page_pages})
            for item in payload:
                if not isinstance(item, dict) or not item.get("date") or not item.get("link") or not item.get("id"):
                    raise RuntimeError(f"API invalid post for {month}")
                url = normalize_url(item["link"], Config.source_name)
                if not url or url in result or not month <= datetime.fromisoformat(item["date"]).date() < next_month:
                    raise RuntimeError(f"API duplicate/out-of-month post: {item.get('link')!r}")
                result[url] = item
            if page >= max(1, pages):
                break
            page += 1
        if len(result) == total and pages == math.ceil(total / PAGE_SIZE) and all(
            row["total"] == total and row["pages"] == pages for row in rows
        ):
            evidence["api_months"].append({"month": month.strftime("%Y-%m"), "total": total, "pages": rows, "attempt": attempt + 1})
            return result
    raise RuntimeError(f"API unstable pagination for {month}: {rows}")


def _listing_page(page: int) -> tuple[dict[str, datetime], str]:
    url = LISTING if page == 1 else f"{LISTING}page/{page}/"
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    cards = soup.select("article.card-news.type-post")
    if not cards:
        raise RuntimeError(f"news listing has no post cards: {url}")
    posts = {}
    for card in cards:
        title = card.select_one("h2.title-news a[href]")
        timestamp = card.select_one("time.entry-date[datetime]")
        if not title or not timestamp:
            raise RuntimeError(f"news card missing URL/date: {url}")
        article_url = normalize_url(title.get("href"), Config.source_name)
        try:
            published = datetime.fromisoformat(timestamp["datetime"])
        except ValueError as exc:
            raise RuntimeError(f"news card has invalid date: {url}") from exc
        if not article_url or article_url in posts:
            raise RuntimeError(f"news card invalid/duplicate URL: {url}")
        posts[article_url] = published.astimezone(SAO_PAULO).replace(tzinfo=None) if published.tzinfo else published
    return posts, url


def _listing_window(start: date, end: date, evidence: dict) -> dict[str, datetime]:
    found = {}
    page = 1
    previous_last = None
    crossed = False
    evidence["listing_pages"] = []
    while page <= 300:
        posts, url = _listing_page(page)
        values = list(posts.values())
        if any(left < right for left, right in zip(values, values[1:])):
            raise RuntimeError(f"news listing is not in descending publication order: {url}")
        if previous_last is not None and values[0] > previous_last:
            raise RuntimeError(f"news listing page order broke between pages: {url}")
        previous_last = values[-1]
        evidence["listing_pages"].append({
            "page": page, "url": url, "cards": len(posts),
            "newest": values[0].isoformat(), "oldest": values[-1].isoformat(),
        })
        for article_url, published in posts.items():
            if article_url in found:
                raise RuntimeError(f"news listing repeated URL across pages: {article_url}")
            if start <= published.date() <= end:
                found[article_url] = published
        if values[-1].date() < start:
            if crossed:
                evidence["listing_lower_boundary_confirmed"] = True
                return found
            crossed = True
        page += 1
    raise RuntimeError("news listing did not cross lower publication boundary within 300 pages")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end:
        raise ValueError("start must be on or before end")
    _api_posts.clear()
    evidence.update({
        "discovery": "publisher_wordpress_posts_api_plus_chronological_ultimas_noticias_listing",
        "api_endpoint": API, "listing_endpoint": LISTING,
        "publisher_sitemap_status": "robots.txt advertises /sitemap.xml and /sitemap.html; both returned HTTP 404 during 2026-09-17 probe",
        "api_months": [], "coverage_complete": False,
    })
    api = {}
    for month, next_month in _months(start, end):
        api.update(_api_month(month, next_month, evidence))
    _api_posts.update(api)
    api_window = {
        url for url, item in api.items()
        if start <= datetime.fromisoformat(item["date"]).date() <= end
    }
    listing = _listing_window(start, end, evidence)
    evidence["api_window_count"] = len(api_window)
    evidence["listing_window_count"] = len(listing)
    evidence["api_missing_from_listing"] = sorted(api_window - set(listing))
    evidence["listing_missing_from_api"] = sorted(set(listing) - api_window)
    evidence["api_listing_date_mismatches"] = sorted(
        url for url in api_window & set(listing)
        if datetime.fromisoformat(api[url]["date"]).date() != listing[url].date()
    )
    evidence["coverage_complete"] = bool(evidence.get("listing_lower_boundary_confirmed")) and not any((
        evidence["api_missing_from_listing"], evidence["listing_missing_from_api"],
        evidence["api_listing_date_mismatches"],
    ))
    return sorted(api_window | set(listing), key=lambda url: (api.get(url, {}).get("date", ""), url))


def _body(soup: BeautifulSoup) -> tuple[str | None, dict]:
    container = soup.select_one("main#content article.type-post .entry-content")
    if not container:
        return None, {"body_container_found": False}
    clone = BeautifulSoup(str(container), "html.parser")
    for bad in clone.select("script, style, iframe, form, figure, figcaption, .entry-links, .share, .related"):
        bad.decompose()
    lines = []
    skipped = []
    for block in clone.select("p, h2, h3"):
        value = clean(block.get_text(" ", strip=True))
        if not value:
            continue
        if re.match(r"^(?:Leia também:|Seja um apoiador mensal|Quero me inscrever)", value, re.I):
            skipped.append(value[:160])
            continue
        if value not in lines:
            lines.append(value)
    return "\n\n".join(lines) or None, {"body_container_found": True, "blocks_kept": len(lines), "skipped": skipped}


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title_node = soup.select_one("main#content article.type-post h1.title-news")
    time_node = soup.select_one("main#content article.type-post time.entry-date[itemprop='datePublished']")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    raw_time = clean(time_node.get("datetime")) if time_node else None
    published = None
    try:
        parsed = datetime.fromisoformat(raw_time) if raw_time else None
        if parsed:
            published = parsed.astimezone(SAO_PAULO).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    maintext, body_cleanup = _body(soup)
    og = soup.select_one('meta[property="og:title"]')
    og_title = clean(og.get("content")) if og else None
    api = _api_posts.get(url)
    errors = []
    if not published:
        errors.append("visible_publication_missing")
    if api and published:
        api_date = datetime.fromisoformat(api["date"])
        if abs((published - api_date).total_seconds()) > 120:
            errors.append("visible_vs_api_publication_disagreement")
        if api.get("date_gmt"):
            utc_local = datetime.fromisoformat(api["date_gmt"]).replace(tzinfo=gettz("UTC")).astimezone(SAO_PAULO).replace(tzinfo=None)
            if abs((published - utc_local).total_seconds()) > 120:
                errors.append("visible_vs_api_gmt_disagreement")
    if title and og_title and SequenceMatcher(None, title.casefold(), og_title.casefold()).ratio() < .78:
        errors.append("visible_vs_og_title_disagreement")
    if api and title:
        api_title = clean(BeautifulSoup(html.unescape(api.get("title", {}).get("rendered", "")), "html.parser").get_text(" ", strip=True))
        if api_title and SequenceMatcher(None, title.casefold(), api_title.casefold()).ratio() < .78:
            errors.append("visible_vs_api_title_disagreement")
    api_content = api.get("content", {}) if api else {}
    api_body = clean(BeautifulSoup(api_content.get("rendered", ""), "html.parser").get_text(" ", strip=True)) if isinstance(api_content, dict) else None
    if isinstance(api_content, dict) and api_content.get("protected"):
        errors.append("api_content_protected")
    if maintext and api_body:
        page_words = set(re.findall(r"\w+", maintext.casefold()))
        api_words = set(re.findall(r"\w+", api_body.casefold()))
        if len(page_words & api_words) / max(1, min(len(page_words), len(api_words))) < .55:
            errors.append("page_vs_api_maintext_disagreement")
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "date_publish": published, "maintext": maintext,
        "language": "pt", "parser": "pure_adapter", "collection": None,
        "parser_error": ";".join(errors) or None,
        "field_sources": {"title": "article.type-post h1.title-news", "date_publish": "time.entry-date[itemprop=datePublished]", "maintext": "article.type-post .entry-content"},
        "field_evidence": {
            "titles": ([{"kind": "visible_h1", "raw": title, "source": "article.type-post h1.title-news"}] if title else []) + ([{"kind": "og_title", "raw": og_title, "source": "meta.og:title"}] if og_title else []),
            "dates": ([{"kind": "visible_time", "raw": raw_time, "source": "time.entry-date[itemprop=datePublished]"}] if raw_time else []) + ([{"kind": "published", "raw": api["date"], "source": "wp-json.post.date"}] if api else []),
            "bodies": ([{"kind": "wordpress_content", "source": "wp-json.post.content.rendered", "text": api_body[:20000]}] if api_body else []) + ([{"kind": "rendered_article_body", "text": maintext[:20000]}] if maintext else []),
            "http_status": response.status_code, "body_cleanup": body_cleanup,
            "api_publication_raw": api.get("date") if api else None,
            "publisher_response_sha256": hashlib.sha256(response.content).hexdigest(),
            "publisher_response_bytes": len(response.content),
        },
    }
