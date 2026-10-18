"""Read-only Agência Pública Socioambiental observation adapter.

The legacy collector used a fixed 41-page archive scan. This adapter bounds
the archive by original publication date and reconciles it with every public
REST content type that carries the publisher's ``editorial`` taxonomy. It has
no database import and does no work at import time.
"""
from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup
from dateutil.tz import gettz

from adapters.BRA.common import clean, fetch, jsonld_objects, normalize_url


class Config:
    source_name = "apublica.org"
    country_code = "BRA"
    is_env = True


BASE = "https://apublica.org"
ARCHIVE = BASE + "/editorial/socioambiental/"
API = BASE + "/wp-json/wp/v2"
PAGE_SIZE = 100
MAX_DAYS = 180
MAX_ARCHIVE_PAGES = 80
MAX_API_PAGES = 30
MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}
VISIBLE_DATE = re.compile(
    r"\b(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(20\d{2})(?:\s+(\d{1,2}):(\d{2}))?\b",
    re.I,
)
ARTICLE_ID = re.compile(r"^post-(\d+)$")
API_POSTS: dict[str, dict] = {}
ARCHIVE_DATES: dict[str, date] = {}
ZONE = gettz("America/Sao_Paulo")
if ZONE is None:
    raise RuntimeError("America/Sao_Paulo timezone is unavailable")


def _url(raw: str) -> str | None:
    value = normalize_url(raw, Config.source_name)
    if not value:
        return None
    path = urlparse(value).path
    if path.startswith(("/editorial/", "/tag/", "/autor/", "/arquivo/", "/wp-", "/category/")):
        return None
    return value


def _published(raw: str | None) -> datetime | None:
    if not raw:
        return None
    match = VISIBLE_DATE.search(raw)
    if not match:
        return None
    day, month, year, hour, minute = match.groups()
    try:
        return datetime(int(year), MONTHS[month.casefold()], int(day), int(hour or 0), int(minute or 0))
    except (KeyError, ValueError):
        return None


def _api_datetime(raw: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _term(evidence: dict) -> int:
    response = fetch(f"{API}/editorial?slug=socioambiental")
    items = response.json()
    if not isinstance(items, list) or len(items) != 1 or items[0].get("slug") != "socioambiental":
        raise ValueError("Socioambiental editorial REST term is ambiguous or absent")
    term = items[0]
    if urlparse(term.get("link", "")).path.rstrip("/") != urlparse(ARCHIVE).path.rstrip("/"):
        raise ValueError("REST Socioambiental term points away from the checked archive")
    evidence["editorial_term"] = {"id": term["id"], "slug": term["slug"], "link": term["link"]}
    return int(term["id"])


def _content_types(evidence: dict) -> list[tuple[str, str, str]]:
    response = fetch(f"{API}/types")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("WordPress content type inventory is not an object")
    types = sorted(
        (name, item.get("rest_namespace", "wp/v2"), item["rest_base"])
        for name, item in payload.items()
        if isinstance(item, dict) and "editorial" in item.get("taxonomies", [])
        and isinstance(item.get("rest_base"), str)
    )
    if not types or len(types) > 20:
        raise ValueError("WordPress editorial content type inventory is absent or excessive")
    evidence["editorial_content_types"] = [dict(type=name, rest_namespace=namespace, rest_base=base)
                                            for name, namespace, base in types]
    return types


def _api_inventory(start: date, end: date, term: int, types: list[tuple[str, str, str]], evidence: dict) -> dict[str, dict]:
    # Date bounds use the site's local wall clock. A one-day overlap on both
    # sides protects midnight behavior; page `date` decides final inclusion.
    params = {
        "editorial": term, "after": f"{start - timedelta(days=1)}T23:59:59",
        "before": f"{end + timedelta(days=1)}T00:00:00",
        "per_page": PAGE_SIZE, "orderby": "date", "order": "desc",
    }
    found: dict[str, dict] = {}
    evidence["api_types"] = []
    for content_type, namespace, rest_base in types:
        total = pages = None
        rows = []
        page_evidence = []
        for page in range(1, MAX_API_PAGES + 1):
            endpoint = f"{BASE}/wp-json/{namespace}/{rest_base}?{urlencode({**params, 'page': page})}"
            response = fetch(endpoint)
            items = response.json()
            if not isinstance(items, list):
                raise ValueError(f"WordPress {content_type} page {page} did not return a list")
            try:
                header_total = int(response.headers["X-WP-Total"])
                header_pages = int(response.headers["X-WP-TotalPages"])
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"WordPress {content_type} pagination totals are missing") from exc
            if total is None:
                total, pages = header_total, header_pages
                if pages != math.ceil(total / PAGE_SIZE) or pages > MAX_API_PAGES:
                    raise ValueError(f"WordPress {content_type} pagination count is inconsistent or excessive")
            if (header_total, header_pages) != (total, pages):
                raise ValueError(f"WordPress {content_type} count changed during pagination")
            page_evidence.append({"page": page, "count": len(items), "headers_total": header_total,
                                  "headers_pages": header_pages, "sha256": hashlib.sha256(response.content).hexdigest()})
            rows.extend(items)
            if page >= max(1, pages):
                break
        ids = [item.get("id") for item in rows if isinstance(item, dict)]
        if len(rows) != total or len(set(ids)) != len(rows):
            raise ValueError(f"WordPress {content_type} inventory has missing or duplicated IDs")
        in_window = 0
        for item in rows:
            published = _api_datetime(item.get("date"))
            url = _url(item.get("link", ""))
            editorial = item.get("editorial")
            if not published or not isinstance(editorial, list) or not url:
                raise ValueError(f"WordPress {content_type} row lacks date, editorial terms, or canonical URL")
            if term not in editorial:
                raise ValueError(f"WordPress {content_type} ignored the editorial taxonomy filter")
            if start <= published.date() <= end:
                if url in found:
                    raise ValueError(f"WordPress API repeated a URL across content types: {url}")
                found[url] = item
                in_window += 1
        evidence["api_types"].append({"type": content_type, "rest_namespace": namespace, "rest_base": rest_base,
                                      "total_with_boundary_overlap": total, "in_window": in_window,
                                      "pages": page_evidence})
    evidence["api_window_urls"] = len(found)
    return found


