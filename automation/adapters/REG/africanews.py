"""Fail-closed Africanews browser adapter candidate.

This module preserves the only locally recorded browser discovery contract for
Africanews while removing every database and production-write path.  It is
deliberately disabled until current, hash-pinned robots, rendered-listing, and
rendered-article canaries have been reviewed.  The public entry points fail
before any network or browser action while that gate is closed.

The locally recorded contract is limited to the English ``/news/`` and French
``/infos/`` listings, ``div.jsArticle`` cards, ``.teaser__title a`` links,
``time.article__date`` dates, and the ``a.js-load-more`` control.  The browser
implementation is bounded, uses one shared driver, checks robots under the
truthful ``ChatGPT-User`` identity, and requires a chronologically cumulative
listing with rows on both sides of the requested interval.

There are no database imports, credentials, cache writes, output writers, or
production mutation paths in this module.
"""
from __future__ import annotations

import atexit
import hashlib
import html
import json
import re
import threading
import time
from collections import Counter
from datetime import date, datetime
from typing import Iterator, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup


class Config:
    source_name = "africanews.com"
    country_code = "REG"
    is_env = False
    access_block_circuit_threshold = 1
    browser_worker = True
    production_writes = False
    launch_enabled = False
    launch_ready = False


SOURCE_ID = "mlp:REG:africanews.com"
FIRST_PASS_START = date(2026, 8, 13)
FIRST_PASS_END = date(2026, 9, 30)

# This must remain false until the fixture set named in the handoff exists and
# its hashes are pinned here.  Keeping the gate in code prevents an unreviewed
# packet from becoming launchable merely because a command file is copied.
CURRENT_CANARY_APPROVED = False
CURRENT_CANARY_FIXTURE_SHA256: dict[str, str] = {}
REQUIRED_CURRENT_CANARY_FIXTURES = frozenset(
    {
        "robots_www.txt",
        "robots_fr.txt",
        "listing_en_initial.html",
        "listing_en_after_load_more.html",
        "listing_fr_initial.html",
        "listing_fr_after_load_more.html",
        "article_en_boundary.html",
        "article_en_middle.html",
        "article_en_target.html",
        "article_fr_boundary.html",
        "article_fr_middle.html",
        "article_fr_target.html",
        "browser_canary.json",
    }
)

USER_AGENT = "ChatGPT-User"
SECTION_SPECS = (
    {
        "locale": "en",
        "host": "www.africanews.com",
        "url": "https://www.africanews.com/news/",
    },
    {
        "locale": "fr",
        "host": "fr.africanews.com",
        "url": "https://fr.africanews.com/infos/",
    },
)
HOST_LOCALES = {
    "africanews.com": "en",
    "www.africanews.com": "en",
    "fr.africanews.com": "fr",
}
CANONICAL_HOSTS = {"en": "www.africanews.com", "fr": "fr.africanews.com"}

ARTICLE_SELECTOR = "div.jsArticle"
LINK_SELECTOR = ".teaser__title a"
DATE_SELECTOR = "time.article__date"
LOAD_MORE_SELECTOR = "div.view-more-bar a.js-load-more"
LOAD_MORE_TEXTS = frozenset({"view more"})

MAX_ROUNDS_PER_LOCALE = 80
MAX_CARDS_PER_LOCALE = 5_000
MAX_SELECTED_URLS = 5_000
MAX_RENDERED_BYTES = 24 * 1024 * 1024
MAX_ROBOTS_BYTES = 128 * 1024
PAGE_LOAD_TIMEOUT_SECONDS = 45
DOM_WAIT_SECONDS = 15
MIN_INTERACTION_INTERVAL_SECONDS = 1.25
CHALLENGE = re.compile(
    r"access denied|captcha|cloudflare|checking your browser|just a moment|"
    r"verify you are human|web application firewall",
    re.I,
)
MEDIA = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)(?:$|\?)", re.I)
BLOCKED_PREFIXES = (
    "/author/",
    "/category/",
    "/search/",
    "/tag/",
    "/wp-admin/",
    "/wp-content/",
)


