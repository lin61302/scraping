"""Read-only Amazônia Real observer with independent archive/API/sitemap checks.

The WordPress API and dated monthly archive are compared for each full month
that intersects a requested publication window. Post sitemaps are checked as
an additional inventory. Sitemap ``lastmod`` is only a candidate selector;
article-page publication evidence determines ``date_publish``.
"""
from __future__ import annotations

import html
import hashlib
import math
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import unquote, urlencode, urlparse

from bs4 import BeautifulSoup
from dateutil.tz import gettz

from adapters.BRA.common import clean, fetch, jsonld_objects, normalize_url, sitemap_entries, xml_document


class Config:
    source_name = "amazoniareal.com.br"
    country_code = "BRA"
    is_env = True


BASE = "https://amazoniareal.com.br"
API = f"{BASE}/wp-json/wp/v2/posts"
SPECIAL_API = f"{BASE}/wp-json/wp/v2/especiais"
CORRECTION_API = f"{BASE}/wp-json/wp/v2/correcao"
CORRECTION_TYPE = f"{BASE}/wp-json/wp/v2/types/correcao"
ARCHIVE = f"{BASE}/arquivos/"
SITEMAP_INDEX = f"{BASE}/sitemap_index.xml"
POST_MAP = re.compile(r"/post-sitemap\d*\.xml$", re.I)
SPECIAL_MAP = re.compile(r"/especiais-sitemap\d*\.xml$", re.I)
CORRECTION_MAP = re.compile(r"/correcao-sitemap\d*\.xml$", re.I)
CUSTOM_STORY_MAP = re.compile(r"/(?:jovem_cidadao|personagem|especiais|correcao|web-story)-sitemap\d*\.xml$", re.I)
ARCHIVE_DAY = re.compile(r"^(\d{2})/(\d{2})/(20\d{2})$")
VISIBLE_DATE = re.compile(r"Publicado\s+em:\s*(\d{2})/(\d{2})/(20\d{2})\s+às\s+(\d{1,2}):(\d{2})", re.I)
AUTHOR_SECTION = re.compile(r"^Sobre\s+(?:os?\s+)?autores?\s*$", re.I)
FOOTER_START = re.compile(
    r"^(?:Republique nossos conteúdos:|\*?\s*Esta reportagem foi (?:produzida|atualizada)|"
    r"\*[^.]{1,90}\s+é\s+(?:jornalista|repórter))",
    re.I,
)
SPECIAL_IMAGE_CAPTION = re.compile(r"\b(?:Foto\s+da\s+pesquisa|Foto:|Imagem:)", re.I)
MANAUS = gettz("America/Manaus")
if MANAUS is None:
    raise RuntimeError("America/Manaus timezone unavailable")
PAGE_SIZE = 100

_api_posts: dict[str, dict] = {}
_sitemap_page_cache: dict[str, dict] = {}


def _months(start: date, end: date):
    month = date(start.year, start.month, 1)
    while month <= end:
        next_month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        yield month, next_month
        month = next_month


