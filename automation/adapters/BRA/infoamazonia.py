"""Read-only InfoAmazonia post observer with multilingual REST/sitemap reconciliation."""
from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup
from dateutil.tz import gettz

from adapters.BRA.common import clean, fetch, jsonld_objects, normalize_url, sitemap_entries, xml_document


class Config:
    source_name = "infoamazonia.org"
    country_code = "BRA"
    is_env = True


BASE = "https://infoamazonia.org"
INDEX = BASE + "/sitemap_index.xml"
API = BASE + "/wp-json/wp/v2/posts"
POST_MAP = re.compile(r"/post-sitemap(\d*)\.xml$")
PATH_DAY = re.compile(r"^/(?:en/|es/)?(20\d{2})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/")
MONTHS = {
    "janeiro": 1, "january": 1, "enero": 1,
    "fevereiro": 2, "february": 2, "febrero": 2,
    "março": 3, "marco": 3, "march": 3, "marzo": 3,
    "abril": 4, "april": 4, "maio": 5, "may": 5, "mayo": 5,
    "junho": 6, "june": 6, "junio": 6,
    "julho": 7, "july": 7, "julio": 7,
    "agosto": 8, "august": 8,
    "setembro": 9, "september": 9, "septiembre": 9,
    "outubro": 10, "october": 10, "octubre": 10,
    "novembro": 11, "november": 11, "noviembre": 11,
    "dezembro": 12, "december": 12, "diciembre": 12,
}
DISPLAY_DATE = re.compile(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\s+at\s+(\d{1,2}):(\d{2})\b", re.I)
PAGE_SIZE = 100
MAX_DAYS = 180
MAX_API_PAGES = 20
ZONE = gettz("America/Sao_Paulo")
if ZONE is None:
    raise RuntimeError("America/Sao_Paulo timezone unavailable")

_posts: dict[str, dict] = {}


def _url(raw: str) -> str | None:
    value = normalize_url(raw, Config.source_name)
    return value if value and PATH_DAY.match(urlparse(value).path) else None


def _path_day(url: str) -> date | None:
    match = PATH_DAY.match(urlparse(url).path)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _visible_datetime(raw: str | None) -> datetime | None:
    match = DISPLAY_DATE.search(raw or "")
    if not match:
        return None
    day, month, year, hour, minute = match.groups()
    try:
        return datetime(int(year), MONTHS[month.casefold()], int(day), int(hour), int(minute))
    except (KeyError, ValueError):
        return None


def _iso_datetime(raw: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None
    return parsed.astimezone(ZONE).replace(tzinfo=None) if parsed and parsed.tzinfo else parsed


def _sitemap_inventory(start: date, end: date, evidence: dict) -> set[str]:
    index = [(url, modified) for url, modified in sitemap_entries(xml_document(INDEX)) if POST_MAP.search(url)]
    indexed = sorted((int(POST_MAP.search(url).group(1) or 1), url) for url, _ in index)
    if not indexed or [number for number, _ in indexed] != list(range(1, len(indexed) + 1)):
        raise ValueError("InfoAmazonia post sitemap index has a missing or repeated shard")
    evidence["post_sitemaps"] = []
    all_urls = set()
    window_urls = set()
    duplicate_window_urls = set()
    for number, url in indexed:
        response = fetch(url)
        entries = sitemap_entries(BeautifulSoup(response.content, "xml"))
        if not entries:
            raise ValueError(f"InfoAmazonia sitemap shard {number} is empty")
        normalized = [_url(raw) for raw, _ in entries]
        duplicates = {value for value in normalized if value and normalized.count(value) > 1}
        duplicate_window_urls.update(value for value in duplicates if (day := _path_day(value)) and start <= day <= end)
        evidence["post_sitemaps"].append({"url": url, "entries": len(entries),
                                          "duplicate_urls": len(duplicates),
                                          "sha256": hashlib.sha256(response.content).hexdigest(),
                                          "bytes": len(response.content)})
        for value in normalized:
            if value:
                all_urls.add(value)
                day = _path_day(value)
                if day and start <= day <= end:
                    window_urls.add(value)
    evidence["post_sitemap_total_dated_urls"] = len(all_urls)
    evidence["post_sitemap_path_window_urls"] = len(window_urls)
    evidence["post_sitemap_duplicate_window_urls"] = sorted(duplicate_window_urls)
    return window_urls


def _api_inventory(start: date, end: date, evidence: dict) -> dict[str, dict]:
    evidence["api_languages"] = []
    evidence["external_rest_links_excluded"] = []
    found: dict[str, dict] = {}
    for language in ("pt", "en", "es"):
        params = {"lang": language, "after": f"{start - timedelta(days=1)}T23:59:59",
                  "before": f"{end + timedelta(days=1)}T00:00:00",
                  "per_page": PAGE_SIZE, "orderby": "date", "order": "desc"}
        total = pages = None
        rows = []
        checked = []
        for page in range(1, MAX_API_PAGES + 1):
            response = fetch(f"{API}?{urlencode({**params, 'page': page})}")
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"InfoAmazonia REST {language} page {page} is not a list")
            try:
                header_total = int(response.headers["X-WP-Total"])
                header_pages = int(response.headers["X-WP-TotalPages"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"InfoAmazonia REST {language} pagination headers missing") from exc
            if total is None:
                total, pages = header_total, header_pages
                if pages != math.ceil(total / PAGE_SIZE) or pages > MAX_API_PAGES:
                    raise ValueError(f"InfoAmazonia REST {language} pagination inconsistent")
            if (header_total, header_pages) != (total, pages):
                raise ValueError(f"InfoAmazonia REST {language} count changed during scan")
            checked.append({"page": page, "items": len(payload), "total": header_total,
                            "pages": header_pages, "sha256": hashlib.sha256(response.content).hexdigest()})
            rows.extend(payload)
            if page >= max(1, pages):
                break
        ids = [row.get("id") for row in rows if isinstance(row, dict)]
        if len(rows) != total or len(set(ids)) != len(rows):
            raise ValueError(f"InfoAmazonia REST {language} IDs/count mismatch")
        selected = 0
        for row in rows:
            url = _url(row.get("link", ""))
            published = _iso_datetime(row.get("date"))
            if not published:
                raise ValueError(f"InfoAmazonia REST {language} post lacks publication date")
            if not url:
                evidence["external_rest_links_excluded"].append({"id": row.get("id"),
                    "language": language, "link": row.get("link"), "date": row.get("date")})
                continue
            path = urlparse(url).path
            if language == "en" and not path.startswith("/en/") or language == "es" and not path.startswith("/es/") or language == "pt" and path.startswith(("/en/", "/es/")):
                raise ValueError(f"InfoAmazonia REST language filter ignored: {language}: {url}")
            if start <= published.date() <= end:
                if url in found:
                    raise ValueError(f"InfoAmazonia REST repeated URL across languages: {url}")
                found[url] = row
                selected += 1
        evidence["api_languages"].append({"language": language, "total_with_boundary_overlap": total,
                                          "in_window": selected, "pages": checked})
    evidence["api_window_urls"] = len(found)
    return found


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > MAX_DAYS:
        raise ValueError(f"invalid InfoAmazonia date window (max {MAX_DAYS + 1} days)")
    _posts.clear()
    evidence.update({"method": "all_indexed_post_sitemaps_plus_counted_pt_en_es_REST_publication_inventory",
                     "sitemap_index": INDEX, "coverage_complete": False,
                     "sitemap_lastmod_is_not_publication_date": True})
    sitemap = _sitemap_inventory(start, end, evidence)
    api = _api_inventory(start, end, evidence)
    api_urls = set(api)
    evidence["api_urls_missing_in_sitemap"] = sorted(api_urls - sitemap)
    evidence["path_dated_sitemap_urls_missing_in_api"] = sorted(sitemap - api_urls)
    evidence["coverage_complete"] = not evidence["api_urls_missing_in_sitemap"] and not evidence["path_dated_sitemap_urls_missing_in_api"] and not evidence["post_sitemap_duplicate_window_urls"]
    _posts.update(api)
    return sorted(api_urls | sitemap)


def _body(soup: BeautifulSoup) -> tuple[str | None, list[str]]:
    original = soup.select_one("article .entry-content") or soup.select_one(".entry-content")
    if not original:
        return None, []
    node = BeautifulSoup(str(original), "html.parser")
    for bad in node.select("script, style, iframe, nav, aside, figure, figcaption, form, .related, .share, .social, [class*='related'], [class*='newsletter'], [class*='recommend']"):
        bad.decompose()
    paragraphs = []
    for item in node.select("p, h2, h3, blockquote"):
        if item.name == "blockquote" and item.select("p"):
            continue
        value = clean(item.get_text(" ", strip=True))
        if value and value not in paragraphs and len(value) >= 15:
            paragraphs.append(value)
    return "\n\n".join(paragraphs) or None, paragraphs


def scrape_url(url: str) -> dict:
    if _url(url) != url:
        raise ValueError(f"not a canonical InfoAmazonia post URL: {url}")
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title_node = soup.select_one("article h1.entry-title") or soup.select_one("h1.entry-title")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    date_node = soup.select_one("article .post-date") or soup.select_one(".post-date")
    raw_date = clean(date_node.get_text(" ", strip=True)) if date_node else None
    visible = _visible_datetime(raw_date)
    body, paragraphs = _body(soup)
    api = _posts.get(url)
    api_date = _iso_datetime(api.get("date")) if api else None
    api_title = clean(BeautifulSoup(html.unescape(api.get("title", {}).get("rendered", "")), "html.parser").get_text(" ", strip=True)) if api else None
    meta = soup.select_one('meta[property="article:published_time"]')
    meta_raw = meta.get("content") if meta else None
    meta_date = _iso_datetime(meta_raw)
    ld = next((item for item in jsonld_objects(soup) if item.get("@type") in ("NewsArticle", "Article")), {})
    ld_date = _iso_datetime(ld.get("datePublished"))
    ld_title = clean(html.unescape(ld.get("headline", "")))
    errors = []
    if not title or not visible or not body:
        errors.append("required_visible_field_missing")
    for name, other in (("rest", api_date), ("metadata", meta_date), ("jsonld", ld_date)):
        if visible and other and abs((visible - other).total_seconds()) >= 60:
            errors.append(f"visible_vs_{name}_publication_disagreement")
    if title and api_title and title.casefold() != api_title.casefold():
        errors.append("visible_vs_rest_title_disagreement")
    if title and ld_title and title.casefold() != ld_title.casefold():
        errors.append("visible_vs_jsonld_title_disagreement")
    if body and len(body) < 180:
        errors.append("maintext_short")
    published = api_date if visible and api_date and abs((visible - api_date).total_seconds()) < 60 else visible
    dates = [{"kind": "visible_time", "raw": visible.isoformat(), "source": ".post-date first timestamp"}] if visible else []
    for source, value in (("REST date", api_date), ("article:published_time", meta_date), ("JSON-LD datePublished", ld_date)):
        if value:
            dates.append({"kind": "published", "source": source, "raw": value.isoformat()})
    titles = [{"kind": "visible_h1", "raw": title, "source": "h1.entry-title"}] if title else []
    if api_title:
        titles.append({"kind": "rest_title", "raw": api_title})
    if ld_title:
        titles.append({"kind": "jsonld_headline", "raw": ld_title})
    language = "en" if urlparse(url).path.startswith("/en/") else "es" if urlparse(url).path.startswith("/es/") else "pt"
    return {"url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
            "title": title, "date_publish": published, "maintext": body, "language": language,
            "parser": "pure_adapter", "collection": None, "parser_error": ",".join(errors) or None,
            "field_sources": {"title": "h1.entry-title", "date_publish": ".post-date first timestamp",
                              "maintext": "article .entry-content paragraphs"},
            "field_evidence": {"http_status": response.status_code, "titles": titles, "dates": dates,
                               "bodies": [{"kind": "scoped_visible_paragraphs", "text": body[:20000]}] if body else [],
                               "visible_date_raw": raw_date, "rest_id": api.get("id") if api else None,
                               "paragraph_count": len(paragraphs),
                               "first_paragraph": paragraphs[0][:200] if paragraphs else None,
                               "last_paragraph": paragraphs[-1][:200] if paragraphs else None}}