def _archive_cards(soup: BeautifulSoup, page: int) -> list[tuple[str, date | None, int | None]]:
    # The publisher inserts a newsletter CTA as <article id="archive-cta">
    # inside the same brick grid; it has no permalink and is not a story.
    cards = soup.select("div.brick article:not(#archive-cta)")
    if not cards:
        raise ValueError(f"Socioambiental archive page {page} has no article cards")
    result = []
    for card in cards:
        link = card.select_one("a.stretched-link[href]")
        url = _url(link.get("href", "")) if link else None
        if not url or "editorial-socioambiental" not in card.get("class", []):
            raise ValueError(f"Socioambiental archive page {page} has an unrecognized card URL or taxonomy")
        raw = card.select_one(".post-date-actual")
        published = _published(raw.get_text(" ", strip=True)).date() if raw and _published(raw.get_text(" ", strip=True)) else None
        match = ARTICLE_ID.match(card.get("id", ""))
        result.append((url, published, int(match.group(1)) if match else None))
    if len({row[0] for row in result}) != len(result):
        raise ValueError(f"Socioambiental archive page {page} repeats a URL")
    return result


def _article_page_date(url: str) -> date | None:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    node = soup.select_one("div.col-12.col-md-3.col-lg-2.text-md-end.fst-italic")
    value = _published(node.get_text(" ", strip=True) if node else None)
    return value.date() if value else None


