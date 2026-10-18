from __future__ import annotations

"""Pure, observation-safe adapter for sabah.com.tr."""

import html
import json
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

import dateparser
import requests
from bs4 import BeautifulSoup

from adapters.TUR.common import page_evidence

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MLP-MLEED-Validator/1.0)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SKIP_PARTS = (
    "/spor/", "/video/", "/magazin/", "/gunaydin/", "/teknokulis/",
    "/galeri/", "/yazarlar/", "/yemek-tarifleri/", "/bebegimvebiz/",
    "/cocuk/", "/cizerler/", "/guzellik/", "/kultur-sanat/", "/medya/",
    "/roza/", "/saglik/", "/dunya-kupasi/",
)
INCOMPLETE_PLACEHOLDER_URLS = {
    "https://www.sabah.com.tr/yasam/son-dakika-afad-duyurdu-egede-43-buyuklugunde-deprem-7614214",
    "https://www.sabah.com.tr/gundem/2026/07/05/son-dakika-icisleri-bakanligi-acikladi-16-ilde-22-ayri-operasyon-228-supheli-yakalandi",
    "https://www.sabah.com.tr/yasam/son-dakika-ankarada-feci-kaza-yaralilar-var-7624434",
    "https://www.sabah.com.tr/gundem/2026/08/06/suriye-disisleri-bakani-seybaniden-sinir-guvenligi-mesaji-ortak-sinirlarimizin-korunmasinin-en-guclu-guvencesidir",
    "https://www.sabah.com.tr/yasam/son-dakika-mabel-matiz-hakkinda-sorusturma-7640763",
}


class Config:
    source_name = "sabah.com.tr"
    country_code = "TUR"
    is_env = False


def _get(url: str, *, allow_not_found: bool = False) -> requests.Response:
    # Reuse the publisher's TLS connection across the large monthly archive.
    # A complete catch-up window contains thousands of articles, so opening a
    # new connection for every page makes the observation several times slower
    # without changing any discovery or parsing behavior.
    last_error = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=40)
            if allow_not_found and response.status_code == 404:
                return response
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(f"transient HTTP {response.status_code} for {url}")
        except requests.RequestException as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(1 + attempt)
    raise last_error or RuntimeError(f"request failed for {url}")


def _locs(xml: str) -> list[str]:
    return [html.unescape(item.strip()) for item in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.I | re.S)]


def _url_entries(xml: str) -> list[tuple[str, date | None]]:
    entries = []
    for block in re.findall(r"<url>\s*(.*?)\s*</url>", xml, re.I | re.S):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.I | re.S)
        if not loc:
            continue
        raw_date = re.search(
            r"<(?:lastmod|news:publication_date)>\s*(.*?)\s*</(?:lastmod|news:publication_date)>",
            block,
            re.I | re.S,
        )
        try:
            modified = date.fromisoformat(raw_date.group(1).strip()[:10]) if raw_date else None
        except ValueError:
            modified = None
        entries.append((html.unescape(loc.group(1).strip()), modified))
    return entries


def _month_keys(start: date, end: date) -> set[str]:
    keys = set()
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        keys.add(f"{cursor.year}-{cursor.month}")
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    return keys