def _api_month(month: date, next_month: date, evidence: dict, endpoint: str = API, bucket: str = "api_months") -> dict[str, dict]:
    params = {
        "after": (datetime.combine(month, datetime.min.time()) - timedelta(seconds=1)).isoformat(),
        "before": datetime.combine(next_month, datetime.min.time()).isoformat(),
        "per_page": PAGE_SIZE,
        "orderby": "date", "order": "asc", "_fields": "id,date,link,title,content",
    }
    for attempt in range(2):
        posts = []
        totals = []
        page = 1
        expected = pages = None
        while True:
            response = fetch(f"{endpoint}?{urlencode({**params, 'page': page})}")
            items = response.json()
            if not isinstance(items, list):
                raise RuntimeError(f"WordPress API returned a non-list for {month} page {page}")
            try:
                total = int(response.headers["X-WP-Total"])
                total_pages = int(response.headers["X-WP-TotalPages"])
            except (KeyError, ValueError, TypeError) as exc:
                raise RuntimeError(f"WordPress API omitted pagination totals for {month}") from exc
            if expected is None:
                expected, pages = total, total_pages
            totals.append({"page": page, "count": len(items), "total": total, "pages": total_pages})
            posts.extend(items)
            if page >= max(1, pages):
                break
            page += 1
        ids = [item.get("id") for item in posts if isinstance(item, dict)]
        stable = (
            len(posts) == expected and pages == math.ceil(expected / PAGE_SIZE)
            and len(ids) == len(posts) and len(ids) == len(set(ids))
            and all(item["total"] == expected and item["pages"] == pages for item in totals)
        )
        if stable:
            result = {}
            for item in posts:
                if not item.get("date") or not item.get("link"):
                    raise RuntimeError(f"WordPress API item lacks publication date or URL in {month}")
                published = datetime.fromisoformat(item["date"])
                if not month <= published.date() < next_month:
                    raise RuntimeError(f"WordPress API date escaped requested month: {item['link']}")
                url = normalize_url(item["link"], Config.source_name)
                if not url or url in result:
                    raise RuntimeError(f"WordPress API invalid/duplicate source URL: {item['link']!r}")
                result[url] = item
            evidence[bucket].append({
                "month": month.strftime("%Y-%m"), "total": expected,
                "pages": totals, "stability_attempt": attempt + 1,
            })
            return result
    raise RuntimeError(f"WordPress API changed during pagination for {month}: {totals}")


def _archive_month(month: date, evidence: dict) -> dict[str, date]:
    url = f"{ARCHIVE}?{urlencode({'ano': month.year, 'mes': month.month})}"
    soup = BeautifulSoup(fetch(url).content, "html.parser")
    result = {}
    for anchor in soup.select("a[href]"):
        bold = anchor.find("b")
        match = ARCHIVE_DAY.fullmatch(clean(bold.get_text(" ", strip=True)) or "") if bold else None
        if not match:
            continue
        day, mo, year = map(int, match.groups())
        try:
            published = date(year, mo, day)
        except ValueError as exc:
            raise RuntimeError(f"invalid dated archive item in {url}: {match.group(0)}") from exc
        if published.year != month.year or published.month != month.month:
            raise RuntimeError(f"archive {url} contains an item from {published}")
        article_url = normalize_url(anchor.get("href"), Config.source_name)
        if not article_url or (article_url in result and result[article_url] != published):
            raise RuntimeError(f"invalid/duplicate dated archive URL: {anchor.get('href')!r}")
        result[article_url] = published
    evidence["archive_months"].append({"month": month.strftime("%Y-%m"), "url": url, "dated_urls": len(result)})
    return result


def _rest_window(start: date, end: date, evidence: dict, endpoint: str, bucket: str) -> dict[str, dict]:
    result = {}
    for month, next_month in _months(start, end):
        result.update(_api_month(month, next_month, evidence, endpoint, bucket))
    return result


