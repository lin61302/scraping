"""Read-only Repórter Brasil post observer.

Discovery reconciles a count-checked WordPress post API, every publisher post
sitemap, and the descending Jornalismo archive. Sitemap ``lastmod`` and URL
months select pages for verification; neither is a publication date.
"""
from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup
from dateutil.tz import gettz

from adapters.BRA.common import SESSION, clean, fetch, jsonld_objects, normalize_url, sitemap_entries


class Config:
    source_name = "reporterbrasil.org.br"
    country_code = "BRA"
    is_env = True


BASE = "https://reporterbrasil.org.br"
API = BASE + "/wp-json/wp/v2/posts"
ARCHIVE = BASE + "/jornalismo/"
SITEMAP_INDEX = BASE + "/sitemap_index.xml"
POST_MAP = re.compile(r"/post-sitemap\d*\.xml$", re.I)
PATH_MONTH = re.compile(r"^/(20\d{2})/(\d{2})/[^/]+/?$")
VISIBLE_DATE = re.compile(r"\b(\d{2})/(\d{2})/(20\d{2})(?:\s+(\d{1,2}):(\d{2}))?\b")
POST_ID = re.compile(r"(?:^|\s)post-(\d+)(?:\s|$)")
BODY_POST_ID = re.compile(r"(?:^|\s)postid-(\d+)(?:\s|$)")
PAGE_SIZE = 100
MAX_API_PAGES = 25
MAX_ARCHIVE_PAGES = 80
MAX_DAYS = 180
SAO_PAULO = gettz("America/Sao_Paulo")
if SAO_PAULO is None:
    raise RuntimeError("America/Sao_Paulo timezone unavailable")

# The publisher puts replies, annexes and press notes in post records too.
# They are retained in the discovery audit with their original URL and reason.
AUXILIARY_CATEGORIES = {6: "annex_or_statement", 13: "note_or_release"}
NEWS_CATEGORIES = {8, 10, 12, 16}
API_POSTS: dict[str, dict] = {}
ARCHIVE_DATES: dict[str, date] = {}
SITEMAP_VERIFIED: dict[str, dict] = {}


def _url(raw: str) -> str | None:
    return normalize_url(raw, Config.source_name)


def _date(raw: str | None) -> datetime | None:
    match = VISIBLE_DATE.search(raw or "")
    if not match:
        return None
    day, month, year, hour, minute = match.groups()
    try:
        return datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
    except ValueError:
        return None


def _utc_to_local(raw: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw) if raw else None
        return value.astimezone(SAO_PAULO).replace(tzinfo=None) if value and value.tzinfo else value
    except ValueError:
        return None


