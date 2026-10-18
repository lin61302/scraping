"""Read-only Interpressnews observation from publisher article sitemaps.

The publisher embeds a complete, server-rendered article data block in its
HTML. Parsing that block does not require executing JavaScript or Selenium.
This module has no database access or production write path.
"""
from __future__ import annotations

import re
import hashlib
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from adapters.GEO.common import SESSION, clean, fetch, normalize_url


class Config:
    source_name = "interpressnews.ge"
    country_code = "GEO"
    is_env = False


BASE = "https://www.interpressnews.ge"
ROBOTS = BASE + "/robots.txt"
DAILY = BASE + "/sitemap/articles-daily.xml"
LATEST_SHELL = BASE + "/ka/latest-news/"
LATEST_API = BASE + "/ka/api/latestnews/"
ARTICLE_SITEMAP = re.compile(r"^https://www\.interpressnews\.ge/static/sitemaps/articles_(\d+)\.xml$", re.I)
ARTICLE_PATH = re.compile(r"^/ka/article/(\d+)-[^/]+/?$", re.I)
CSRF_TOKEN = re.compile(r"csrfmiddlewaretoken\s*=\s*['\"]([A-Za-z0-9_-]+)['\"]", re.I)
MAX_SITEMAPS = 12
MAX_WINDOW_URLS = 6000
MAX_LATEST_PAGES = 500
LATEST_PAGE_SIZE = 15
OLDER_BOUNDARY_PAGES = 2
MAX_WINDOW_DAYS = 120


def _article_id(raw: str) -> int | None:
    parsed = urlparse(raw)
    if parsed.hostname not in {"interpressnews.ge", "www.interpressnews.ge"}:
        return None
    match = ARTICLE_PATH.fullmatch(parsed.path)
    return int(match.group(1)) if match else None


def _urlset(raw: bytes, origin: str) -> list[tuple[str, date | None]]:
    """Reject HTML/error pages and malformed publisher sitemap entries."""
    root = ET.fromstring(raw)
    if root.tag.rsplit("}", 1)[-1] != "urlset":
        raise ValueError(f"publisher sitemap is not a urlset: {origin}")
    rows = []
    for entry in root:
        if entry.tag.rsplit("}", 1)[-1] != "url":
            continue
        loc = entry.find("{*}loc")
        if loc is None or not (loc.text or "").strip():
            raise ValueError(f"publisher sitemap URL missing loc: {origin}")
        url = normalize_url(loc.text.strip(), Config.source_name)
        if not url or _article_id(url) is None:
            raise ValueError(f"publisher sitemap has non-article URL: {loc.text!r}")
        lastmod = entry.find("{*}lastmod")
        try:
            modified = date.fromisoformat(lastmod.text.strip()[:10]) if lastmod is not None and lastmod.text else None
        except ValueError as exc:
            raise ValueError(f"publisher sitemap has malformed lastmod for {url}") from exc
        rows.append((url, modified))
    if not rows:
        raise ValueError(f"publisher sitemap has no article URLs: {origin}")
    return rows


def _advertised_sitemaps(robots_text: str) -> list[str]:
    found = {}
    for line in robots_text.splitlines():
        match = re.match(r"\s*Sitemap:\s*(\S+)", line, re.I)
        if not match:
            continue
        url = match.group(1).strip()
        numbered = ARTICLE_SITEMAP.fullmatch(url)
        if numbered:
            found[int(numbered.group(1))] = url
    numbers = sorted(found)
    if not numbers or numbers != list(range(1, numbers[-1] + 1)) or len(numbers) > MAX_SITEMAPS:
        raise ValueError(f"publisher robots has noncontiguous or excessive article sitemaps: {numbers}")
    if not any(line.strip().casefold() == f"sitemap: {DAILY}".casefold() for line in robots_text.splitlines()):
        raise ValueError("publisher robots no longer advertises the daily article sitemap")
    return [found[number] for number in numbers]