class CanaryRequiredError(RuntimeError):
    """Current publisher evidence has not yet been reviewed and pinned."""


class AccessBlockedError(RuntimeError):
    """Publisher policy, authentication, challenge, or rate-limit blocker."""


class _RobotsResponse:
    def __init__(self, url: str, status: int, content: bytes):
        self.url = url
        self.status_code = status
        self.content = content


_DRIVER = None
_DRIVER_LOCK = threading.RLock()
_REQUEST_LOCK = threading.Lock()
_ROBOTS_LOCK = threading.Lock()
_ROBOTS_CACHE: dict[str, tuple[_RobotsResponse, RobotFileParser]] = {}
_LAST_REQUEST_AT = 0.0
_LAST_BROWSER_ACTION_AT = 0.0
_DISCOVERY_ROWS: dict[str, dict] = {}


def _clean(value: object) -> Optional[str]:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", html.unescape(str(value)).replace("\xa0", " ")).strip()
    return cleaned or None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_current_canary() -> None:
    missing = sorted(REQUIRED_CURRENT_CANARY_FIXTURES - CURRENT_CANARY_FIXTURE_SHA256.keys())
    malformed = sorted(
        name
        for name, digest in CURRENT_CANARY_FIXTURE_SHA256.items()
        if name in REQUIRED_CURRENT_CANARY_FIXTURES
        and not re.fullmatch(r"[0-9a-f]{64}", digest or "")
    )
    if not CURRENT_CANARY_APPROVED or missing or malformed:
        details = []
        if not CURRENT_CANARY_APPROVED:
            details.append("approval flag is false")
        if missing:
            details.append("missing fixtures: %s" % ", ".join(missing))
        if malformed:
            details.append("invalid fixture hashes: %s" % ", ".join(malformed))
        raise CanaryRequiredError("Africanews current-canary gate is closed (%s)" % "; ".join(details))


def _validate_window(start: date, end: date) -> None:
    if start != FIRST_PASS_START or end != FIRST_PASS_END:
        raise ValueError(
            "Africanews candidate is pinned to %s through %s"
            % (FIRST_PASS_START.isoformat(), FIRST_PASS_END.isoformat())
        )


