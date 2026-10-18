"""Reusable headless-browser fallback for Južne Vesti article pages.

Cloudflare can start challenging article HTML after a long requests-only run,
while the publisher RSS archive remains available.  This module opens only an
already-discovered article URL, reuses one browser session, and extracts the
same publisher fields as the requests parser.  It has no discovery, database,
or write path.
"""
from __future__ import annotations

import atexit
import threading
import time

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options

from adapters.SRB.common import article_body, clean, normalize_url, parse_datetime


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_DRIVER = None
_LOCK = threading.RLock()


def _close_driver() -> None:
    global _DRIVER
    with _LOCK:
        if _DRIVER is not None:
            try:
                _DRIVER.quit()
            except Exception:
                pass
            _DRIVER = None


atexit.register(_close_driver)


def _new_driver():
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
        "--disable-blink-features=AutomationControlled",
        "--window-size=1440,2200",
        "--lang=sr-RS",
        f"--user-agent={USER_AGENT}",
    ):
        options.add_argument(argument)
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(12)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def _challenge(title: str, html: str) -> bool:
    value = f"{title} {html[:1500]}".casefold()
    return any(token in value for token in (
        "just a moment", "checking your browser", "verify you are human",
        "challenge-platform",
    ))


def _page(url: str) -> tuple[str, str]:
    global _DRIVER
    with _LOCK:
        errors = []
        for attempt in range(2):
            if _DRIVER is None:
                _DRIVER = _new_driver()
            else:
                # Once Chrome has passed the first-party challenge, request
                # subsequent article HTML through same-origin fetch.  This
                # preserves its clearance cookie without paying a full page
                # navigation and advertising load for every article.
                script = """
                    const done = arguments[arguments.length - 1];
                    fetch(arguments[0], {credentials: 'include', cache: 'no-store'})
                      .then(async response => done({
                        status: response.status,
                        url: response.url,
                        text: await response.text()
                      }))
                      .catch(error => done({error: String(error)}));
                """
                try:
                    _DRIVER.set_script_timeout(8)
                    result = _DRIVER.execute_async_script(script, url)
                    html = result.get("text") if isinstance(result, dict) else ""
                    if (
                        isinstance(result, dict)
                        and not result.get("error")
                        and int(result.get("status") or 0) == 200
                        and len(html or "") > 10000
                        and not _challenge("", html)
                    ):
                        return html, result.get("url") or url
                    errors.append(f"same-origin fetch unusable: {result!r}"[:500])
                except Exception as exc:
                    errors.append(f"same-origin fetch: {type(exc).__name__}: {exc}")
            try:
                try:
                    _DRIVER.get(url)
                except TimeoutException:
                    _DRIVER.execute_script("window.stop();")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    title = _DRIVER.title or ""
                    html = _DRIVER.page_source or ""
                    if not _challenge(title, html) and len(html) > 10000:
                        return html, _DRIVER.current_url
                    time.sleep(1)
                raise RuntimeError(f"browser challenge remained: {title!r}")
            except Exception as exc:
                errors.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")
                try:
                    _DRIVER.quit()
                except Exception:
                    pass
                _DRIVER = None
        raise RuntimeError(f"Južne Vesti browser fetch failed for {url}: {'; '.join(errors)}")


def extract_article_browser(url: str, *, source: str) -> dict:
    html, retrieval_url = _page(url)
    soup = BeautifulSoup(html, "html.parser")

    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical = normalize_url(canonical_node.get("href"), source) if canonical_node else None
    requested = normalize_url(url, source)
    if canonical is None or canonical != requested:
        raise RuntimeError(
            f"Južne Vesti browser canonical mismatch for {url}: {canonical!r}"
        )

    title_node = soup.select_one(".single-post h1") or soup.select_one("h1")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    body, body_selector = article_body(soup, (".prose.max-w-none",))
    date_node = soup.select_one('meta[property="article:published_time"][content]')
    published_raw = clean(date_node.get("content")) if date_node else None
    published = parse_datetime(published_raw)
    if not title:
        raise RuntimeError(f"Južne Vesti browser title missing for {url}")
    if published is None:
        raise RuntimeError(f"Južne Vesti browser publication date missing for {url}")
    if not body or len(body) < 200:
        raise RuntimeError(f"Južne Vesti browser main text missing for {url}")

    visible_time = soup.select_one(".single-post time[datetime]")
    visible_raw = clean(visible_time.get("datetime")) if visible_time else None
    visible_date = parse_datetime(visible_raw)
    if visible_date is not None and visible_date != published:
        raise RuntimeError(f"Južne Vesti visible/meta publication date mismatch for {url}")

    dates = [{
        "kind": "published",
        "source": 'meta[property="article:published_time"]',
        "raw": published_raw,
    }]
    if visible_raw and visible_raw != published_raw:
        dates.append({
            "kind": "visible_time",
            "source": ".single-post time[datetime]",
            "raw": visible_raw,
        })
    return {
        "url": url,
        "source_domain": source,
        "country_code": "SRB",
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": "sr",
        "parser": "pure_adapter_selenium_cloudflare_fallback",
        "collection": None,
        "parser_error": None,
        "field_sources": {
            "title": ".single-post h1",
            "date_publish": 'meta[property="article:published_time"]',
            "maintext": body_selector,
        },
        "field_evidence": {
            "titles": [{
                "kind": "visible_h1",
                "source": ".single-post h1",
                "raw": title,
            }],
            "dates": dates,
            "bodies": [{
                "kind": body_selector or "selected_body",
                "text": body[:20000],
            }],
            "http_status": 200,
            "retrieval_url": retrieval_url,
            "canonical_url": canonical,
            "browser_transport": "headless_chrome",
            "page_bytes": len(html.encode("utf-8")),
        },
    }