def _verify_correction_publication(url: str) -> tuple[datetime, dict]:
    """Resolve a sitemap correction's original date outside the bounded REST months.

    A sitemap lastmod may reflect an edit. Require a unique correction-type REST
    record for this exact URL and independent article publication evidence on the
    page; ambiguous, missing, or disagreeing evidence fails closed.
    """
    slug = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if not slug:
        raise ValueError("correction URL has no slug")
    query = urlencode({"slug": slug, "_fields": "id,date,link"})
    records = fetch(f"{CORRECTION_API}?{query}").json()
    if not isinstance(records, list):
        raise ValueError("correction REST lookup returned a non-list")
    matches = [
        item for item in records if isinstance(item, dict)
        and normalize_url(item.get("link"), Config.source_name) == url
    ]
    if len(records) != 1 or len(matches) != 1:
        raise ValueError(f"correction REST lookup did not uniquely match URL (returned {len(records)} records)")
    record = matches[0]
    rest_date = _publication_local(record.get("date"))
    if rest_date is None:
        raise ValueError("correction REST lookup has no valid original publication date")

    soup = BeautifulSoup(fetch(url).content, "html.parser")
    page_dates = []
    for item in jsonld_objects(soup):
        kinds = item.get("@type")
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(isinstance(kind, str) and "Article" in kind for kind in kinds):
            continue
        if item.get("datePublished"):
            parsed = _publication_local(item["datePublished"])
            if parsed is None:
                raise ValueError("correction page has invalid Article datePublished")
            page_dates.append({"source": "jsonld.Article.datePublished", "raw": item["datePublished"], "date": parsed})
    meta = soup.select_one('meta[property="article:published_time"]')
    if meta and meta.get("content"):
        parsed = _publication_local(meta["content"])
        if parsed is None:
            raise ValueError("correction page has invalid article:published_time")
        page_dates.append({"source": "meta.article:published_time", "raw": meta["content"], "date": parsed})
    if not page_dates:
        raise ValueError("correction page has no article publication date")
    if any(abs((item["date"] - rest_date).total_seconds()) > 120 for item in page_dates):
        raise ValueError("correction page and REST original publication dates disagree")
    return rest_date, {
        "url": url, "date_publish": rest_date.isoformat(),
        "rest_id": record.get("id"), "rest_date": record["date"],
        "page_publication_sources": [
            {"source": item["source"], "raw": item["raw"]} for item in page_dates
        ],
    }