def _archive_inventory(start: date, end: date, api: dict[str, dict], evidence: dict) -> tuple[dict[str, date], bool]:
    seen = set()
    in_window: dict[str, date] = {}
    evidence["archive_pages"] = []
    previous_date: date | None = None
    lower_reached = False
    for page in range(1, MAX_ARCHIVE_PAGES + 1):
        url = ARCHIVE if page == 1 else f"{ARCHIVE}page/{page}/"
        response = fetch(url)
        soup = BeautifulSoup(response.content, "html.parser")
        cards = _archive_cards(soup, page)
        dated = []
        for card_url, card_date, article_id in cards:
            if card_url in seen:
                raise ValueError(f"Socioambiental archive shifted or duplicated URLs: {card_url}")
            seen.add(card_url)
            api_post = api.get(card_url)
            api_date = _api_datetime(api_post.get("date")).date() if api_post else None
            published = card_date or api_date or _article_page_date(card_url)
            if not published:
                raise ValueError(f"Socioambiental archive card has no original publication date: {card_url}")
            if card_date and api_date and card_date != api_date:
                raise ValueError(f"Socioambiental card and API disagree on publication date: {card_url}")
            if api_post and article_id != api_post.get("id"):
                raise ValueError(f"Socioambiental card and API post ID disagree: {card_url}")
            if previous_date and published > previous_date:
                raise ValueError(f"Socioambiental archive is not descending at {card_url}")
            previous_date = published
            dated.append(published)
            if start <= published <= end:
                in_window[card_url] = published
                ARCHIVE_DATES[card_url] = published
        next_url = f"{ARCHIVE}page/{page + 1}/"
        has_next = any(link.get("href", "").rstrip("/") == next_url.rstrip("/") for link in soup.select("a[href]"))
        evidence["archive_pages"].append({
            "url": url, "sha256": hashlib.sha256(response.content).hexdigest(),
            "bytes": len(response.content), "cards": len(cards), "first_date": dated[0].isoformat(),
            "last_date": dated[-1].isoformat(), "next_link": has_next,
        })
        if dated[-1] < start:
            lower_reached = True
            break
        if not has_next:
            raise ValueError("Socioambiental archive ended before the lower date boundary")
    evidence["archive_lower_boundary_reached"] = lower_reached
    evidence["archive_window_urls"] = len(in_window)
    return in_window, lower_reached


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > MAX_DAYS:
        raise ValueError(f"invalid Agência Pública date window (max {MAX_DAYS + 1} days)")
    API_POSTS.clear()
    ARCHIVE_DATES.clear()
    evidence.update({"method": "Socioambiental archive reconciled with count-checked REST taxonomy inventory",
                     "archive": ARCHIVE, "coverage_complete": False})
    term = _term(evidence)
    types = _content_types(evidence)
    api = _api_inventory(start, end, term, types, evidence)
    archive, lower = _archive_inventory(start, end, api, evidence)
    api_urls = set(api)
    archive_urls = set(archive)
    missing_archive = sorted(api_urls - archive_urls)
    missing_api = sorted(archive_urls - api_urls)
    evidence["api_urls_missing_from_archive"] = missing_archive
    evidence["archive_urls_missing_from_api"] = missing_api
    evidence["coverage_complete"] = lower and not missing_archive and not missing_api
    API_POSTS.update(api)
    return sorted(api_urls | archive_urls)


def _maintext(soup: BeautifulSoup) -> tuple[str | None, list[str]]:
    node = soup.select_one("article div.entry-content") or soup.select_one("div.entry-content")
    if node is None:
        return None, []
    copy = BeautifulSoup(str(node), "html.parser")
    for bad in copy.select("script, style, iframe, audio, video, form, nav, aside, figure, figcaption, .single-podcast-player, .newsletter, .share, .social, .related, [class*='newsletter'], [class*='related']"):
        bad.decompose()
    paragraphs = []
    for item in copy.select("p, h2, h3, blockquote"):
        if item.name == "blockquote" and item.select("p"):
            continue
        if item.find_parent("blockquote") and item.name != "blockquote":
            continue
        value = clean(item.get_text(" ", strip=True))
        if not value or value in paragraphs:
            continue
        if value.casefold().startswith(("quer receber os textos desta coluna", "assine a newsletter", "veja mais episódios")):
            continue
        paragraphs.append(value)
    return "\n\n".join(paragraphs) or None, paragraphs


def _ld_published(soup: BeautifulSoup) -> tuple[datetime | None, str | None]:
    for item in jsonld_objects(soup):
        types = item.get("@type")
        types = types if isinstance(types, list) else [types]
        if not ({"NewsArticle", "Article", "PodcastEpisode", "WebPage"} & set(types)):
            continue
        raw = item.get("datePublished")
        value = _api_datetime(raw)
        if value:
            return value.astimezone(ZONE).replace(tzinfo=None) if value.tzinfo else value, raw
    return None, None


