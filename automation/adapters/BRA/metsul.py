"""Read-only MetSul observer with API, dated listing, and sitemap reconciliation.

The legacy scraper imports MongoDB and writes at module load. This adapter
contains no database path. Sitemap lastmod is never treated as publication.
"""
from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from dateutil.tz import gettz

from adapters.BRA.common import clean, fetch, jsonld_objects, normalize_url, sitemap_entries, xml_document


class Config:
    source_name = "metsul.com"
    country_code = "BRA"
    is_env = True


BASE = "https://metsul.com"
API = f"{BASE}/wp-json/wp/v2/posts"
LISTING = f"{BASE}/analises/"
SITEMAP_INDEX = f"{BASE}/sitemap_index.xml"
NEWS_SITEMAP = f"{BASE}/news-sitemap.xml"
POST_MAP = re.compile(r"/post-sitemap\d*\.xml$", re.I)
VISIBLE_DATE = re.compile(r"\b(\d{2})/(\d{2})/(20\d{2})\s+às\s+(\d{1,2}):(\d{2})\b", re.I)
LISTING_DATE = re.compile(r"^(\d{2})/(\d{2})/(20\d{2})$")
PAGE_SIZE = 100
SAO_PAULO = gettz("America/Sao_Paulo")
if SAO_PAULO is None:
    raise RuntimeError("America/Sao_Paulo timezone unavailable")

_api_posts: dict[str, dict] = {}
_listing_dates: dict[str, date] = {}


def _url(raw: str) -> str:
    value = normalize_url(raw, Config.source_name)
    if not value:
        raise ValueError(f"Invalid MetSul article URL: {raw!r}")
    return value


def _api_params(start: date, end: date, page: int) -> dict[str, object]:
    return {
        "after": (datetime.combine(start, time.min) - timedelta(seconds=1)).isoformat(),
        "before": datetime.combine(end + timedelta(days=1), time.min).isoformat(),
        "per_page": PAGE_SIZE, "page": page,
        "orderby": "date", "order": "asc",
        "_fields": "id,date,date_gmt,modified,link,slug,title,content",
    }