def _sitemap_inventory(start: date, evidence: dict) -> tuple[set[str], set[str], set[str], set[str], set[str], set[str]]:
    index = sitemap_entries(xml_document(SITEMAP_INDEX))
    maps = [url for url, _ in index if POST_MAP.search(url)]
    if not maps:
        raise RuntimeError("publisher sitemap index has no post sitemap")
    special_maps = [url for url, _ in index if SPECIAL_MAP.search(url)]
    correction_maps = [url for url, _ in index if CORRECTION_MAP.search(url)]
    if not special_maps or not correction_maps:
        raise RuntimeError("publisher sitemap index omits special or correction map")
    evidence["other_custom_sitemaps_requiring_policy"] = [
        {"url": url, "index_lastmod": lastmod.isoformat() if lastmod else None}
        for url, lastmod in index
        if CUSTOM_STORY_MAP.search(url) and not (SPECIAL_MAP.search(url) or CORRECTION_MAP.search(url))
        and (lastmod is None or lastmod >= start)
    ]
    evidence["other_custom_sitemaps_older_than_start"] = [
        {"url": url, "index_lastmod": lastmod.isoformat()}
        for url, lastmod in index
        if CUSTOM_STORY_MAP.search(url) and not (SPECIAL_MAP.search(url) or CORRECTION_MAP.search(url))
        and lastmod is not None and lastmod < start
    ]
    all_urls = set()
    modified_candidates = set()
    evidence["post_sitemaps"] = []
    for map_url in maps:
        entries = sitemap_entries(xml_document(map_url))
        for raw, lastmod in entries:
            url = normalize_url(raw, Config.source_name)
            if not url:
                raise RuntimeError(f"invalid post sitemap URL: {raw!r}")
            all_urls.add(url)
            # A post published in the window cannot have an earlier lastmod.
            # Missing lastmod is treated as a candidate and parsed below.
            if lastmod is None or lastmod >= start:
                modified_candidates.add(url)
        evidence["post_sitemaps"].append({"url": map_url, "entries": len(entries)})
    evidence["sitemap_total_unique_urls"] = len(all_urls)
    evidence["sitemap_lastmod_candidates"] = len(modified_candidates)
    evidence["sitemap_lastmod_is_not_publication_date"] = True
    special_urls = set()
    special_modified = set()
    evidence["special_sitemaps"] = []
    for map_url in special_maps:
        entries = sitemap_entries(xml_document(map_url))
        for raw, lastmod in entries:
            url = normalize_url(raw, Config.source_name)
            if not url:
                raise RuntimeError(f"invalid special sitemap URL: {raw!r}")
            special_urls.add(url)
            if lastmod is None or lastmod >= start:
                special_modified.add(url)
        evidence["special_sitemaps"].append({"url": map_url, "entries": len(entries)})
    correction_urls = set()
    correction_modified = set()
    evidence["correction_sitemaps"] = []
    for map_url in correction_maps:
        entries = sitemap_entries(xml_document(map_url))
        for raw, lastmod in entries:
            url = normalize_url(raw, Config.source_name)
            if not url:
                raise RuntimeError(f"invalid correction sitemap URL: {raw!r}")
            correction_urls.add(url)
            if lastmod is None or lastmod >= start:
                correction_modified.add(url)
        evidence["correction_sitemaps"].append({"url": map_url, "entries": len(entries)})
    evidence["special_sitemap_unique_urls"] = len(special_urls)
    evidence["special_sitemap_lastmod_candidates"] = len(special_modified)
    evidence["correction_sitemap_unique_urls"] = len(correction_urls)
    evidence["correction_sitemap_lastmod_candidates"] = len(correction_modified)
    return all_urls, modified_candidates, special_urls, special_modified, correction_urls, correction_modified


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end:
        raise ValueError("start must be on or before end")
    _api_posts.clear()
    _sitemap_page_cache.clear()
    evidence.update({
        "discovery": "publisher_posts_and_specials_rest_by_publication_month_plus_dated_archive_and_type_sitemaps",
        "api_endpoint": API, "archive_endpoint": ARCHIVE,
        "special_api_endpoint": SPECIAL_API, "correction_api_endpoint": CORRECTION_API,
        "sitemap_index": SITEMAP_INDEX, "api_months": [], "special_api_months": [],
        "correction_api_months": [], "archive_months": [],
        "coverage_complete": False,
    })
    api_all = {}
    archive_all = {}
    for month, next_month in _months(start, end):
        api_all.update(_api_month(month, next_month, evidence))
        archive_all.update(_archive_month(month, evidence))
    special_all = _rest_window(start, end, evidence, SPECIAL_API, "special_api_months")
    correction_all = _rest_window(start, end, evidence, CORRECTION_API, "correction_api_months")
    api_set = set(api_all)
    archive_set = set(archive_all)
    mismatch = sorted(url for url in api_set & archive_set if datetime.fromisoformat(api_all[url]["date"]).date() != archive_all[url])
    evidence["api_archive_missing_from_archive"] = sorted(api_set - archive_set)
    evidence["archive_missing_from_api"] = sorted(archive_set - api_set)
    evidence["api_archive_date_mismatches"] = mismatch
    in_window = {
        url for url, item in api_all.items()
        if start <= datetime.fromisoformat(item["date"]).date() <= end
    }
    _api_posts.update(api_all)
    _api_posts.update(special_all)
    sitemap_urls, modified_candidates, special_sitemap, special_modified, correction_sitemap, correction_modified = _sitemap_inventory(start, evidence)
    evidence["api_window_missing_from_sitemap"] = sorted(in_window - sitemap_urls)
    sitemap_extra_in_window = []
    sitemap_extra_failures = []
    sitemap_unresolved_candidates = []
    for url in sorted(modified_candidates - api_set):
        try:
            item = _parse_page(url)
            _sitemap_page_cache[url] = item
            published = item.get("date_publish")
            if isinstance(published, datetime) and start <= published.date() <= end:
                sitemap_extra_in_window.append(url)
            elif not isinstance(published, datetime):
                sitemap_unresolved_candidates.append(url)
        except Exception as exc:
            sitemap_extra_failures.append({"url": url, "error": str(exc)})
    evidence["sitemap_only_in_window"] = sitemap_extra_in_window
    evidence["sitemap_extra_page_failures"] = sitemap_extra_failures
    evidence["sitemap_unresolved_candidates"] = sitemap_unresolved_candidates
    archive_only_in_window = sorted(
        url for url in archive_set - api_set if start <= archive_all[url] <= end
    )
    evidence["archive_only_in_window"] = archive_only_in_window
    special_window = {
        url for url, item in special_all.items()
        if start <= datetime.fromisoformat(item["date"]).date() <= end
    }
    evidence["special_api_in_window"] = len(special_window)
    evidence["special_api_window_missing_from_sitemap"] = sorted(special_window - special_sitemap)
    special_sitemap_only_in_window = []
    special_sitemap_failures = []
    special_unresolved = []
    landing = normalize_url(f"{BASE}/especiais/", Config.source_name)
    evidence["special_landing_excluded"] = landing if landing in special_modified and landing not in special_all else None
    for url in sorted(special_modified - set(special_all) - {landing}):
        try:
            item = _parse_page(url)
            _sitemap_page_cache[url] = item
            published = item.get("date_publish")
            if isinstance(published, datetime) and start <= published.date() <= end:
                special_sitemap_only_in_window.append(url)
            elif not isinstance(published, datetime):
                special_unresolved.append(url)
        except Exception as exc:
            special_sitemap_failures.append({"url": url, "error": str(exc)})
    evidence["special_sitemap_only_in_window"] = special_sitemap_only_in_window
    evidence["special_sitemap_page_failures"] = special_sitemap_failures
    evidence["special_sitemap_unresolved_candidates"] = special_unresolved

    correction_type = fetch(CORRECTION_TYPE).json()
    correction_type_verified = (
        isinstance(correction_type, dict) and correction_type.get("slug") == "correcao"
        and correction_type.get("rest_base") == "correcao"
        and correction_type.get("name") == "Correções"
    )
    correction_window = {
        url for url, item in correction_all.items()
        if start <= datetime.fromisoformat(item["date"]).date() <= end
    }
    evidence["correction_policy_exclusion"] = {
        "publisher_rest_type": "correcao", "publisher_rest_name": correction_type.get("name") if isinstance(correction_type, dict) else None,
        "verified": correction_type_verified, "reason": "publisher correction notices rather than news or special reports",
        "in_window_urls": sorted(correction_window),
    }
    evidence["correction_api_window_missing_from_sitemap"] = sorted(correction_window - correction_sitemap)
    correction_missing = []
    correction_failures = []
    correction_spillovers = []
    for url in sorted(correction_modified - set(correction_all)):
        try:
            published, details = _verify_correction_publication(url)
        except Exception as exc:
            correction_missing.append(url)
            correction_failures.append({"url": url, "error": str(exc)})
            continue
        if start <= published.date() <= end:
            correction_missing.append(url)
            correction_failures.append({"url": url, "error": "correction published in requested window but absent from full-month REST inventory", **details})
        else:
            correction_spillovers.append({**details, "window_relation": "before" if published.date() < start else "after"})
    evidence["correction_modified_not_in_api_full_months"] = correction_missing
    evidence["correction_candidate_verification_failures"] = correction_failures
    evidence["correction_sitemap_out_of_window_spillovers"] = correction_spillovers
    evidence["coverage_complete"] = not any((
        evidence["api_archive_missing_from_archive"], evidence["archive_missing_from_api"],
        mismatch, evidence["api_window_missing_from_sitemap"],
        sitemap_extra_in_window, sitemap_extra_failures, sitemap_unresolved_candidates,
        evidence["special_api_window_missing_from_sitemap"], special_sitemap_only_in_window,
        special_sitemap_failures, special_unresolved,
        evidence["correction_api_window_missing_from_sitemap"],
        evidence["correction_modified_not_in_api_full_months"],
        evidence["other_custom_sitemaps_requiring_policy"],
        not correction_type_verified,
    ))
    selected = in_window | set(sitemap_extra_in_window) | set(archive_only_in_window) | special_window | set(special_sitemap_only_in_window)
    return sorted(selected, key=lambda url: ((api_all.get(url) or special_all.get(url) or {}).get("date", ""), url))