def _fetch_robots(host: str, *, timeout: int = 30) -> _RobotsResponse:
    """Fetch one allowlisted robots file with a small response cap."""
    global _LAST_REQUEST_AT
    if host not in {spec["host"] for spec in SECTION_SPECS}:
        raise ValueError("Africanews refused an unreviewed robots host: %s" % host)
    url = "https://%s/robots.txt" % host
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/plain,*/*;q=0.1"},
        method="GET",
    )
    try:
        with _REQUEST_LOCK:
            delay = MIN_INTERACTION_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_AT)
            if delay > 0:
                time.sleep(delay)
            try:
                stream = urlopen(request, timeout=timeout)
            finally:
                _LAST_REQUEST_AT = time.monotonic()
        with stream:
            status = int(getattr(stream, "status", 200))
            final_url = stream.geturl()
            final = urlparse(final_url)
            if final.scheme != "https" or final.hostname != host or final.path != "/robots.txt":
                raise AccessBlockedError("unexpected robots redirect for %s" % url)
            declared = stream.headers.get("Content-Length")
            if declared and int(declared) > MAX_ROBOTS_BYTES:
                raise RuntimeError("Africanews robots response exceeds byte cap")
            payload = stream.read(MAX_ROBOTS_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403, 407, 429, 451}:
            raise AccessBlockedError("Africanews robots access blocked (%s)" % exc.code) from exc
        raise RuntimeError("Africanews robots returned HTTP %s" % exc.code) from exc
    except URLError as exc:
        raise RuntimeError("Africanews robots request failed: %s" % exc.reason) from exc
    if status != 200 or not payload:
        raise RuntimeError("Africanews robots returned unexpected status %s" % status)
    if len(payload) > MAX_ROBOTS_BYTES:
        raise RuntimeError("Africanews robots response exceeds byte cap")
    return _RobotsResponse(final_url, status, payload)


def _robots_for(host: str) -> tuple[_RobotsResponse, RobotFileParser]:
    with _ROBOTS_LOCK:
        cached = _ROBOTS_CACHE.get(host)
    if cached is not None:
        return cached
    response = _fetch_robots(host)
    parser = RobotFileParser()
    parser.set_url("https://%s/robots.txt" % host)
    parser.parse(response.content.decode("utf-8", "replace").splitlines())
    result = (response, parser)
    with _ROBOTS_LOCK:
        _ROBOTS_CACHE.setdefault(host, result)
        return _ROBOTS_CACHE[host]


def _require_allowed(parser: RobotFileParser, url: str) -> None:
    if not parser.can_fetch(USER_AGENT, url):
        raise AccessBlockedError(
            "Africanews robots policy disallows %s for %s" % (url, USER_AGENT)
        )


def _locale_for_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None
    return HOST_LOCALES.get((parsed.hostname or "").lower())


def _normalize_article_url(raw: object, locale: str) -> Optional[str]:
    value = _clean(raw)
    if not value or locale not in CANONICAL_HOSTS:
        return None
    base = "https://%s/" % CANONICAL_HOSTS[locale]
    parsed = urlparse(urljoin(base, value))
    detected = HOST_LOCALES.get((parsed.hostname or "").lower())
    if parsed.scheme != "https" or detected != locale or parsed.port is not None:
        return None
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    lowered = path.casefold() + "/"
    if not path or path in {"/news", "/infos"} or MEDIA.search(path):
        return None
    if any(lowered.startswith(prefix) for prefix in BLOCKED_PREFIXES):
        return None
    return urlunparse(("https", CANONICAL_HOSTS[locale], path, "", "", ""))


def _parse_datetime(raw: object, locale: str) -> Optional[datetime]:
    value = _clean(raw)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            import dateparser

            parsed = dateparser.parse(
                value,
                languages=[locale],
                settings={"DATE_ORDER": "DMY", "PREFER_LOCALE_DATE_ORDER": True},
            )
        except Exception:
            parsed = None
    return parsed.replace(tzinfo=None) if parsed and parsed.tzinfo else parsed


def _parse_listing_html(payload: str | bytes, *, locale: str) -> list[dict]:
    """Parse one cumulative rendered listing snapshot using recorded selectors."""
    soup = BeautifulSoup(payload, "html.parser")
    cards = soup.select(ARTICLE_SELECTOR)
    if not cards:
        raise RuntimeError("Africanews rendered listing has no %s cards" % ARTICLE_SELECTOR)
    if len(cards) > MAX_CARDS_PER_LOCALE:
        raise RuntimeError("Africanews rendered listing exceeds the card safety ceiling")
    rows = []
    by_url: dict[str, date] = {}
    for position, card in enumerate(cards, start=1):
        link = card.select_one(LINK_SELECTOR)
        url = _normalize_article_url(link.get("href") if link else None, locale)
        if not url:
            raise RuntimeError("Africanews card %s lacks an allowlisted article URL" % position)
        time_node = card.select_one(DATE_SELECTOR)
        if not time_node:
            raise RuntimeError("Africanews card %s lacks %s" % (position, DATE_SELECTOR))
        raw_date = _clean(
            time_node.get("datetime")
            or time_node.get("content")
            or time_node.get_text(" ", strip=True)
        )
        parsed = _parse_datetime(raw_date, locale)
        if parsed is None:
            raise RuntimeError("Africanews card %s has an unparseable date" % position)
        published = parsed.date()
        if url in by_url:
            if by_url[url] != published:
                raise RuntimeError("Africanews duplicate card has conflicting dates: %s" % url)
            continue
        by_url[url] = published
        rows.append(
            {
                "url": url,
                "listing_date": published,
                "listing_date_raw": raw_date,
                "listing_position": position,
                "locale": locale,
            }
        )
    dates = [row["listing_date"] for row in rows]
    if any(current > previous for previous, current in zip(dates, dates[1:])):
        raise RuntimeError("Africanews listing cards are not in descending date order")
    return rows


def _close_driver() -> None:
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is not None:
            try:
                _DRIVER.quit()
            except Exception:
                pass
            _DRIVER = None


atexit.register(_close_driver)


def _new_driver():
    """Create one ordinary headless Chrome session without challenge bypasses."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("Africanews browser worker requires Selenium") from exc

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
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1440,2200",
        "--lang=en-US",
        "--user-agent=%s" % USER_AGENT,
    ):
        options.add_argument(argument)
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    return driver


def _driver_for_work():
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is None:
            _DRIVER = _new_driver()
        return _DRIVER


def _pace_browser_action() -> None:
    global _LAST_BROWSER_ACTION_AT
    delay = MIN_INTERACTION_INTERVAL_SECONDS - (time.monotonic() - _LAST_BROWSER_ACTION_AT)
    if delay > 0:
        time.sleep(delay)
    _LAST_BROWSER_ACTION_AT = time.monotonic()


def _rendered_payload(driver, *, expected_locale: str, minimum_bytes: int = 1_000) -> bytes:
    title = _clean(getattr(driver, "title", "")) or ""
    source = getattr(driver, "page_source", "") or ""
    payload = source.encode("utf-8")
    if len(payload) > MAX_RENDERED_BYTES:
        raise RuntimeError("Africanews rendered page exceeds byte cap")
    if len(payload) < minimum_bytes:
        raise RuntimeError("Africanews rendered page is unexpectedly small")
    if CHALLENGE.search("%s %s" % (title, source[:24_000])):
        raise AccessBlockedError("Africanews browser returned a challenge page")
    current_url = getattr(driver, "current_url", "") or ""
    if _locale_for_url(current_url) != expected_locale:
        raise AccessBlockedError("Africanews browser left the reviewed locale host")
    return payload


def _wait_for_listing(driver, *, locale: str) -> tuple[bytes, list[dict]]:
    deadline = time.monotonic() + DOM_WAIT_SECONDS
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            payload = _rendered_payload(driver, expected_locale=locale)
            return payload, _parse_listing_html(payload, locale=locale)
        except (RuntimeError, AccessBlockedError) as exc:
            last_error = exc
            if isinstance(exc, AccessBlockedError):
                raise
            time.sleep(0.25)
    raise RuntimeError("Africanews listing did not become usable: %s" % last_error)


def _click_load_more(driver) -> bool:
    try:
        candidates = driver.find_elements("css selector", LOAD_MORE_SELECTOR)
    except Exception as exc:
        raise RuntimeError("Africanews could not inspect the load-more control") from exc
    for button in candidates:
        text = _clean(getattr(button, "text", "")) or ""
        try:
            aria = _clean(button.get_attribute("aria-label")) or ""
        except Exception:
            aria = ""
        combined = "%s %s" % (text.casefold(), aria.casefold())
        if not any(expected in combined for expected in LOAD_MORE_TEXTS):
            continue
        try:
            if hasattr(button, "is_displayed") and not button.is_displayed():
                continue
            if hasattr(button, "is_enabled") and not button.is_enabled():
                continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
            _pace_browser_action()
            try:
                button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", button)
            return True
        except Exception as exc:
            raise RuntimeError("Africanews load-more click failed") from exc
    return False


def _wait_for_growth(driver, *, locale: str, previous_urls: list[str]) -> tuple[bytes, list[dict]]:
    deadline = time.monotonic() + DOM_WAIT_SECONDS
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            payload = _rendered_payload(driver, expected_locale=locale)
            rows = _parse_listing_html(payload, locale=locale)
            urls = [row["url"] for row in rows]
            if len(urls) > len(previous_urls):
                if urls[: len(previous_urls)] != previous_urls:
                    raise RuntimeError("Africanews cumulative listing changed earlier cards")
                return payload, rows
        except (RuntimeError, AccessBlockedError) as exc:
            last_error = exc
            if isinstance(exc, AccessBlockedError):
                raise
        time.sleep(0.25)
    raise RuntimeError("Africanews listing did not grow after load-more: %s" % last_error)


def _crawl_section(spec: dict, start: date, end: date) -> tuple[list[dict], dict]:
    response, robots = _robots_for(spec["host"])
    _require_allowed(robots, spec["url"])
    driver = _driver_for_work()
    try:
        _pace_browser_action()
        driver.get(spec["url"])
        payload, rows = _wait_for_listing(driver, locale=spec["locale"])
        rounds = []
        for round_number in range(1, MAX_ROUNDS_PER_LOCALE + 1):
            dates = [row["listing_date"] for row in rows]
            selected = [row for row in rows if start <= row["listing_date"] <= end]
            months = {row["listing_date"].strftime("%Y-%m") for row in selected}
            summary = {
                "round": round_number,
                "cards": len(rows),
                "selected": len(selected),
                "newest": dates[0].isoformat(),
                "oldest": dates[-1].isoformat(),
                "rendered_bytes": len(payload),
                "rendered_sha256": _sha256(payload),
            }
            rounds.append(summary)
            boundary_after = any(value > end for value in dates)
            boundary_before = any(value < start for value in dates)
            if boundary_after and boundary_before and months == {"2026-08", "2026-09"}:
                return selected, {
                    "locale": spec["locale"],
                    "listing_url": spec["url"],
                    "robots_url": response.url,
                    "robots_sha256": _sha256(response.content),
                    "rounds": rounds,
                    "cards_at_boundary": len(rows),
                    "selected": len(selected),
                    "selected_month_counts": dict(
                        sorted(Counter(row["listing_date"].strftime("%Y-%m") for row in selected).items())
                    ),
                    "boundary_after_target": max(dates).isoformat(),
                    "boundary_before_start": min(dates).isoformat(),
                    "chronology": "strictly_checked_nonincreasing",
                    "cumulative_prefix_checked": True,
                    "coverage_complete": True,
                }
            previous_urls = [row["url"] for row in rows]
            if not _click_load_more(driver):
                raise RuntimeError(
                    "Africanews load-more ended before both interval boundaries were closed"
                )
            payload, rows = _wait_for_growth(
                driver, locale=spec["locale"], previous_urls=previous_urls
            )
        raise RuntimeError("Africanews listing exceeded the round safety ceiling")
    except Exception:
        _close_driver()
        raise


def _discover_urls_reviewed(start: date, end: date, evidence: dict) -> list[str]:
    """Internal discovery implementation exercised only by reviewed fixtures/tests."""
    _validate_window(start, end)
    selected_rows = []
    locale_evidence = []
    for spec in SECTION_SPECS:
        rows, summary = _crawl_section(spec, start, end)
        selected_rows.extend(rows)
        locale_evidence.append(summary)
    urls = [row["url"] for row in selected_rows]
    if not urls or len(urls) > MAX_SELECTED_URLS:
        raise RuntimeError("Africanews selected URL count is outside the reviewed safety bounds")
    if len(urls) != len(set(urls)):
        raise RuntimeError("Africanews locale inventories overlap after canonicalization")
    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in selected_rows})
    evidence.update(
        {
            "method": "bounded_headless_browser_cumulative_load_more",
            "source_id": SOURCE_ID,
            "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
            "recorded_listing_selectors": {
                "card": ARTICLE_SELECTOR,
                "link": LINK_SELECTOR,
                "date": DATE_SELECTOR,
                "load_more": LOAD_MORE_SELECTOR,
            },
            "locales": locale_evidence,
            "selected": len(urls),
            "selected_locale_counts": dict(sorted(Counter(row["locale"] for row in selected_rows).items())),
            "selected_month_counts": dict(
                sorted(Counter(row["listing_date"].strftime("%Y-%m") for row in selected_rows).items())
            ),
            "selected_url_set_sha256": _sha256("".join("%s\n" % url for url in sorted(urls)).encode()),
            "listing_dates_used_as_publication_dates": False,
            "article_page_date_is_authoritative": True,
            "browser_transport": "one_shared_headless_chrome_worker",
            "production_writes": False,
            "coverage_complete": True,
        }
    )
    return urls


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    """Fail before network/browser use until current publisher canaries are pinned."""
    _validate_window(start, end)
    _require_current_canary()
    return _discover_urls_reviewed(start, end, evidence)


def _jsonld_objects(soup: BeautifulSoup) -> Iterator[dict]:
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(node.string or node.get_text() or "")
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


def _article_jsonld(soup: BeautifulSoup) -> dict:
    for item in _jsonld_objects(soup):
        kinds = item.get("@type")
        kinds = set(kinds if isinstance(kinds, list) else [kinds])
        if kinds & {"Article", "NewsArticle", "ReportageNewsArticle"}:
            return item
    return {}


def _body_from_soup(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str], int]:
    selectors = (
        "[itemprop='articleBody']",
        "div.article__content",
        "div.article__body",
        "article .article-content",
    )
    remove = (
        "script",
        "style",
        "iframe",
        "form",
        "nav",
        "aside",
        "footer",
        "figure",
        "figcaption",
        ".ad",
        ".ads",
        ".advertisement",
        ".related",
        ".share",
        ".social",
        ".newsletter",
        "[class*='related']",
        "[class*='recommend']",
        "[class*='newsletter']",
        "[class*='subscribe']",
    )
    for selector in selectors:
        original = soup.select_one(selector)
        if not original:
            continue
        node = BeautifulSoup(str(original), "html.parser")
        for bad_selector in remove:
            for bad in node.select(bad_selector):
                bad.decompose()
        parts = []
        for child in node.select("p, h2, h3"):
            value = _clean(child.get_text(" ", strip=True))
            if not value or len(value) < 20:
                continue
            if value not in parts:
                parts.append(value)
        if parts:
            return "\n\n".join(parts), selector, len(parts)
    return None, None, 0


def _parse_article_html(
    url: str, payload: bytes | str, *, response_url: Optional[str] = None
) -> dict:
    locale = _locale_for_url(url)
    requested = _normalize_article_url(url, locale or "") if locale else None
    soup = BeautifulSoup(payload, "html.parser")
    item = _article_jsonld(soup)
    errors = []

    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = _normalize_article_url(
        canonical_node.get("href") if canonical_node else None, locale or ""
    )
    if not requested or canonical != requested:
        errors.append("canonical_vs_requested_url_disagreement")
    if response_url:
        retrieved = _normalize_article_url(response_url, locale or "")
        if retrieved != requested:
            errors.append("response_vs_requested_url_disagreement")

    title = None
    title_selector = None
    for selector in ("h1.article__title", "article h1", "h1"):
        node = soup.select_one(selector)
        value = _clean(node.get_text(" ", strip=True)) if node else None
        if value:
            title, title_selector = value, selector
            break
    if not title:
        errors.append("missing_title")

    date_values = []
    for selector in (
        'meta[property="article:published_time"][content]',
        'meta[itemprop="datePublished"][content]',
        'time.article__date[datetime]',
    ):
        node = soup.select_one(selector)
        raw = _clean(node.get("content") or node.get("datetime")) if node else None
        parsed = _parse_datetime(raw, locale or "en")
        if parsed:
            date_values.append({"source": selector, "raw": raw, "parsed": parsed})
    jsonld_raw = _clean(item.get("datePublished"))
    jsonld_date = _parse_datetime(jsonld_raw, locale or "en")
    if jsonld_date:
        date_values.append(
            {"source": "jsonld.datePublished", "raw": jsonld_raw, "parsed": jsonld_date}
        )
    published = date_values[0]["parsed"] if date_values else None
    if published is None:
        errors.append("missing_publication_date")
    elif len({row["parsed"].date() for row in date_values}) != 1:
        errors.append("publication_date_disagreement")

    body, body_selector, paragraph_count = _body_from_soup(soup)
    if not body or len(body) < 200 or paragraph_count < 2:
        errors.append("missing_or_short_maintext")

    html_language = _clean((soup.html or {}).get("lang") if soup.html else None)
    language = (html_language or _clean(item.get("inLanguage")) or "").split("-", 1)[0].lower()
    if language != locale:
        errors.append("language_vs_locale_disagreement")

    title_values = []
    for kind, source, raw in (
        ("visible_h1", title_selector, title),
        ("jsonld_headline", "jsonld.headline", _clean(item.get("headline") or item.get("name"))),
    ):
        if raw and all(existing["raw"] != raw for existing in title_values):
            title_values.append({"kind": kind, "source": source, "raw": raw})
    payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
    return {
        "url": requested or url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": language or None,
        "parser": "pure_browser_africanews_candidate",
        "collection": None,
        "parser_error": ";".join(dict.fromkeys(errors)) or None,
        "field_sources": {
            "title": title_selector,
            "date_publish": date_values[0]["source"] if date_values else None,
            "maintext": body_selector,
            "language": "html[lang] or JSON-LD inLanguage",
        },
        "field_evidence": {
            "canonical": canonical,
            "response_url": response_url or url,
            "response_sha256": _sha256(payload_bytes),
            "response_bytes": len(payload_bytes),
            "transport": "headless_chrome_rendered_html",
            "locale": locale,
            "titles": title_values,
            "dates": [
                {"kind": "published", "source": row["source"], "raw": row["raw"]}
                for row in date_values
            ],
            "bodies": (
                [{"kind": "visible_body_container", "source": body_selector, "text": body[:20_000]}]
                if body
                else []
            ),
            "paragraph_count": paragraph_count,
            "html_language": html_language,
            "listing_date": (
                _DISCOVERY_ROWS.get(requested or "", {}).get("listing_date")
                if requested
                else None
            ),
            "listing_date_used_as_publication_date": False,
            "production_writes": False,
        },
    }


def _render_article(url: str) -> tuple[bytes, str]:
    locale = _locale_for_url(url)
    normalized = _normalize_article_url(url, locale or "") if locale else None
    if not normalized or locale not in CANONICAL_HOSTS:
        raise ValueError("Africanews refused a noncanonical article URL")
    host = CANONICAL_HOSTS[locale]
    _, robots = _robots_for(host)
    _require_allowed(robots, normalized)
    driver = _driver_for_work()
    try:
        _pace_browser_action()
        driver.get(normalized)
        deadline = time.monotonic() + DOM_WAIT_SECONDS
        last_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                payload = _rendered_payload(driver, expected_locale=locale)
                parsed = _parse_article_html(
                    normalized,
                    payload,
                    response_url=getattr(driver, "current_url", normalized),
                )
                if parsed["parser_error"] is None:
                    return payload, getattr(driver, "current_url", normalized)
                last_error = RuntimeError(parsed["parser_error"])
            except (RuntimeError, AccessBlockedError) as exc:
                last_error = exc
                if isinstance(exc, AccessBlockedError):
                    raise
            time.sleep(0.25)
        raise RuntimeError("Africanews article did not become usable: %s" % last_error)
    except Exception:
        _close_driver()
        raise


def _scrape_url_reviewed(url: str) -> dict:
    payload, response_url = _render_article(url)
    return _parse_article_html(url, payload, response_url=response_url)


def scrape_url(url: str) -> dict:
    """Fail before network/browser use until current publisher canaries are pinned."""
    _require_current_canary()
    return _scrape_url_reviewed(url)