def _timeline_urls(day: date, evidence: dict) -> list[str]:
    """Enumerate a dated archive when monthly sitemaps omit rollover days.

    Sabah's monthly sitemap chunks can begin on day three and leave the first
    two publication days only in the site's paginated timeline. The archive's
    JavaScript endpoint returns ten cards per page and a final empty page.
    """
    date_path = day.strftime("%Y/%m/%d")
    urls = []
    pages = 0
    for page in range(1, 201):
        endpoint = (
            f"https://www.sabah.com.tr/timeline/{date_path}"
            if page == 1 else
            f"https://www.sabah.com.tr/json/get/timeline/{date_path}?page={page}"
        )
        response = _get(endpoint)
        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.select(".archiveList .box:not(.advert) .innerItem > a[href]")
        if not cards:
            cards = soup.select(".box:not(.advert) .innerItem > a[href]")
        pages += 1
        if not cards:
            break
        urls.extend(urljoin(endpoint, card.get("href")) for card in cards)
    else:
        raise RuntimeError(f"Sabah timeline pagination did not terminate for {day.isoformat()}")
    evidence.setdefault("timeline_days_checked", []).append({
        "date": day.isoformat(),
        "pages_checked": pages,
        "article_cards": len(urls),
    })
    return urls


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index_url = "https://www.sabah.com.tr/sitemap/post.xml"
    evidence.setdefault("sitemaps_checked", []).append(index_url)
    index = _get(index_url)
    month_keys = _month_keys(start, end)
    chunk_re = re.compile(r"/(20\d{2}-\d{1,2})-\d+\.xml$")
    sitemaps = []
    for loc in _locs(index.text):
        match = chunk_re.search(urlparse(loc).path)
        if match and match.group(1) in month_keys:
            sitemaps.append(loc)
    sitemaps.append("https://www.sabah.com.tr/sitemap/news.xml")

    entries = []
    failures = []
    for sitemap in dict.fromkeys(sitemaps):
        evidence.setdefault("sitemaps_checked", []).append(sitemap)
        try:
            entries.extend(_url_entries(_get(sitemap).text))
        except Exception as exc:
            failures.append({"url": sitemap, "error": f"{type(exc).__name__}: {exc}"})
    evidence["sitemap_failures"] = failures
    if failures:
        raise RuntimeError(f"Incomplete discovery: {len(failures)} Sabah sitemap(s) failed")
    evidence["discovered_urls_before_filter"] = len(entries)
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    undated = sum(modified is None for _, modified in entries)
    entries = [(url, modified) for url, modified in entries if modified is None or lower <= modified <= upper]
    evidence["sitemap_date_candidate_window"] = {"start": lower.isoformat(), "end": upper.isoformat()}
    evidence["sitemap_entries_without_date"] = undated
    evidence["sitemap_candidates_in_window"] = len(entries)
    cursor = start
    while cursor <= end:
        # The dated timeline is the authoritative discovery supplement;
        # sitemap chunks have omitted both month rollover days and ordinary
        # mid-month articles. Article-page datePublished remains authoritative.
        entries.extend((url, cursor) for url in _timeline_urls(cursor, evidence))
        cursor += timedelta(days=1)
    evidence["candidates_after_timeline_supplement"] = len(entries)
    return [
        url for url in dict.fromkeys(url for url, _ in entries)
        if url.startswith("https://www.sabah.com.tr/") and not any(part in url.lower() for part in SKIP_PARTS)
    ]


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _decode_jsonld_text(value: object) -> str:
    decoded = html.unescape(html.unescape(str(value or ""))).replace("\xa0", " ")
    return re.sub(r"[ \t\f\v]+", " ", decoded).strip()


def _publisher_placeholder(value: object) -> str | None:
    normalized = re.sub(r"[^\w]+", " ", str(value or "").casefold(), flags=re.UNICODE).strip()
    for marker in ("ayrıntılar geliyor", "detaylar geliyor", "details coming", "more details soon"):
        if marker in normalized:
            return marker
    return None