def _publication_local(raw: object) -> datetime | None:
    value = clean(raw)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(MANAUS).replace(tzinfo=None) if parsed.tzinfo else parsed


def _article_body(soup: BeautifulSoup, special: bool = False) -> tuple[str | None, dict]:
    node = soup.select_one("section.especiais-container aside" if special else "article.single aside")
    if node is None:
        return None, {"body_container_found": False}
    cloned = BeautifulSoup(str(node), "html.parser")
    for bad in cloned.select("script, style, iframe, form, nav, figure, figcaption, .share, .related, .co-authors-bg"):
        bad.decompose()
    paragraphs = []
    captions_removed = 0
    removed_byline = removed_tail = None
    for block in cloned.select("p, h2, h3"):
        line = clean(block.get_text(" ", strip=True))
        if not line:
            continue
        if not paragraphs and re.match(r"^Por\s+.{3,180}$", line, re.I):
            removed_byline = line
            continue
        if AUTHOR_SECTION.match(line) or FOOTER_START.match(line):
            removed_tail = line[:200]
            break
        if special and len(line) < 500 and SPECIAL_IMAGE_CAPTION.search(line):
            captions_removed += 1
            continue
        if line not in paragraphs:
            paragraphs.append(line)
    return "\n\n".join(paragraphs) or None, {
        "body_container_found": True, "paragraphs_kept": len(paragraphs),
        "image_captions_removed": captions_removed,
        "leading_byline_removed": removed_byline,
        "tail_stop_marker": removed_tail,
    }