def _rest_inventory(start: date, end: date, evidence: dict) -> dict[str, dict]:
    params = {
        "after": f"{start - timedelta(days=1)}T23:59:59",
        "before": f"{end + timedelta(days=1)}T00:00:00",
        "per_page": PAGE_SIZE,
        "orderby": "date", "order": "desc",
        "_fields": "id,date,link,title,content,categories,status,type",
    }
    rows = []
    pages = []
    expected = page_count = None
    for page in range(1, MAX_API_PAGES + 1):
        response = fetch(f"{API}?{urlencode({**params, 'page': page})}")
        items = response.json()
        if not isinstance(items, list):
            raise ValueError(f"post REST page {page} did not return a list")
        try:
            total = int(response.headers["X-WP-Total"])
            total_pages = int(response.headers["X-WP-TotalPages"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("post REST pagination headers missing") from exc
        if expected is None:
            expected, page_count = total, total_pages
            if page_count != math.ceil(total / PAGE_SIZE) or page_count > MAX_API_PAGES:
                raise ValueError("post REST page count is inconsistent or excessive")
        if (total, total_pages) != (expected, page_count):
            raise ValueError("post REST count changed during pagination")
        rows.extend(items)
        pages.append({"page": page, "rows": len(items), "total": total,
                      "total_pages": total_pages, "sha256": hashlib.sha256(response.content).hexdigest()})
        if page >= max(1, page_count):
            break
    ids = [item.get("id") for item in rows if isinstance(item, dict)]
    if len(rows) != expected or len(ids) != len(rows) or len(set(ids)) != len(ids):
        raise ValueError("post REST inventory has a missing or repeated ID")
    result = {}
    for item in rows:
        url = _url(item.get("link", ""))
        published = _utc_to_local(item.get("date"))
        if not url or not published or not isinstance(item.get("categories"), list):
            raise ValueError("post REST item has invalid URL, date, or categories")
        if item.get("type") not in (None, "post") or item.get("status") not in (None, "publish"):
            raise ValueError(f"post REST endpoint returned another content type/state: {url}")
        if not start <= published.date() <= end:
            raise ValueError(f"post REST page escaped requested publication window: {url}")
        if url in result:
            raise ValueError(f"post REST inventory repeated URL: {url}")
        result[url] = item
    evidence["rest_pages"] = pages
    evidence["rest_window_urls"] = len(result)
    return result


def _archive_cards(soup: BeautifulSoup) -> list[tuple[str, date, int]]:
    cards = soup.select(".e-loop-item")
    if len(cards) < 4:
        raise ValueError("Jornalismo archive has no dynamic story cards")
    found = []
    for card in cards:
        link = card.select_one(".elementor-widget-theme-post-title a[href]")
        value = _url(link.get("href", "")) if link else None
        published = None
        for info in card.select(".elementor-widget-post-info"):
            published = _date(info.get_text(" ", strip=True))
            if published:
                break
        id_match = POST_ID.search(" ".join(card.get("class", [])))
        if not value or not published or not id_match:
            raise ValueError("Jornalismo archive card lacks URL, displayed date, or post ID")
        found.append((value, published.date(), int(id_match.group(1))))
    if len({url for url, _, _ in found}) != len(found):
        raise ValueError("Jornalismo archive page repeated a URL")
    return found


def _archive_inventory(start: date, end: date, evidence: dict) -> dict[str, date]:
    seen: set[str] = set()
    in_window: dict[str, date] = {}
    featured: tuple[str, ...] | None = None
    previous: date | None = None
    lower_reached = False
    evidence["archive_pages"] = []
    for page in range(1, MAX_ARCHIVE_PAGES + 1):
        url = ARCHIVE if page == 1 else f"{ARCHIVE}?e-page-f8294ee={page}"
        response = fetch(url)
        soup = BeautifulSoup(response.content, "html.parser")
        cards = _archive_cards(soup)
        # The first three hero cards repeat verbatim on every archive page.
        hero = tuple(row[0] for row in cards[:3])
        if featured is None:
            featured = hero
        elif hero != featured:
            raise ValueError("Jornalismo featured cards shifted during pagination")
        current = cards if page == 1 else cards[3:]
        if not current:
            raise ValueError("Jornalismo archive page has no new cards")
        for card_url, published, post_id in current:
            if card_url in seen:
                raise ValueError(f"Jornalismo archive repeated a story across pages: {card_url}")
            seen.add(card_url)
            if previous and published > previous:
                raise ValueError(f"Jornalismo archive date order increased at {card_url}")
            previous = published
            if start <= published <= end:
                in_window[card_url] = published
                ARCHIVE_DATES[card_url] = published
        next_page = f"{ARCHIVE}?e-page-f8294ee={page + 1}"
        has_next = any(a.get("href", "").rstrip("/") == next_page.rstrip("/") for a in soup.select("a[href]"))
        evidence["archive_pages"].append({
            "url": url, "sha256": hashlib.sha256(response.content).hexdigest(),
            "cards": len(cards), "new_cards": len(current), "first_new_date": current[0][1].isoformat(),
            "last_new_date": current[-1][1].isoformat(), "next_link": has_next,
        })
        if current[-1][1] < start:
            lower_reached = True
            break
        if not has_next:
            raise ValueError("Jornalismo archive ended before requested lower boundary")
    evidence["archive_lower_boundary_reached"] = lower_reached
    evidence["archive_window_urls"] = len(in_window)
    return in_window


def _sitemap_inventory(start: date, end: date, evidence: dict) -> set[str]:
    index_response = fetch(SITEMAP_INDEX)
    index = BeautifulSoup(index_response.content, "xml")
    maps = [url for url, _ in sitemap_entries(index) if POST_MAP.search(url)]
    if not maps:
        raise ValueError("publisher sitemap index has no post maps")
    evidence["sitemap_index_sha256"] = hashlib.sha256(index_response.content).hexdigest()
    evidence["post_sitemaps"] = []
    selected: set[str] = set()
    all_urls: set[str] = set()
    for map_url in maps:
        response = fetch(map_url)
        entries = sitemap_entries(BeautifulSoup(response.content, "xml"))
        for raw, _ in entries:
            url = _url(raw)
            if not url or url in all_urls:
                raise ValueError(f"post sitemap has invalid or duplicated URL: {raw}")
            all_urls.add(url)
            match = PATH_MONTH.match(urlparse(url).path)
            if match:
                month_start = date(int(match.group(1)), int(match.group(2)), 1)
                next_month = date(month_start.year + month_start.month // 12, month_start.month % 12 + 1, 1)
                if month_start <= end and next_month > start:
                    selected.add(url)
        evidence["post_sitemaps"].append({"url": map_url, "entries": len(entries),
                                           "sha256": hashlib.sha256(response.content).hexdigest()})
    evidence["all_post_sitemap_urls"] = len(all_urls)
    evidence["sitemap_window_month_path_candidates"] = len(selected)
    evidence["sitemap_lastmod_is_not_publication_date"] = True
    return selected


def _scope_reason(item: dict) -> str | None:
    categories = set(item.get("categories") or [])
    for category, reason in AUXILIARY_CATEGORIES.items():
        if category in categories:
            return reason
    if not categories & NEWS_CATEGORIES:
        return "outside_original_reporting_taxonomy"
    return None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > MAX_DAYS:
        raise ValueError(f"invalid Repórter Brasil date window (max {MAX_DAYS + 1} days)")
    API_POSTS.clear()
    ARCHIVE_DATES.clear()
    SITEMAP_VERIFIED.clear()
    evidence.update({"method": "count_checked_post_rest_plus_all_post_sitemaps_plus_dated_jornalismo_archive",
                     "scope": "published original reporting posts, including English and Spanish; auxiliary statements and notes excluded",
                     "coverage_complete": False})
    api = _rest_inventory(start, end, evidence)
    archive = _archive_inventory(start, end, evidence)
    sitemap = _sitemap_inventory(start, end, evidence)
    API_POSTS.update(api)
    evidence["archive_urls_missing_from_rest"] = sorted(set(archive) - set(api))
    evidence["rest_urls_missing_from_archive"] = sorted(set(api) - set(archive))
    evidence["rest_urls_missing_from_sitemap"] = sorted(set(api) - sitemap)
    evidence["archive_rest_date_disagreements"] = sorted(
        url for url in set(api) & set(archive)
        if _utc_to_local(api[url]["date"]).date() != archive[url]
    )
    sitemap_only = sorted(sitemap - set(api))
    evidence["sitemap_only_page_checks"] = []
    additional: set[str] = set()
    unresolved: set[str] = set()
    for url in sitemap_only:
        try:
            article = _parse_page(url)
            SITEMAP_VERIFIED[url] = article
            published = article["date_publish"]
            in_window = isinstance(published, datetime) and start <= published.date() <= end
            evidence["sitemap_only_page_checks"].append({"url": url,
                "date_publish": published.isoformat() if isinstance(published, datetime) else None,
                "in_window": in_window, "parser_error": article["parser_error"]})
            if in_window:
                additional.add(url)
            elif not published:
                unresolved.add(url)
        except Exception as exc:
            evidence["sitemap_only_page_checks"].append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            unresolved.add(url)
    candidates = set(api) | set(archive) | additional
    excluded = []
    selected = []
    for url in sorted(candidates):
        item = api.get(url)
        reason = _scope_reason(item) if item else None
        if reason:
            excluded.append({"url": url, "reason": reason, "categories": item["categories"]})
        else:
            selected.append(url)
    evidence["policy_exclusions"] = excluded
    evidence["selected_urls"] = len(selected)
    evidence["selected_url_list"] = selected
    evidence["sitemap_only_unresolved"] = sorted(unresolved)
    evidence["sitemap_only_in_window"] = sorted(additional)
    # REST may include noindex posts, which explains sitemap omissions. REST
    # and archive together remain the authoritative publication inventory.
    evidence["coverage_complete"] = bool(
        evidence["archive_lower_boundary_reached"]
        and not evidence["archive_urls_missing_from_rest"]
        and not evidence["archive_rest_date_disagreements"]
        and not additional and not unresolved
    )
    return selected


def _article_ld(soup: BeautifulSoup) -> dict | None:
    for obj in jsonld_objects(soup):
        value = obj.get("@type")
        types = value if isinstance(value, list) else [value]
        if {"Article", "NewsArticle", "ReportageNewsArticle"} & set(types):
            return obj
    return None


def _body(soup: BeautifulSoup) -> tuple[str | None, list[str]]:
    node = soup.select_one(".elementor-widget-theme-post-content")
    if node is None:
        return None, []
    copy = BeautifulSoup(str(node), "html.parser")
    for bad in copy.select("script, style, iframe, form, nav, aside, figure, figcaption, "
                           ".newsletter, .share, .social, .related, [class*='newsletter'], [class*='related']"):
        bad.decompose()
    paragraphs = []
    for item in copy.select("p, h2, h3, li, blockquote"):
        if item.name == "blockquote" and item.select("p"):
            continue
        if item.find_parent("blockquote") and item.name != "blockquote":
            continue
        value = clean(item.get_text(" ", strip=True))
        if value and value not in paragraphs and not value.casefold().startswith(("leia também", "assine nossa newsletter")):
            paragraphs.append(value)
    return "\n\n".join(paragraphs) or None, paragraphs


def _rest_content_duplicates_visible_body(paragraphs: list[str], api_body: str) -> bool:
    """Recognize publisher REST markup that repeats a complete visible story.

    A short visible body remains an error unless every substantial visible
    paragraph occurs in REST and most paragraphs occur there twice. This is
    specific to the publisher's duplicate hidden Elementor content observed
    on the June 3 tariff article; it does not accept a merely short extract.
    """
    rest = " ".join(api_body.split()).casefold()
    substantial = [" ".join(part.split()).casefold() for part in paragraphs if len(part) >= 100]
    return bool(len(substantial) >= 8 and all(part in rest for part in substantial)
                and sum(rest.count(part) >= 2 for part in substantial) >= math.ceil(len(substantial) * 0.7))


def _canonical_target(url: str) -> dict:
    """Record the publisher's declared target without promoting it as a URL."""
    try:
        response = SESSION.get(url, timeout=25, allow_redirects=True)
        result = {"status": response.status_code, "final_url": _url(response.url),
                  "sha256": hashlib.sha256(response.content).hexdigest()}
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            match = BODY_POST_ID.search(" ".join(soup.body.get("class", []))) if soup.body else None
            result["page_post_id"] = int(match.group(1)) if match else None
        return result
    except Exception as exc:
        return {"status": None, "error": f"{type(exc).__name__}: {exc}"}


def _parse_page(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = _url(canonical.get("href", "")) if canonical else None
    issues = []
    warnings = []
    canonical_alias = None
    title_nodes = [node for node in soup.select(".elementor-widget-heading h2, .elementor-widget-heading h1")
                   if not node.find_parent(class_="e-loop-item")]
    visible_title = clean(title_nodes[0].get_text(" ", strip=True)) if title_nodes else None
    og = soup.select_one('meta[property="og:title"]')
    og_title = clean(og.get("content")) if og else None
    title = visible_title
    info_nodes = [node for node in soup.select(".elementor-widget-post-info")
                  if not node.find_parent(class_="e-loop-item")]
    visible_raw = None
    published = None
    for node in info_nodes:
        raw = clean(node.get_text(" ", strip=True))
        parsed = _date(raw)
        if parsed:
            visible_raw, published = raw, parsed
            break
    api = API_POSTS.get(url, {})
    api_date = _utc_to_local(api.get("date"))
    api_title = clean(BeautifulSoup(html.unescape(api.get("title", {}).get("rendered", "")), "html.parser").get_text(" ", strip=True)) if api else None
    api_body = clean(BeautifulSoup(api.get("content", {}).get("rendered", ""), "html.parser").get_text(" ", strip=True)) if api else None
    ld = _article_ld(soup)
    ld_date = _utc_to_local(ld.get("datePublished")) if ld else None
    ld_title = clean(ld.get("headline")) if ld else None
    meta = soup.select_one('meta[property="article:published_time"]')
    meta_date = _utc_to_local(meta.get("content")) if meta else None
    body, paragraphs = _body(soup)
    if not title or not published or not body:
        issues.append("required_visible_field_missing")
    if title and api_title and SequenceMatcher(None, title.casefold(), api_title.casefold()).ratio() < 0.8:
        issues.append("visible_vs_rest_title_disagreement")
    if title and og_title and SequenceMatcher(None, title.casefold(), og_title.casefold()).ratio() < 0.8:
        if api_title and SequenceMatcher(None, title.casefold(), api_title.casefold()).ratio() >= 0.8:
            warnings.append("stale_og_title_visible_and_rest_agree")
        else:
            issues.append("visible_vs_og_title_disagreement")
    for label, other in (("rest", api_date), ("jsonld", ld_date), ("meta", meta_date)):
        if published and other and abs((published - other).total_seconds()) >= 60:
            issues.append(f"visible_vs_{label}_publication_disagreement")
    if published and url in ARCHIVE_DATES and published.date() != ARCHIVE_DATES[url]:
        issues.append("archive_vs_article_publication_disagreement")
    if body and len(body) < 180:
        issues.append("maintext_short")
    if api_body and body and len(body) < len(api_body) * 0.65:
        if _rest_content_duplicates_visible_body(paragraphs, api_body):
            warnings.append("rest_content_duplicates_complete_visible_body")
        else:
            issues.append("page_body_shorter_than_rest_content")
    if canonical_url != url:
        page_match = BODY_POST_ID.search(" ".join(soup.body.get("class", []))) if soup.body else None
        page_id = int(page_match.group(1)) if page_match else None
        rest_id = api.get("id")
        same_identity = bool(
            rest_id and page_id == rest_id and title and api_title and published and api_date
            and SequenceMatcher(None, title.casefold(), api_title.casefold()).ratio() >= 0.8
            and abs((published - api_date).total_seconds()) < 60
        )
        canonical_alias = {"requested_rest_url": url, "declared_canonical_url": canonical_url,
                           "rest_id": rest_id, "page_post_id": page_id,
                           "rest_page_title_and_publication_agree": same_identity}
        if same_identity and canonical_url:
            target = _canonical_target(canonical_url)
            canonical_alias["declared_target"] = target
            if target.get("status") == 200 and target.get("page_post_id") is None:
                issues.append("canonical_target_identity_unverified")
            else:
                warnings.append("declared_canonical_alias_verified_by_rest_page_identity")
                if target.get("status") != 200:
                    warnings.append("declared_canonical_target_unavailable")
                elif target.get("page_post_id") != rest_id:
                    # Two verified June posts point their SEO canonical tags
                    # at *other* published posts. The requested page's own
                    # WordPress ID, title and original date still match REST.
                    warnings.append("declared_canonical_points_to_other_published_post")
        else:
            issues.append("canonical_url_disagreement")
    precise = api_date if published and api_date and abs((published - api_date).total_seconds()) < 60 else published
    date_evidence = [{"kind": "visible_time", "source": "article post-info", "raw": visible_raw}] if visible_raw else []
    for kind, source, raw in (("published", "wp-json date", api.get("date")),
                              ("published", "jsonld.datePublished", ld.get("datePublished") if ld else None),
                              ("published", "article:published_time", meta.get("content") if meta else None)):
        if raw:
            date_evidence.append({"kind": kind, "source": source, "raw": raw})
    titles = [{"kind": "visible_heading", "source": "article h2/h1", "raw": title}] if title else []
    for kind, raw in (("rest_title", api_title), ("og_title", og_title), ("jsonld_headline", ld_title)):
        if raw:
            titles.append({"kind": kind, "raw": raw})
    bodies = [{"kind": "scoped_visible_paragraphs", "source": ".elementor-widget-theme-post-content", "text": body[:20000]}] if body else []
    if api_body:
        bodies.append({"kind": "rest_rendered_content", "source": "wp-json content.rendered", "text": api_body[:20000]})
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "date_publish": precise, "maintext": body,
        "language": "en" if 10 in set(api.get("categories") or []) else "es" if 12 in set(api.get("categories") or []) else "pt",
        "parser": "pure_adapter", "collection": None, "parser_error": ",".join(issues) or None,
        "field_sources": {"title": "article h2/h1 outside archive cards", "date_publish": "article post-info",
                          "maintext": ".elementor-widget-theme-post-content"},
        "field_evidence": {"http_status": response.status_code, "titles": titles, "dates": date_evidence,
                           "bodies": bodies, "canonical_url": canonical_url,
                           "canonical_alias": canonical_alias, "rest_id": api.get("id"),
                           "archive_date": ARCHIVE_DATES.get(url).isoformat() if url in ARCHIVE_DATES else None,
                           "paragraph_count": len(paragraphs),
                           "first_paragraph": paragraphs[0][:200] if paragraphs else None,
                           "last_paragraph": paragraphs[-1][:200] if paragraphs else None,
                           "page_sha256": hashlib.sha256(response.content).hexdigest(),
                           "metadata_warnings": warnings},
    }


def scrape_url(url: str) -> dict:
    if _url(url) != url:
        raise ValueError(f"not a canonical Repórter Brasil article URL: {url}")
    return SITEMAP_VERIFIED.pop(url) if url in SITEMAP_VERIFIED else _parse_page(url)
