"""Network-only helpers for Mali observation adapters.

The helpers in this module have no database imports, credentials, caches on
disk, or production write paths. Sitemap ``lastmod`` values are only discovery
signals. Every returned publication date is parsed again from the article page.
"""
from __future__ import annotations

import json
import atexit
import re
import shutil
import time
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import dateparser
import requests
from bs4 import BeautifulSoup


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MLP-MLEED-observation/1.0)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_BROWSER = None
_BROWSER_ATEXIT_REGISTERED = False
_BROWSER_ONLY_HOSTS: set[str] = set()
BROWSER_FALLBACK_DOMAINS = {"maliweb.net", "www.maliweb.net"}
MEDIA = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)(?:\?|$)", re.I)
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|just a moment|verify you are human|web application firewall",
    re.I,
)


def clean(value: object) -> Optional[str]:
    if value is None:
        return None
    result = re.sub(r"\s+", " ", str(value)).strip()
    return result or None


def _browser_fetch(url: str, timeout: int) -> requests.Response:
    """Fetch a challenge-protected page with one reusable headless browser."""
    global _BROWSER, _BROWSER_ATEXIT_REGISTERED

    def start_browser():
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.page_load_strategy = "eager"
        options.add_experimental_option(
            "prefs",
            {
                "profile.managed_default_content_settings.images": 2,
                "profile.default_content_setting_values.notifications": 2,
            },
        )
        for argument in (
            "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
            "--window-size=1400,1000",
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        ):
            options.add_argument(argument)
        driver_path = shutil.which("chromedriver")
        driver = webdriver.Chrome(
            service=Service(driver_path) if driver_path else Service(),
            options=options,
        )
        driver.set_page_load_timeout(min(timeout, 20))
        return driver

    if not _BROWSER_ATEXIT_REGISTERED:
        atexit.register(lambda: _BROWSER and _BROWSER.quit())
        _BROWSER_ATEXIT_REGISTERED = True
    last_error = None
    for attempt in range(3):
        if _BROWSER is None:
            _BROWSER = start_browser()
        try:
            try:
                _BROWSER.get(url)
            except Exception as navigation_exc:
                # Advertising resources can keep a page load open after the
                # publisher's DOM is complete. Stop the tail and validate the
                # actual page below before deciding to restart the browser.
                try:
                    _BROWSER.execute_script("window.stop();")
                except Exception:
                    raise navigation_exc
            deadline = time.time() + min(timeout, 15)
            while time.time() < deadline:
                title = clean(_BROWSER.title) or ""
                html = _BROWSER.page_source or ""
                if html and not CHALLENGE.search(title):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("browser challenge did not clear")
            time.sleep(0.25)
            break
        except Exception as exc:
            last_error = exc
            try:
                _BROWSER.quit()
            except Exception:
                pass
            _BROWSER = None
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
    else:
        raise RuntimeError(f"browser fetch failed for {url}: {type(last_error).__name__}: {last_error}")
    response = requests.Response()
    response.status_code = 200
    response.url = _BROWSER.current_url
    response._content = html.encode("utf-8")
    response.encoding = "utf-8"
    response.scrape_transport = "selenium_chrome"
    return response


def fetch(url: str, timeout: int = 45, allow_browser: bool = True) -> requests.Response:
    """Fetch one public page with bounded retries and challenge detection."""
    host = (urlparse(url).hostname or "").lower()
    if allow_browser and host in _BROWSER_ONLY_HOSTS:
        return _browser_fetch(url, timeout)
    errors = []
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=timeout)
            title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text[:12000], re.I | re.S)
            title = clean(title_match.group(1)) if title_match else None
            if response.status_code != 200:
                raise requests.HTTPError(f"status={response.status_code}")
            if not response.content:
                raise ValueError("empty response")
            if title and CHALLENGE.search(title):
                raise ValueError(f"challenge page: {title}")
            response.scrape_transport = "requests"
            return response
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt < 2:
                time.sleep(1 + attempt)
    if allow_browser and host in BROWSER_FALLBACK_DOMAINS:
        response = _browser_fetch(url, timeout)
        _BROWSER_ONLY_HOSTS.add(host)
        return response
    raise RuntimeError(f"fetch failed for {url}: {'; '.join(errors)}")


def normalize_url(raw: str, domain: str, deny: Iterable[str] = ()) -> Optional[str]:
    try:
        parsed = urlparse(urljoin(f"https://{domain}/", raw))
        host = (parsed.hostname or "").lower()
        if host not in {domain, f"www.{domain}"}:
            return None
        if not parsed.path or parsed.path == "/" or MEDIA.search(parsed.path):
            return None
        path = parsed.path.rstrip("/")
        # WordPress permalinks conventionally use a terminal slash, while the
        # Malijet and Maliweb routers use literal ``.html`` paths. Appending a
        # slash to the latter can yield a soft-404 page with otherwise valid
        # looking metadata.
        if not re.search(r"\.(?:html?|php)$", path, re.I):
            path += "/"
        normalized = urlunparse(("https", parsed.netloc.lower(), path, "", "", ""))
        if any(part.casefold() in normalized.casefold() for part in deny):
            return None
        return normalized
    except Exception:
        return None