def _parse_page(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    special = "/especiais/" in url
    title_node = soup.select_one("h1" if special else "article.single h1")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    maintext, cleanup = _article_body(soup, special)
    visible_node = next((n for n in soup.select(".section-post-publish .text-author-single-top span") if "Publicado em:" in n.get_text(" ", strip=True)), None)
    visible_raw = clean(visible_node.get_text(" ", strip=True)) if visible_node else None
    match = VISIBLE_DATE.search(visible_raw or "")
    visible = None
    if match:
        day, month, year, hour, minute = map(int, match.groups())
        try:
            visible = datetime(year, month, day, hour, minute)
        except ValueError:
            pass
    meta_node = soup.select_one('meta[property="article:published_time"]')
    meta_raw = clean(meta_node.get("content")) if meta_node else None
    meta_local = _publication_local(meta_raw)
    schema_published = []
    webpage_published = []
    schema_titles = []
    for item in jsonld_objects(soup):
        kinds = item.get("@type")
        kinds = set(kinds if isinstance(kinds, list) else [kinds])
        if "WebPage" in kinds and item.get("datePublished"):
            webpage_published.append({"raw": item["datePublished"], "local": _publication_local(item["datePublished"])})
        if not any("Article" in (kind or "") for kind in kinds):
            continue
        if item.get("datePublished"):
            schema_published.append({"raw": item["datePublished"], "local": _publication_local(item["datePublished"])})
        headline = clean(html.unescape(item.get("headline") or ""))
        if headline:
            schema_titles.append(headline)
    og_node = soup.select_one('meta[property="og:title"]')
    og_title = clean(og_node.get("content")) if og_node else None
    dates = []
    if visible:
        dates.append({"kind": "visible_time", "source": ".section-post-publish", "raw": visible.isoformat()})
    if meta_raw:
        dates.append({"kind": "published", "source": "meta.article:published_time", "raw": meta_raw})
    for item in schema_published:
        dates.append({"kind": "published", "source": "jsonld.datePublished", "raw": item["raw"]})
    for item in webpage_published:
        dates.append({"kind": "published", "source": "jsonld.WebPage.datePublished", "raw": item["raw"]})
    published = next((item["local"] for item in schema_published if item["local"]), None) if special else visible
    selected_date_source = "jsonld.Article.datePublished" if special else ".section-post-publish visible Publicado em"
    title_source = "h1" if special else "article.single h1"
    body_source = "section.especiais-container aside" if special else "article.single aside"
    title_evidence = ([{"kind": "visible_h1", "raw": title, "source": title_source}] if title else [])
    title_evidence.extend({"kind": "jsonld_headline", "raw": x, "source": "jsonld.headline"} for x in schema_titles)
    if og_title:
        title_evidence.append({"kind": "og_title", "raw": og_title, "source": "meta.og:title"})
    parser_errors = []
    if published is None:
        parser_errors.append("article_publication_missing" if special else "visible_publication_missing")
    if published and meta_local and abs((published - meta_local).total_seconds()) > 120:
        parser_errors.append("visible_vs_meta_publication_disagreement")
    if published and any(item["local"] and abs((published - item["local"]).total_seconds()) > 120 for item in schema_published):
        parser_errors.append("visible_vs_jsonld_publication_disagreement")
    if special and published and any(item["local"] and abs((published - item["local"]).total_seconds()) > 120 for item in webpage_published):
        parser_errors.append("article_vs_webpage_publication_disagreement")
    if title and schema_titles and max(SequenceMatcher(None, title.casefold(), candidate.casefold()).ratio() for candidate in schema_titles) < .78:
        parser_errors.append("visible_vs_schema_title_disagreement")
    api = _api_posts.get(url)
    if api and published:
        api_date = _publication_local(api["date"])
        if api_date and abs((published - api_date).total_seconds()) > 120:
            parser_errors.append("article_vs_api_publication_disagreement" if special else "visible_vs_api_publication_disagreement")
    if api and title:
        api_title = clean(BeautifulSoup(html.unescape(api.get("title", {}).get("rendered", "")), "html.parser").get_text(" ", strip=True))
        if api_title and SequenceMatcher(None, title.casefold(), api_title.casefold()).ratio() < .78:
            parser_errors.append("visible_vs_api_title_disagreement")
    api_content = api.get("content", {}) if api else {}
    api_body = clean(BeautifulSoup(api_content.get("rendered", ""), "html.parser").get_text(" ", strip=True)) if isinstance(api_content, dict) else None
    if isinstance(api_content, dict) and api_content.get("protected"):
        parser_errors.append("api_content_protected")
    if maintext and api_body:
        page_words = set(re.findall(r"\w+", maintext.casefold()))
        api_words = set(re.findall(r"\w+", api_body.casefold()))
        if len(page_words & api_words) / max(1, min(len(page_words), len(api_words))) < .55:
            parser_errors.append("page_vs_api_maintext_disagreement")
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "date_publish": published, "maintext": maintext,
        "language": "pt", "parser": "pure_adapter", "collection": None,
        "parser_error": ";".join(parser_errors) or None,
        "field_sources": {"title": title_source, "date_publish": selected_date_source, "maintext": body_source},
        "field_evidence": {
            "titles": title_evidence, "dates": dates,
            "bodies": ([{"kind": "wordpress_content", "source": "wp-json.post.content.rendered", "text": api_body[:20000]}] if api_body else []) + ([{"kind": "article_body_cleaned", "text": maintext[:20000]}] if maintext else []),
            "http_status": response.status_code, "visible_publication_raw": visible_raw,
            "api_publication_raw": api.get("date") if api else None,
            "publisher_response_sha256": hashlib.sha256(response.content).hexdigest(),
            "publisher_response_bytes": len(response.content),
            "body_cleanup": cleanup,
        },
    }


def scrape_url(url: str) -> dict:
    return _sitemap_page_cache.get(url) or _parse_page(url)