def _latest_url(raw: object) -> str | None:
    value = clean(raw)
    if not value:
        return None
    path = urlparse(value).path
    if path.startswith("/article/"):
        value = BASE + "/ka" + path
    elif path.startswith("/ka/article/"):
        value = BASE + path
    return normalize_url(value, Config.source_name)


def _latest_row(item: object) -> dict:
    if not isinstance(item, dict) or not isinstance(item.get("id"), int):
        raise ValueError("latest-news API returned an article without an integer ID")
    url = _latest_url(item.get("url"))
    if not url or _article_id(url) != item["id"]:
        raise ValueError(f"latest-news API URL and article ID disagree: {item.get('id')!r}")
    try:
        published = datetime.fromisoformat(str(item.get("pub_dt")))
        displayed = datetime.strptime(str(item.get("publish_up")), "%d.%m.%Y / %H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"latest-news API publication fields are malformed for {url}") from exc
    if published != displayed:
        raise ValueError(f"latest-news API publication fields disagree for {url}")
    title = clean(item.get("title"))
    if not title:
        raise ValueError(f"latest-news API title is missing for {url}")
    return {"id": item["id"], "url": url, "published": published, "title": title}


def _latestnews_inventory(start: date, end: date, evidence: dict) -> tuple[list[dict], dict[str, dict]]:
    """Traverse the publisher's original-publication feed past the lower bound.

    The browser application itself uses this POST endpoint for its latest-news
    pagination. Each result contains both an ISO ``pub_dt`` and the displayed
    ``publish_up`` value. Requiring those fields to agree avoids using sitemap
    modification times as publication dates.
    """
    shell = fetch(LATEST_SHELL)
    token_match = CSRF_TOKEN.search(shell.text)
    if not token_match:
        raise ValueError("latest-news shell omitted its CSRF token")
    token = token_match.group(1)
    evidence.update({
        "latestnews_shell": LATEST_SHELL,
        "latestnews_api": LATEST_API,
        "latestnews_shell_sha256": hashlib.sha256(shell.content).hexdigest(),
        "latestnews_pages": [],
        "latestnews_duplicate_rows": [],
        "latestnews_lower_boundary_reached": False,
    })
    by_id: dict[int, dict] = {}
    by_url: dict[str, dict] = {}
    older_streak = 0
    for page in range(1, MAX_LATEST_PAGES + 1):
        errors = []
        response = payload = None
        for attempt in range(3):
            try:
                response = SESSION.post(
                    LATEST_API,
                    data={"csrfmiddlewaretoken": token, "page": page},
                    headers={"lang": "ka", "Referer": shell.url},
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                    raise ValueError("latest-news API response is not a result object")
                break
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                response = payload = None
                if attempt < 2:
                    time.sleep(1 + attempt)
        if response is None or payload is None:
            raise RuntimeError(f"latest-news API page {page} failed: {'; '.join(errors)}")
        if payload.get("pagination") != {"page": page}:
            raise ValueError(f"latest-news API pagination marker changed on page {page}")
        rows = [_latest_row(item) for item in payload["results"]]
        if len(rows) != LATEST_PAGE_SIZE:
            raise ValueError(f"latest-news API page {page} returned {len(rows)} rows before the lower boundary")
        if any(rows[index]["published"] < rows[index + 1]["published"] for index in range(len(rows) - 1)):
            raise ValueError(f"latest-news API page {page} is not in descending publication order")
        for row in rows:
            identifier = row["id"]
            if identifier in by_id:
                previous = by_id[identifier]
                if any(previous[key] != row[key] for key in ("url", "published", "title")):
                    raise ValueError(f"latest-news API conflicts for article ID {identifier}")
                evidence["latestnews_duplicate_rows"].append({
                    "id": identifier, "url": row["url"], "page": page,
                    "date_publish": row["published"].isoformat(),
                })
                continue
            if row["url"] in by_url:
                raise ValueError(f"latest-news API maps multiple IDs to one URL: {row['url']}")
            by_id[identifier] = row
            by_url[row["url"]] = row
        page_dates = [row["published"].date() for row in rows]
        evidence["latestnews_pages"].append({
            "page": page,
            "sha256": hashlib.sha256(response.content).hexdigest(),
            "bytes": len(response.content),
            "rows": len(rows),
            "first_id": rows[0]["id"],
            "last_id": rows[-1]["id"],
            "first_date_publish": rows[0]["published"].isoformat(),
            "last_date_publish": rows[-1]["published"].isoformat(),
        })
        older_streak = older_streak + 1 if max(page_dates) < start else 0
        if older_streak >= OLDER_BOUNDARY_PAGES:
            evidence["latestnews_lower_boundary_reached"] = True
            break
    if not evidence["latestnews_lower_boundary_reached"]:
        raise ValueError(f"latest-news API exceeded {MAX_LATEST_PAGES} pages before its lower boundary")
    selected = [row for row in by_id.values() if start <= row["published"].date() <= end]
    selected.sort(key=lambda row: (row["published"], row["id"]))
    if len(selected) > MAX_WINDOW_URLS:
        raise ValueError(f"publisher window has {len(selected)} URLs; split the observation window")
    evidence["latestnews_inventory_count"] = len(by_id)
    evidence["latestnews_selected_count"] = len(selected)
    evidence["latestnews_selected_rows"] = [
        {"id": row["id"], "url": row["url"], "date_publish": row["published"].isoformat()}
        for row in selected
    ]
    return selected, by_url


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError(f"invalid or excessive Interpressnews window (max {MAX_WINDOW_DAYS + 1} days)")
    evidence.update({
        "method": "latestnews_original_publication_feed_reconciled_with_all_advertised_article_sitemaps",
        "robots": ROBOTS,
        "daily_sitemap": DAILY,
        "article_sitemaps": [],
        "duplicate_sitemap_entries": [],
        "coverage_complete": False,
        "sitemap_lastmod_is_not_date_publish": True,
    })
    robots_response = fetch(ROBOTS)
    robots = robots_response.text
    evidence["robots_sha256"] = hashlib.sha256(robots_response.content).hexdigest()
    advertised = _advertised_sitemaps(robots)
    inventory: dict[int, tuple[str, date, str]] = {}
    for sitemap_url in advertised:
        response = fetch(sitemap_url)
        rows = _urlset(response.content, sitemap_url)
        if any(modified is None for _, modified in rows):
            raise ValueError(f"publisher article sitemap omitted lastmod: {sitemap_url}")
        ids = [_article_id(url) for url, _ in rows]
        if len(set(ids)) != len(ids) or any(ids[i] <= ids[i + 1] for i in range(len(ids) - 1)):
            raise ValueError(f"publisher article sitemap ID order or uniqueness changed: {sitemap_url}")
        evidence["article_sitemaps"].append({
            "url": sitemap_url,
            "sha256": hashlib.sha256(response.content).hexdigest(),
            "bytes": len(response.content),
            "entries": len(rows),
            "newest_id": ids[0],
            "oldest_id": ids[-1],
            "first_lastmod": rows[0][1].isoformat(),
            "last_lastmod": rows[-1][1].isoformat(),
            "window_lastmod_candidates": sum(start <= modified <= end for _, modified in rows),
        })
        for url, modified in rows:
            identifier = _article_id(url)
            if identifier in inventory:
                previous_url, previous_modified, previous_sitemap = inventory[identifier]
                if (previous_url, previous_modified) != (url, modified):
                    raise ValueError(f"publisher article ID conflicts across sitemaps: {identifier}")
                # During a shard rollover the publisher repeats the exact
                # boundary row in both adjacent sitemaps. Preserve that fact
                # in evidence, but deduplicate it from the article inventory.
                evidence["duplicate_sitemap_entries"].append({
                    "id": identifier,
                    "url": url,
                    "lastmod": modified.isoformat(),
                    "first_sitemap": previous_sitemap,
                    "duplicate_sitemap": sitemap_url,
                })
                continue
            inventory[identifier] = (url, modified, sitemap_url)
    daily_response = fetch(DAILY)
    daily_rows = _urlset(daily_response.content, DAILY)
    evidence["daily_sitemap_sha256"] = hashlib.sha256(daily_response.content).hexdigest()
    daily_missing = [url for url, _ in daily_rows if _article_id(url) not in inventory]
    evidence["daily_urls"] = [url for url, _ in daily_rows]
    evidence["daily_missing_from_static_sitemaps"] = daily_missing
    evidence["daily_only_rows"] = []
    # The daily sitemap can lead the static shard by one or more fresh rows
    # during a publisher rollover. Fetch those pages and use their scoped
    # original publication timestamp so they cannot silently disappear from a
    # requested window while the static sitemap catches up.
    for url in daily_missing:
        response = fetch(url)
        article = _parse_page(url, response.content, response.status_code)
        published = article.get("date_publish")
        if not isinstance(published, datetime):
            raise ValueError(f"daily-only article lacks a publication timestamp: {url}")
        identifier = _article_id(url)
        evidence["daily_only_rows"].append({
            "id": identifier,
            "url": url,
            "date_publish": published.isoformat(),
            "page_sha256": hashlib.sha256(response.content).hexdigest(),
        })
        inventory[identifier] = (url, published.date(), DAILY)

    selected, api_inventory = _latestnews_inventory(start, end, evidence)
    sitemap_by_url = {url: (identifier, modified, origin)
                      for identifier, (url, modified, origin) in inventory.items()}
    missing_from_sitemaps = [row for row in selected if row["url"] not in sitemap_by_url]
    publication_after_lastmod = []
    for row in selected:
        sitemap_row = sitemap_by_url.get(row["url"])
        if sitemap_row and sitemap_row[1] < row["published"].date():
            publication_after_lastmod.append({
                "id": row["id"], "url": row["url"],
                "date_publish": row["published"].isoformat(),
                "sitemap_lastmod": sitemap_row[1].isoformat(),
            })
    recent_sitemap = [
        (identifier, url, modified)
        for identifier, (url, modified, _) in inventory.items()
        if modified >= start
    ]
    missing_from_api = [(identifier, url, modified) for identifier, url, modified in recent_sitemap
                        if url not in api_inventory]
    evidence["recent_sitemap_rows_missing_from_latestnews"] = []
    unexpected_in_window = []
    for identifier, url, modified in missing_from_api:
        response = fetch(url)
        article = _parse_page(url, response.content, response.status_code)
        published = article.get("date_publish")
        if not isinstance(published, datetime):
            raise ValueError(f"recent sitemap article missing from latest-news has no publication date: {url}")
        row = {
            "id": identifier, "url": url, "sitemap_lastmod": modified.isoformat(),
            "date_publish": published.isoformat(),
            "page_sha256": hashlib.sha256(response.content).hexdigest(),
        }
        evidence["recent_sitemap_rows_missing_from_latestnews"].append(row)
        if start <= published.date() <= end:
            unexpected_in_window.append(row)
            selected.append({"id": identifier, "url": url, "published": published, "title": article.get("title")})
    selected.sort(key=lambda row: (row["published"], row["id"]))
    evidence["article_inventory_count"] = len(inventory)
    evidence["recent_sitemap_candidates"] = len(recent_sitemap)
    evidence["latestnews_urls_missing_from_sitemaps"] = [
        {"id": row["id"], "url": row["url"], "date_publish": row["published"].isoformat()}
        for row in missing_from_sitemaps
    ]
    evidence["publication_after_sitemap_lastmod"] = publication_after_lastmod
    evidence["unexpected_in_window_sitemap_rows"] = unexpected_in_window
    evidence["coverage_complete"] = bool(evidence["latestnews_lower_boundary_reached"]) and not any((
        missing_from_sitemaps, publication_after_lastmod, unexpected_in_window,
    ))
    evidence["coverage_blocker"] = None if evidence["coverage_complete"] else "latestnews_and_sitemap_reconciliation_failed"
    return [row["url"] for row in selected]


def _matching_article(soup: BeautifulSoup, article_id: int):
    matches = []
    for node in soup.select('div[itemtype="http://schema.org/Article"], div[itemtype="https://schema.org/Article"]'):
        link = node.select_one('a[itemprop="url"][href]')
        if link and _article_id(urljoin(BASE, link.get("href"))) == article_id:
            matches.append(node)
    if len(matches) != 1:
        raise ValueError(f"article page has {len(matches)} matching server-rendered article blocks for ID {article_id}")
    return matches[0]


def _body_text(node) -> str | None:
    content = node.select_one('div[itemprop="description"]')
    if content is None:
        return None
    # Article-specific content is bounded to the matched item's description.
    # Do not include recommendations or category cards in the page body.
    for bad in content.select("script, style, iframe, form, nav, aside, footer"):
        bad.decompose()
    parts = [clean(item.get_text(" ", strip=True)) for item in content.select("p, li, blockquote")]
    parts = [part for part in parts if part]
    return "\n".join(parts) if parts else clean(content.get_text(" ", strip=True))


def _parse_page(url: str, content: bytes, status: int) -> dict:
    article_id = _article_id(url)
    if article_id is None:
        raise ValueError(f"not a canonical Interpressnews article URL: {url}")
    soup = BeautifulSoup(content, "html.parser")
    article = _matching_article(soup, article_id)
    title_node = article.select_one('h2[itemprop="name"]')
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    time_node = article.select_one("time[datetime]")
    date_raw = clean(time_node.get("datetime")) if time_node else None
    try:
        published = datetime.fromisoformat(date_raw) if date_raw else None
    except ValueError:
        published = None
    maintext = _body_text(article)
    og_title = soup.select_one('meta[property="og:title"][content]')
    og_description = soup.select_one('meta[property="og:description"][content]')
    og_url = soup.select_one('meta[property="og:url"][content]')
    og_title_text = clean(og_title.get("content")) if og_title else None
    og_description_text = clean(og_description.get("content")) if og_description else None
    errors = []
    if not title or not published or not maintext:
        errors.append("missing_required_article_field")
    if maintext and len(maintext) < 80:
        errors.append("suspiciously_short_maintext")
    if og_title_text and title and SequenceMatcher(None, title.casefold(), og_title_text.casefold()).ratio() < 0.8:
        errors.append("visible_title_disagrees_with_og_title")
    if og_url and _article_id(urljoin(BASE, og_url.get("content"))) != article_id:
        errors.append("og_url_article_id_disagrees")
    if og_description_text and maintext and len(og_description_text) >= 40:
        body_words = set(re.findall(r"\w+", maintext.casefold()))
        desc_words = set(re.findall(r"\w+", og_description_text.casefold()))
        if len(body_words & desc_words) / max(1, len(desc_words)) < 0.55:
            errors.append("og_description_disagrees_with_maintext")
    titles = [{"kind": "visible_h2", "source": "matching Article h2[itemprop=name]", "raw": title}] if title else []
    if og_title_text:
        titles.append({"kind": "og_title", "source": "meta[property=og:title]", "raw": og_title_text})
    bodies = [{"kind": "matching Article [itemprop=description]", "text": maintext[:20000]}] if maintext else []
    if og_description_text:
        bodies.append({"kind": "og_description", "text": og_description_text[:20000]})
    return {
        "url": url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "maintext": maintext,
        "date_publish": published,
        "parser": "matched_server_rendered_article_item",
        "parser_error": ";".join(errors) or None,
        "field_evidence": {
            "titles": titles,
            "dates": [{"kind": "visible_time", "source": "matching Article time[datetime]", "raw": date_raw}] if date_raw else [],
            "bodies": bodies,
            "http_status": status,
            "article_id": article_id,
            "article_block_count": 1,
        },
    }


def scrape_url(url: str) -> dict:
    response = fetch(url)
    return _parse_page(url, response.content, response.status_code)