def parse_datetime(
    raw: object,
    *,
    relative_base: Optional[datetime] = None,
    day_first: bool = True,
) -> Optional[datetime]:
    value = clean(raw)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    settings = {
        "DATE_ORDER": "YMD" if re.match(r"^\s*20\d{2}[-/.]", value) else "DMY" if day_first else "YMD",
        "PREFER_LOCALE_DATE_ORDER": False,
        "RETURN_AS_TIMEZONE_AWARE": False,
    }
    if relative_base:
        settings["RELATIVE_BASE"] = relative_base
    parsed = dateparser.parse(value, languages=["fr"], settings=settings)
    return parsed.replace(tzinfo=None) if parsed and parsed.tzinfo else parsed


def xml_document(url: str) -> BeautifulSoup:
    soup = BeautifulSoup(fetch(url).content, "xml")
    root = soup.find()
    if root is None or root.name not in {"sitemapindex", "urlset"}:
        raise ValueError(f"not a sitemap XML document: {url}; root={getattr(root, 'name', None)}")
    return soup


def sitemap_entries(soup: BeautifulSoup) -> list[tuple[str, Optional[date]]]:
    nodes = soup.find_all("sitemap") if soup.find("sitemapindex") else soup.find_all("url")
    entries = []
    for node in nodes:
        loc = node.find("loc", recursive=False)
        if loc is None:
            continue
        lastmod = node.find("lastmod", recursive=False)
        modified = parse_datetime(lastmod.get_text(strip=True)).date() if lastmod else None
        entries.append((loc.get_text(strip=True), modified))
    return entries


def wordpress_sitemap_window(
    *,
    index_url: str,
    domain: str,
    start: date,
    end: date,
    evidence: dict,
    deny: Iterable[str] = (),
) -> list[str]:
    """Read the live Yoast index and every child that could cross the window.

    The two most recent numbered post sitemaps are always checked to cover a
    rollover whose index timestamp has not propagated. A two-day pad admits
    timezone and modification edge cases; article-page validation removes them.
    """
    index_entries = sitemap_entries(xml_document(index_url))
    post_entries = [(url, modified) for url, modified in index_entries if re.search(r"/post-sitemap\d*\.xml$", url)]
    if not post_entries:
        raise RuntimeError(f"live sitemap index has no post sitemaps: {index_url}")

    def sequence(item: tuple[str, Optional[date]]) -> int:
        match = re.search(r"post-sitemap(\d*)\.xml$", item[0])
        return int(match.group(1)) if match and match.group(1) else 1

    ordered = sorted(post_entries, key=sequence)
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    selected = {
        url
        for url, modified in ordered
        if modified is None or modified >= lower
    }
    selected.update(url for url, _ in ordered[-2:])
    evidence.update(
        {
            "method": "live_wordpress_sitemap_index",
            "sitemap_index": index_url,
            "post_sitemaps_in_index": len(post_entries),
            "selected_sitemaps": [],
            "candidate_lastmod_window": {"start": lower.isoformat(), "end": upper.isoformat()},
            "lastmod_policy": "discovery_only_article_page_date_is_authoritative",
        }
    )
    candidates: list[tuple[str, Optional[date]]] = []
    for url, _ in ordered:
        if url not in selected:
            continue
        rows = sitemap_entries(xml_document(url))
        dated = [modified for _, modified in rows if modified]
        evidence["selected_sitemaps"].append(
            {
                "url": url,
                "entries": len(rows),
                "oldest_lastmod": min(dated).isoformat() if dated else None,
                "newest_lastmod": max(dated).isoformat() if dated else None,
            }
        )
        candidates.extend((raw, modified) for raw, modified in rows if modified is None or start <= modified <= upper)
    candidates.sort(key=lambda item: item[1] or date.min, reverse=True)
    urls = []
    seen = set()
    for raw, _ in candidates:
        normalized = normalize_url(raw, domain, deny)
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    evidence["candidate_entries_before_url_filter"] = len(candidates)
    evidence["discovered_urls_after_filter"] = len(urls)
    return urls