def scrape_url(url: str) -> dict:
    if _url(url) != url:
        raise ValueError(f"not a canonical Agência Pública article URL: {url}")
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title_node = soup.select_one("article h1") or soup.select_one("h1")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    date_node = soup.select_one("div.col-12.col-md-3.col-lg-2.text-md-end.fst-italic")
    visible_raw = clean(date_node.get_text(" ", strip=True)) if date_node else None
    published = _published(visible_raw)
    body, paragraphs = _maintext(soup)
    api = API_POSTS.get(url)
    api_date = _api_datetime(api.get("date")) if api else None
    api_title = clean(BeautifulSoup(html.unescape(api.get("title", {}).get("rendered", "")), "html.parser").get_text(" ", strip=True)) if api else None
    api_body = clean(BeautifulSoup(api.get("content", {}).get("rendered", ""), "html.parser").get_text(" ", strip=True)) if api else None
    ld_date, ld_raw = _ld_published(soup)
    og = soup.select_one('meta[property="og:title"]')
    og_title = clean(og.get("content")) if og else None
    issues = []
    warnings = []
    if not title or not published or not body:
        issues.append("required_visible_field_missing")
    # The visible timestamp has minute precision. REST and JSON-LD can retain
    # seconds, so a same-minute difference is not a publication conflict.
    if published and api_date and abs((published - api_date).total_seconds()) >= 60:
        issues.append("visible_vs_rest_publication_disagreement")
    if published and ld_date and abs((published - ld_date).total_seconds()) >= 60:
        issues.append("visible_vs_jsonld_publication_disagreement")
    if published and ARCHIVE_DATES.get(url) and published.date() != ARCHIVE_DATES[url]:
        issues.append("archive_vs_article_publication_disagreement")
    if title and api_title and title.casefold() != api_title.casefold():
        issues.append("visible_vs_rest_title_disagreement")
    if title and og_title and title.casefold() != og_title.casefold():
        if api_title and title.casefold() == api_title.casefold():
            warnings.append("stale_og_title_visible_and_rest_agree")
        else:
            issues.append("visible_vs_og_title_disagreement")
    if body and len(body) < 180:
        issues.append("maintext_short")
    is_podcast = "/podcast/" in urlparse(url).path
    audio_player = bool(soup.select_one(".single-podcast-player"))
    transcript_markers = bool(soup.select_one("[class*=transcri], [id*=transcri], [class*=transcript], [id*=transcript]"))
    # These podcast pages contain an audio player and a short episode
    # description, not a transcript. Keep them in the URL audit, but do not
    # present their descriptions as the full main text of a news article.
    description_only = bool(is_podcast and audio_player and not transcript_markers and 1 <= len(paragraphs) <= 3)
    if is_podcast and not description_only:
        issues.append("podcast_requires_transcript_review")
    precise_published = api_date if published and api_date and abs((published - api_date).total_seconds()) < 60 else published
    dates = [{"kind": "visible_time", "source": "article date block", "raw": published.isoformat()}] if published else []
    if api_date:
        dates.append({"kind": "published", "source": "wp-json date", "raw": api_date.isoformat()})
    if ld_date:
        dates.append({"kind": "published", "source": "JSON-LD datePublished", "raw": ld_date.isoformat()})
    if ARCHIVE_DATES.get(url):
        dates.append({"kind": "visible_time", "source": "Socioambiental archive card", "raw": ARCHIVE_DATES[url].isoformat()})
    titles = [{"kind": "visible_h1", "source": "article h1", "raw": title}] if title else []
    if api_title:
        titles.append({"kind": "rest_title", "source": "wp-json title.rendered", "raw": api_title})
    if og_title:
        titles.append({"kind": "og_title", "raw": og_title})
    bodies = [{"kind": "scoped_visible_paragraphs", "source": "article div.entry-content", "text": body[:20000]}] if body else []
    if api_body:
        bodies.append({"kind": "rest_rendered_content", "source": "wp-json content.rendered", "text": api_body[:20000]})
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "date_publish": precise_published, "maintext": body,
        "language": "pt", "parser": "pure_adapter", "collection": "SKIP" if description_only else None,
        "parser_error": ",".join(issues) or None,
        "exclusion_reason": "excluded_podcast_description_without_transcript" if description_only else None,
        "exclusion_evidence": {
            "policy_type": "publisher_podcast_description_without_transcript",
            "publisher_url": url,
            "paragraph_count": len(paragraphs),
            "description_characters": len(body or ""),
            "audio_player_present": audio_player,
            "transcript_markers_present": transcript_markers,
        } if description_only else None,
        "field_sources": {"title": "article h1", "date_publish": "article visible date block",
                          "maintext": "article div.entry-content paragraphs"},
        "field_evidence": {
            "http_status": response.status_code, "titles": titles, "dates": dates, "bodies": bodies,
            "visible_publication_raw": visible_raw, "jsonld_date_published": ld_raw,
            "rest_id": api.get("id") if api else None,
            "rest_date": api.get("date") if api else None,
            "archive_date": ARCHIVE_DATES[url].isoformat() if url in ARCHIVE_DATES else None,
            "paragraph_count": len(paragraphs), "first_paragraph": paragraphs[0][:200] if paragraphs else None,
            "last_paragraph": paragraphs[-1][:200] if paragraphs else None,
            "podcast_description_only": description_only,
            "podcast_audio_player": audio_player,
            "podcast_transcript_markers": transcript_markers,
            "metadata_warnings": warnings,
        },
    }