def _api_page(start: date, end: date, page: int) -> tuple[list[dict], int, int, str]:
    endpoint = f"{API}?{urlencode(_api_params(start, end, page))}"
    response = fetch(endpoint)
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"MetSul API returned non-list page {page}")
    try:
        total = int(response.headers["X-WP-Total"])
        pages = int(response.headers["X-WP-TotalPages"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("MetSul API omitted pagination totals") from exc
    if total < 0 or pages < 0 or pages != (math.ceil(total / PAGE_SIZE) if total else 0):
        raise RuntimeError(f"MetSul API inconsistent totals on page {page}: {total}/{pages}")
    return payload, total, pages, endpoint


def _api_window(start: date, end: date, evidence: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    first_ids: list[tuple[object, object, object]] | None = None
    total_expected = pages_expected = None
    evidence["api_pages"] = []
    page = 1
    while True:
        payload, total, pages, endpoint = _api_page(start, end, page)
        if total_expected is None:
            total_expected, pages_expected = total, pages
        if total != total_expected or pages != pages_expected:
            raise RuntimeError("MetSul API pagination changed during discovery")
        evidence["api_pages"].append({"page": page, "url": endpoint, "count": len(payload), "total": total, "pages": pages})
        if page == 1:
            first_ids = [(item.get("id"), item.get("date"), item.get("link")) for item in payload if isinstance(item, dict)]
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not item.get("date") or not item.get("link"):
                raise RuntimeError(f"MetSul API invalid post on page {page}")
            try:
                published = datetime.fromisoformat(item["date"]).date()
            except ValueError as exc:
                raise RuntimeError(f"MetSul API invalid publication date: {item['link']}") from exc
            if not start <= published <= end:
                raise RuntimeError(f"MetSul API date escaped requested window: {item['link']}")
            url = _url(item["link"])
            if url in result:
                raise RuntimeError(f"MetSul API duplicate URL: {url}")
            result[url] = item
        if page >= max(1, pages):
            break
        page += 1
        if page > 100:
            raise RuntimeError("MetSul API exceeded bounded 100-page scan")
    if len(result) != total_expected:
        raise RuntimeError(f"MetSul API total changed: {len(result)} != {total_expected}")
    again, repeat_total, repeat_pages, _ = _api_page(start, end, 1)
    repeated_ids = [(item.get("id"), item.get("date"), item.get("link")) for item in again if isinstance(item, dict)]
    if repeat_total != total_expected or repeat_pages != pages_expected or repeated_ids != first_ids:
        raise RuntimeError("MetSul API first page changed after discovery")
    evidence["api_window_count"] = len(result)
    evidence["api_first_page_stable"] = True
    return result


def _listing_page(page: int) -> tuple[dict[str, date], str]:
    endpoint = LISTING if page == 1 else f"{LISTING}page/{page}/"
    soup = BeautifulSoup(fetch(endpoint).content, "html.parser")
    cards = soup.select(".post h2.title a[href]")
    if not cards:
        raise RuntimeError(f"MetSul listing has no post cards: {endpoint}")
    result: dict[str, date] = {}
    for link in cards:
        card = link.find_parent(class_="post")
        node = card.select_one(".infos .date") if card else None
        match = LISTING_DATE.fullmatch(clean(node.get_text(" ", strip=True)) or "") if node else None
        if not match:
            raise RuntimeError(f"MetSul listing card has no publication day: {endpoint}")
        try:
            published = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError as exc:
            raise RuntimeError(f"MetSul listing has invalid publication day: {endpoint}") from exc
        url = _url(link["href"])
        if url in result:
            raise RuntimeError(f"MetSul listing duplicates URL within page: {url}")
        result[url] = published
    return result, endpoint


def _listing_window(start: date, end: date, evidence: dict) -> dict[str, date]:
    evidence["listing_pages"] = []
    evidence["listing_repeated_urls"] = []
    result: dict[str, date] = {}
    previous_oldest = None
    crossed = False
    for page in range(1, 301):
        rows, endpoint = _listing_page(page)
        days = list(rows.values())
        if any(left < right for left, right in zip(days, days[1:])):
            raise RuntimeError(f"MetSul listing is not newest-first: {endpoint}")
        fresh = [(url, day) for url, day in rows.items() if url not in result]
        if not fresh:
            raise RuntimeError(f"MetSul listing made no progress: {endpoint}")
        if previous_oldest and fresh[0][1] > previous_oldest:
            raise RuntimeError(f"MetSul listing new-card order broke: {endpoint}")
        previous_oldest = days[-1]
        evidence["listing_pages"].append({
            "page": page, "url": endpoint, "cards": len(rows),
            "newest": days[0].isoformat(), "oldest": days[-1].isoformat(),
        })
        for url, published in rows.items():
            if url in result:
                if result[url] != published:
                    raise RuntimeError(f"MetSul listing changed URL publication day: {url}")
                evidence["listing_repeated_urls"].append(url)
                continue
            result[url] = published
        if days[-1] < start:
            if crossed and days[0] < start:
                evidence["listing_lower_boundary_confirmed"] = True
                return {url: day for url, day in result.items() if start <= day <= end}
            crossed = True
    raise RuntimeError("MetSul listing did not cross the lower boundary within 300 pages")


def _sitemap_inventory(start: date, evidence: dict) -> tuple[set[str], set[str]]:
    index = sitemap_entries(xml_document(SITEMAP_INDEX))
    maps = [url for url, _ in index if POST_MAP.search(url)]
    if not maps or len(maps) > 40:
        raise RuntimeError(f"MetSul post sitemap count outside 1..40: {len(maps)}")
    all_urls: set[str] = set()
    modified: set[str] = set()
    duplicates = []
    relevant_duplicates = []
    evidence["post_sitemaps"] = []
    for map_url in maps:
        entries = sitemap_entries(xml_document(map_url))
        evidence["post_sitemaps"].append({"url": map_url, "entries": len(entries)})
        for raw, lastmod in entries:
            url = _url(raw)
            if url in all_urls:
                duplicates.append(url)
                if lastmod is None or lastmod >= start or url in modified:
                    relevant_duplicates.append(url)
            all_urls.add(url)
            # A lastmod is a URL-candidate signal, never a publication date.
            if lastmod is None or lastmod >= start:
                modified.add(url)
    evidence["sitemap_duplicates"] = sorted(set(duplicates))
    evidence["sitemap_relevant_duplicates"] = sorted(set(relevant_duplicates))
    evidence["sitemap_unique_urls"] = len(all_urls)
    evidence["sitemap_modified_candidates"] = len(modified)
    return all_urls, modified


def _news_sitemap_window(start: date, end: date, evidence: dict) -> set[str]:
    """Use the short-lived news map as an additional positive URL signal."""
    soup = xml_document(NEWS_SITEMAP)
    result: set[str] = set()
    rows = soup.find_all("url")
    for row in rows:
        loc = row.find("loc")
        published = row.find("publication_date")
        if not loc or not published or not loc.get_text(strip=True):
            raise RuntimeError("MetSul news sitemap row has no URL or publication date")
        url = _url(loc.get_text(strip=True))
        local = _local_published(published.get_text(strip=True))
        if not local:
            raise RuntimeError(f"MetSul news sitemap has invalid publication date: {url}")
        if start <= local.date() <= end:
            if url in result:
                raise RuntimeError(f"MetSul news sitemap duplicates in-window URL: {url}")
            result.add(url)
    evidence["news_sitemap_url"] = NEWS_SITEMAP
    evidence["news_sitemap_rows"] = len(rows)
    evidence["news_sitemap_in_window"] = len(result)
    evidence["news_sitemap_short_lived"] = True
    return result


def _published_from_page(url: str) -> date | None:
    soup = BeautifulSoup(fetch(url).content, "html.parser")
    meta = soup.select_one('meta[property="article:published_time"]')
    raw = meta.get("content") if meta else None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None
    return value.astimezone(SAO_PAULO).date() if value and value.tzinfo else None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end:
        raise ValueError("start must not exceed end")
    _api_posts.clear()
    _listing_dates.clear()
    evidence.update({
        "strategy": "complete_date_bounded_wordpress_api_plus_dated_analyses_listing_plus_all_post_sitemaps",
        "api_endpoint": API, "listing_endpoint": LISTING, "sitemap_index": SITEMAP_INDEX,
        "news_sitemap": NEWS_SITEMAP,
        "sitemap_lastmod_is_not_publication_date": True, "coverage_complete": False,
    })
    api = _api_window(start, end, evidence)
    listing = _listing_window(start, end, evidence)
    _api_posts.update(api)
    _listing_dates.update(listing)
    sitemap, modified = _sitemap_inventory(start, evidence)
    news = _news_sitemap_window(start, end, evidence)
    api_set, listing_set = set(api), set(listing)
    evidence["api_missing_from_listing"] = sorted(api_set - listing_set)
    evidence["listing_missing_from_api"] = sorted(listing_set - api_set)
    evidence["api_listing_date_mismatches"] = sorted(
        url for url in api_set & listing_set
        if datetime.fromisoformat(api[url]["date"]).date() != listing[url]
    )
    evidence["api_missing_from_sitemap"] = sorted(api_set - sitemap)
    evidence["news_missing_from_api"] = sorted(news - api_set)
    evidence["news_missing_from_listing"] = sorted(news - listing_set)
    extras = sorted(modified - api_set)
    if len(extras) > 500:
        raise RuntimeError(f"MetSul sitemap has {len(extras)} modified non-API URLs; bounded page verification required")
    sitemap_only = []
    unresolved = []
    for url in extras:
        try:
            published = _published_from_page(url)
        except Exception as exc:
            unresolved.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if published is None:
            unresolved.append({"url": url, "error": "publication signal missing"})
        elif start <= published <= end:
            sitemap_only.append(url)
    evidence["sitemap_only_in_window"] = sitemap_only
    evidence["sitemap_extra_unresolved"] = unresolved
    # The analyses feed omits some published posts.  The bounded WordPress
    # inventory is complete by its own count headers, and every API URL must
    # independently occur in the full post sitemaps.  A listing omission is
    # informative in that case, while a listing-only URL still blocks coverage.
    evidence["listing_partial_but_api_sitemap_reconciled"] = bool(
        evidence["api_missing_from_listing"] and not evidence["api_missing_from_sitemap"]
    )
    evidence["coverage_complete"] = bool(evidence.get("listing_lower_boundary_confirmed")) and not any((
        evidence["listing_missing_from_api"],
        evidence["api_listing_date_mismatches"], evidence["api_missing_from_sitemap"],
        evidence["sitemap_relevant_duplicates"], sitemap_only, unresolved,
        evidence["news_missing_from_api"], evidence["news_missing_from_listing"],
    ))
    urls = api_set | listing_set | set(sitemap_only) | news
    return sorted(urls, key=lambda url: (api.get(url, {}).get("date") or listing.get(url, date.max).isoformat(), url))


def _body(container: BeautifulSoup) -> str | None:
    clone = BeautifulSoup(str(container), "html.parser")
    for bad in clone.select(
        "script, style, iframe, template, noscript, figure, figcaption, .wp-caption, "
        ".metsul-leia-tambem, .membrana_reward_blur, .metsu-antes-do-conteudo, "
        ".metsu-depois-do-conteudo, .MTU_VIDEO, .twitter-tweet, .adsbygoogle, "
        ".pmpro_content_message, [id*='alright'], [id*='intext']"
    ):
        bad.decompose()
    lines = []
    for node in clone.select("p, h2, h3"):
        value = clean(node.get_text(" ", strip=True))
        if not value or re.match(r"^(?:Anúncios|Publicidade|Advertisement|Leia também)\b", value, re.I):
            continue
        if value not in lines:
            lines.append(value)
    return "\n\n".join(lines) or None


def _api_body(item: dict) -> str | None:
    content = item.get("content")
    if not isinstance(content, dict) or content.get("protected") is not False:
        return None
    rendered = content.get("rendered")
    return _body(BeautifulSoup(rendered, "html.parser")) if isinstance(rendered, str) else None


def _news_article(soup: BeautifulSoup) -> dict | None:
    for item in jsonld_objects(soup):
        types = item.get("@type")
        if (types == "NewsArticle" or isinstance(types, list) and "NewsArticle" in types):
            return item
    return None


def _local_published(raw: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None
    return parsed.astimezone(SAO_PAULO).replace(tzinfo=None) if parsed and parsed.tzinfo else None


def _visible_published(soup: BeautifulSoup) -> tuple[datetime | None, str | None]:
    node = soup.select_one("article .post-header .meta-info-row > span")
    raw = clean(node.get_text(" ", strip=True)) if node else None
    match = VISIBLE_DATE.search(raw or "")
    if not match:
        return None, raw
    try:
        return datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)), int(match.group(4)), int(match.group(5))), raw
    except ValueError:
        return None, raw


def scrape_url(url: str) -> dict:
    url = _url(url)
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title_node = soup.select_one("article h1.post-title-main .entry-title-primary")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    og = soup.select_one('meta[property="og:title"]')
    og_title = clean(og.get("content")) if og else None
    ld = _news_article(soup)
    ld_title = clean(ld.get("headline")) if ld else None
    meta = soup.select_one('meta[property="article:published_time"]')
    meta_raw = clean(meta.get("content")) if meta else None
    published = _local_published(meta_raw)
    visible, visible_raw = _visible_published(soup)
    ld_raw = clean(ld.get("datePublished")) if ld else None
    api = _api_posts.get(url)
    api_title = clean(BeautifulSoup(html.unescape(api.get("title", {}).get("rendered", "")), "html.parser").get_text(" ", strip=True)) if api else None
    body_node = soup.select_one("article .post-content.entry-content")
    body = _body(body_node) if body_node else None
    api_body = _api_body(api) if api else None
    errors = []
    warnings = []
    canonical_node = soup.select_one('link[rel="canonical"]')
    canonical_raw = canonical_node.get("href") if canonical_node else None
    try:
        canonical = _url(canonical_raw) if canonical_raw else None
        response_url = _url(response.url)
    except ValueError:
        canonical = response_url = None
    if canonical != url or response_url != url:
        errors.append("canonical_or_response_url_mismatch")
    def normalized_title(value: str | None) -> str:
        return (clean(html.unescape(value or "")) or "").translate(
            str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
        ).casefold()

    if not title or not og_title or (ld_title and normalized_title(title) != normalized_title(ld_title)) or (
        normalized_title(title) != normalized_title(og_title)
        and normalized_title(title) != normalized_title(api_title)
    ):
        errors.append("title_signals_missing_or_disagree")
    elif normalized_title(title) != normalized_title(og_title):
        warnings.append("stale_og_title_visible_and_wordpress_agree")
    if not published or not visible or published.replace(second=0, microsecond=0) != visible:
        errors.append("visible_vs_publisher_publication_disagreement")
    if published and ld_raw and _local_published(ld_raw) != published:
        errors.append("jsonld_vs_meta_publication_disagreement")
    if not api:
        errors.append("wordpress_api_record_missing")
    else:
        try:
            api_published = datetime.fromisoformat(api["date"])
            gmt = datetime.fromisoformat(api["date_gmt"]).replace(tzinfo=timezone.utc).astimezone(SAO_PAULO).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError):
            api_published = gmt = None
        if not published or api_published != published or gmt != published:
            errors.append("wordpress_vs_page_publication_disagreement")
        if normalized_title(title) != normalized_title(api_title):
            errors.append("wordpress_vs_page_title_disagreement")
    if not body or len(body) < 400:
        errors.append("publisher_maintext_missing_or_short")
    if not api_body:
        errors.append("wordpress_maintext_missing_or_protected")
    elif body:
        page_words, api_words = body.split(), api_body.split()
        length_ratio = min(len(page_words), len(api_words)) / max(len(page_words), len(api_words))
        similarity = SequenceMatcher(None, body, api_body).ratio()
        if length_ratio < .75 or similarity < .78:
            errors.append("wordpress_vs_page_maintext_disagreement")
    else:
        length_ratio = similarity = None
    if not body or not api_body:
        length_ratio = similarity = None

    titles = []
    for kind, source, raw in (
        ("visible_h1", "article h1.post-title-main .entry-title-primary", title),
        ("og_title", 'meta[property="og:title"]', og_title),
        ("jsonld_headline", "jsonld.NewsArticle.headline", ld_title),
        ("wordpress_title", "wp-json.post.title.rendered", api_title if api else None),
    ):
        if raw:
            titles.append({"kind": kind, "source": source, "raw": raw})
    dates = []
    for kind, source, raw in (
        ("visible_time", "article .post-header .meta-info-row", visible_raw),
        ("published", 'meta[property="article:published_time"]', meta_raw),
        ("published", "jsonld.NewsArticle.datePublished", ld_raw),
        ("published", "wp-json.post.date", api.get("date") if api else None),
        ("modified", "wp-json.post.modified", api.get("modified") if api else None),
    ):
        if raw:
            dates.append({"kind": kind, "source": source, "raw": raw})
    bodies = []
    if api_body and len(api_body) >= 200:
        bodies.append({"kind": "wordpress_content", "source": "wp-json.post.content.rendered", "text": api_body[:20000]})
    if body and len(body) >= 200:
        bodies.append({"kind": "page_body", "source": "article .post-content.entry-content", "text": body[:20000]})
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "date_publish": published, "maintext": body, "language": "pt",
        "parser": "pure_adapter", "collection": None,
        "parser_error": ";".join(errors) or None,
        "field_sources": {
            "title": "article h1.post-title-main .entry-title-primary",
            "date_publish": 'meta[property="article:published_time"]',
            "maintext": "article .post-content.entry-content",
        },
        "field_evidence": {
            "http_status": response.status_code,
            "page_sha256": hashlib.sha256(response.content).hexdigest(),
            "canonical_url": canonical,
            "publisher_api_id": api.get("id") if api else None,
            "listing_publication_day": _listing_dates.get(url).isoformat() if url in _listing_dates else None,
            "body_cross_check": {"word_length_ratio": length_ratio, "sequence_similarity": similarity},
            "metadata_warnings": warnings,
            "titles": titles, "dates": dates, "bodies": bodies,
        },
    }