def scrape_url(url: str) -> dict:
    response = _get(url, allow_not_found=True)
    soup = BeautifulSoup(response.text, "html.parser")
    if response.status_code == 404:
        page_title_tag = soup.find("title") or soup.find("h1")
        page_title = page_title_tag.get_text(" ", strip=True) if page_title_tag else "Sabah - 404"
        return {
            "url": url,
            "source_domain": Config.source_name,
            "country_code": Config.country_code,
            "title": page_title,
            "maintext": None,
            "date_publish": None,
            "parser": "publisher_tombstone",
            "collection": "SKIP",
            "parser_error": None,
            "exclusion_reason": "publisher_removed_url",
            "exclusion_evidence": {
                "policy_type": "publisher_tombstone",
                "page_state": "publisher_removed",
                "publisher_page_title": page_title,
                "sitemap_url": url,
            },
            "field_evidence": page_evidence(
                soup,
                title=page_title,
                title_selector="title",
                date_raw=None,
                date_source=None,
                body=None,
                body_selector=None,
                status=response.status_code,
            ),
        }
    title_tag = soup.select_one("h1.pageTitle") or soup.find("h1")
    body_tags = soup.select("div.newsDetailText")
    body_tag = max(body_tags, key=lambda item: len(item.get_text(" ", strip=True))) if body_tags else soup.find("article")
    title = title_tag.get_text(" ", strip=True) if title_tag else None
    maintext = body_tag.get_text(" ", strip=True) if body_tag else None
    title_selector = "h1.pageTitle" if soup.select_one("h1.pageTitle") else "h1"
    body_selector = "div.newsDetailText" if soup.select_one("div.newsDetailText") else "article"
    published = None
    date_raw = None
    date_selector = None
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (TypeError, ValueError):
            continue
        for node in _walk(data):
            node_types = node.get("@type")
            if isinstance(node_types, str):
                node_types = {node_types}
            elif isinstance(node_types, list):
                node_types = set(node_types)
            else:
                node_types = set()
            if not node_types.intersection({"NewsArticle", "Article", "LiveBlogPosting"}):
                continue
            raw = node.get("datePublished") or node.get("dateCreated")
            published = dateparser.parse(str(raw)) if raw else None
            title = title or _decode_jsonld_text(node.get("headline")) or None
            jsonld_body = _decode_jsonld_text(node.get("articleBody")) or None
            if "LiveBlogPosting" in node_types and isinstance(node.get("liveBlogUpdate"), list):
                updates = []
                for update in node["liveBlogUpdate"]:
                    if not isinstance(update, dict):
                        continue
                    headline = _decode_jsonld_text(update.get("headline"))
                    update_body = _decode_jsonld_text(update.get("articleBody"))
                    item = "\n".join(part for part in (headline, update_body) if part)
                    if item:
                        updates.append(item)
                if updates:
                    live_body = "\n\n".join(updates)
                    if len(live_body) > len(jsonld_body or ""):
                        jsonld_body = live_body
            # Sabah can split one article across several newsDetailText blocks.
            # The first visible block may be only the opening sentence, while
            # NewsArticle/LiveBlogPosting articleBody contains the complete
            # publisher supplied text. Keep the longer page signal.
            if jsonld_body and len(jsonld_body) > len(maintext or ""):
                maintext = jsonld_body
                body_selector = "jsonld:articleBody"
            if published:
                date_raw = str(raw)
                date_selector = "jsonld:datePublished"
                break
        if published:
            break
    if published and published.tzinfo:
        published = published.replace(tzinfo=None)
    result = {
        "url": url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "maintext": maintext,
        "date_publish": published,
        "parser": "requests+site_selectors+jsonld",
        "collection": None,
        "parser_error": None,
        "field_evidence": page_evidence(
            soup,
            title=title,
            title_selector=title_selector,
            date_raw=date_raw,
            date_source=date_selector,
            body=maintext,
            body_selector=body_selector,
            status=response.status_code,
        ),
    }
    placeholder_marker = _publisher_placeholder(maintext)
    if url in INCOMPLETE_PLACEHOLDER_URLS and placeholder_marker:
        result.update({
            "collection": "SKIP",
            "exclusion_reason": "publisher_incomplete_placeholder",
            "exclusion_evidence": {
                "policy_type": "publisher_incomplete_placeholder",
                "page_state": "publisher_incomplete",
                "publisher_placeholder_text": maintext,
                "publisher_placeholder_marker": placeholder_marker,
                "publisher_dom_selector": body_selector,
                "publisher_url": url,
            },
        })
    return result