def jsonld_objects(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(tag.string or tag.get_text() or "")
        except (TypeError, ValueError):
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, dict):
                yield value
                stack.extend(item for item in value.values() if isinstance(item, (dict, list)))


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
            "script, style, iframe, form, nav, aside, footer, .ad, .ads, .advertisement, "
            ".related, .share, .sharedaddy, .social, .tags, .tag, #jp-relatedposts, #wpdevar_comment_2"
        ):
            bad.decompose()
        parts = []
        for item in node.select("p, h2"):
            value = clean(item.get_text(" ", strip=True))
            if not value or len(value) < 20:
                continue
            # Some publisher HTML contains invalid nested ``p`` tags. In that
            # case BeautifulSoup exposes the first paragraph as the full
            # article and later paragraphs as repeated suffixes. Keep the
            # enclosing signal once instead of multiplying the article text.
            if any(value in previous for previous in parts):
                continue
            parts = [previous for previous in parts if previous not in value]
            parts.append(value)
        body = "\n\n".join(parts) if parts else clean(node.get_text(" ", strip=True))
        if body:
            return body, selector
    return None, None


def published_date(soup: BeautifulSoup, visible_selectors: Iterable[str] = ()) -> tuple[Optional[datetime], Optional[str], Optional[str]]:
    for selector in visible_selectors:
        node = soup.select_one(selector)
        if node:
            raw = clean(node.get("datetime") or node.get("content") or node.get_text(" ", strip=True))
            parsed = parse_datetime(raw)
            if parsed:
                return parsed, selector, raw
    for selector in (
        'meta[property="article:published_time"]',
        'meta[itemprop="datePublished"]',
        'meta[name="datePublished"]',
        'time[itemprop="datePublished"]',
    ):
        node = soup.select_one(selector)
        if node:
            raw = clean(node.get("content") or node.get("datetime") or node.get_text(" ", strip=True))
            parsed = parse_datetime(raw)
            if parsed:
                return parsed, selector, raw
    for item in jsonld_objects(soup):
        raw = clean(item.get("datePublished"))
        parsed = parse_datetime(raw)
        if parsed:
            return parsed, "jsonld.datePublished", raw
    return None, None, None


def independent_page_evidence(soup: BeautifulSoup, status: int) -> dict:
    """Collect alternate visible, metadata, and structured page signals."""
    titles = []
    dates = []
    bodies = []
    for selector, kind in (
        ('meta[property="og:title"]', "og_title"),
        ('meta[name="twitter:title"]', "twitter_title"),
    ):
        node = soup.select_one(selector)
        value = clean(node.get("content")) if node else None
        if value and all(item["raw"] != value for item in titles):
            titles.append({"kind": kind, "source": selector, "raw": value})
    for selector, kind in (
        ('meta[property="article:published_time"]', "published"),
        ('meta[itemprop="datePublished"]', "published"),
        ('time[datetime]', "visible_time"),
    ):
        for node in soup.select(selector)[:5]:
            value = clean(node.get("content") or node.get("datetime") or node.get_text(" ", strip=True))
            if value and all(item["raw"] != value for item in dates):
                dates.append({"kind": kind, "source": selector, "raw": value})
    for item in jsonld_objects(soup):
        headline = clean(item.get("headline"))
        if headline and all(row["raw"] != headline for row in titles):
            titles.append({"kind": "jsonld_headline", "source": "jsonld.headline", "raw": headline})
        published = clean(item.get("datePublished"))
        if published and all(row["raw"] != published for row in dates):
            dates.append({"kind": "published", "source": "jsonld.datePublished", "raw": published})
        body = clean(item.get("articleBody"))
        if body:
            bodies.append({"kind": "jsonld.articleBody", "text": body[:20000]})
    if not titles:
        title, selector = first_text(soup, ("article h1", "main h1", "h1"))
        if title:
            titles.append({"kind": "visible_h1", "source": selector, "raw": title})
    return {"titles": titles, "dates": dates, "bodies": bodies, "http_status": status}


def extract_article(
    *,
    url: str,
    domain: str,
    title_selectors: Iterable[str],
    body_selectors: Iterable[str],
    visible_date_selectors: Iterable[str] = (),
    parser: str,
) -> tuple[dict, BeautifulSoup]:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, _ = first_text(soup, title_selectors)
    body, _ = article_body(soup, body_selectors)
    published, _, _ = published_date(soup, visible_date_selectors)
    missing = [name for name, value in (("title", title), ("maintext", body), ("date_publish", published)) if not value]
    problem = f"missing:{','.join(missing)}" if missing else None
    sample = f"{title or ''} {body or ''}"[:500]
    if CHALLENGE.search(sample):
        problem = "challenge_or_error_page"
    article = {
        "url": url,
        "source_domain": domain,
        "country_code": "MLI",
        "title": title,
        "maintext": body,
        "date_publish": published,
        "parser": parser,
        "parser_error": problem,
        "field_evidence": independent_page_evidence(soup, response.status_code),
    }
    article["field_evidence"]["transport"] = getattr(response, "scrape_transport", "requests")
    # If JSON-LD has an article body, it is independent evidence for the DOM body.
    # Otherwise use a separate broad page container so the observer still checks
    # that the selected body appears on the visible page.
    if not article["field_evidence"]["bodies"] and body:
        article["field_evidence"]["bodies"].append({"kind": "visible_body_container", "text": body[:20000]})
    return article, soup
