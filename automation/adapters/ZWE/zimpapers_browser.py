"""Shared browser-backed WordPress transport for the unified Zimpapers site.

The publisher protects both the HTML site and WordPress REST API with a
JavaScript Cloudflare challenge.  A single headless Chrome session obtains a
normal first-party clearance cookie, then same-origin ``fetch`` requests page
through the date-bounded publisher API.  This module has no database imports
or write path.
"""
from __future__ import annotations

import atexit
import json
import threading
import time
from datetime import date
from urllib.parse import urlencode

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options

from adapters.ZWE.common import normalize_url, parse_datetime, wordpress_rest_article


BASE = "https://www.heraldonline.co.zw"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_DRIVER = None
_LOCK = threading.RLock()


class PublisherAccessError(RuntimeError):
    pass


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
    # The home page keeps ad and analytics requests alive long after its
    # first-party HTML and clearance cookie are usable.
    options.page_load_strategy = "eager"
    for argument in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1440,2200",
        "--disable-blink-features=AutomationControlled",
        "--lang=en-US",
        f"--user-agent={USER_AGENT}",
    ):
        options.add_argument(argument)
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(45)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def _challenge_title(title: str) -> bool:
    lowered = (title or "").casefold()
    return any(value in lowered for value in ("just a moment", "access denied", "attention required"))


def _driver_with_clearance():
    global _DRIVER
    with _LOCK:
        if _DRIVER is not None:
            return _DRIVER
        errors = []
        for attempt in range(2):
            driver = _new_driver()
            try:
                try:
                    driver.get(BASE + "/")
                except TimeoutException:
                    # A renderer timeout does not invalidate a clearance that
                    # has already completed.  Inspect the live page below.
                    pass
                deadline = time.monotonic() + 70
                while time.monotonic() < deadline:
                    if not _challenge_title(driver.title) and len(driver.page_source) > 50000:
                        _DRIVER = driver
                        return driver
                    time.sleep(2)
                errors.append(f"attempt {attempt + 1}: challenge remained ({driver.title!r})")
            except Exception as exc:
                errors.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")
            try:
                driver.quit()
            except Exception:
                pass
        raise PublisherAccessError("Zimpapers browser clearance failed: " + "; ".join(errors))


def _same_origin_json(url: str) -> tuple[object, dict[str, str]]:
    script = """
        const done = arguments[arguments.length - 1];
        fetch(arguments[0], {credentials: 'include', cache: 'no-store'})
          .then(async response => {
            const headers = {};
            response.headers.forEach((value, key) => { headers[key.toLowerCase()] = value; });
            done({status: response.status, headers: headers, text: await response.text()});
          })
          .catch(error => done({error: String(error)}));
    """
    errors = []
    for attempt in range(3):
        try:
            with _LOCK:
                driver = _driver_with_clearance()
                driver.set_script_timeout(90)
                result = driver.execute_async_script(script, url)
            if not isinstance(result, dict) or result.get("error"):
                raise PublisherAccessError(
                    f"Zimpapers API browser fetch failed for {url}: {result}"
                )
            status = int(result.get("status") or 0)
            if status != 200:
                raise PublisherAccessError(
                    f"Zimpapers API returned HTTP {status} for {url}"
                )
            try:
                payload = json.loads(result.get("text") or "")
            except ValueError as exc:
                raise PublisherAccessError(
                    f"Zimpapers API returned non-JSON content for {url}"
                ) from exc
            return payload, {
                str(key).lower(): str(value)
                for key, value in (result.get("headers") or {}).items()
            }
        except PublisherAccessError as exc:
            errors.append(f"attempt {attempt + 1}: {exc}")
            _close_driver()
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise PublisherAccessError("; ".join(errors))


def discover_category(
    *, category_id: int, start: date, end: date, evidence: dict, post_cache: dict[str, dict]
) -> list[str]:
    if start > end:
        raise ValueError("start date follows end date")
    params = {
        "after": f"{start.isoformat()}T00:00:00",
        "before": f"{end.isoformat()}T23:59:59",
        "categories": category_id,
        "per_page": 100,
        "orderby": "date",
        "order": "asc",
        # Discovery needs only identity and publication metadata.  Fetching
        # rendered bodies for every URL made large pages fragile and wasted
        # bandwidth when the observer intentionally selects a small canary.
        "_fields": "id,date,slug,link,categories",
    }
    endpoint = BASE + "/wp-json/wp/v2/posts"
    evidence.update({
        "strategy": "headless_browser_clearance_then_date_bounded_wordpress_rest",
        "discovery_endpoint": endpoint,
        "publication_date_window": {"start": start.isoformat(), "end": end.isoformat()},
        "category_id": category_id,
        "pages_checked": [],
        "coverage_complete": False,
        "browser_transport": "headless_chrome_same_origin_fetch",
    })
    post_cache.clear()
    urls = []
    duplicate_records = []
    expected_total = None
    expected_pages = None
    for page in range(1, 201):
        request_url = endpoint + "?" + urlencode({**params, "page": page})
        rows, headers = _same_origin_json(request_url)
        if not isinstance(rows, list):
            raise RuntimeError(f"Zimpapers posts endpoint returned {type(rows).__name__}, expected list")
        if expected_total is None:
            try:
                expected_total = int(headers["x-wp-total"])
                expected_pages = int(headers["x-wp-totalpages"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("Zimpapers API omitted count-reconciliation headers") from exc
            evidence["publisher_total"] = expected_total
            evidence["publisher_pages"] = expected_pages
            if expected_pages > 200:
                raise RuntimeError(f"Zimpapers API needs {expected_pages} pages, above safety ceiling")
        evidence["pages_checked"].append({
            "page": page,
            "rows": len(rows),
            "newest": rows[-1].get("date") if rows else None,
            "oldest": rows[0].get("date") if rows else None,
            "url": request_url,
        })
        for post in rows:
            published = parse_datetime(post.get("date"))
            if published is None or not start <= published.date() <= end:
                raise RuntimeError(f"Zimpapers returned an out-of-window post on page {page}")
            if category_id not in (post.get("categories") or []):
                raise RuntimeError(f"Zimpapers returned a post outside category {category_id}")
            url = normalize_url(post.get("link") or "", "heraldonline.co.zw")
            if not url:
                raise RuntimeError(f"Zimpapers returned a non-canonical article URL on page {page}")
            if url in post_cache:
                previous = post_cache[url]
                duplicate_records.append({
                    "url": url,
                    "kept_post_id": previous.get("id"),
                    "kept_date": previous.get("date"),
                    "duplicate_post_id": post.get("id"),
                    "duplicate_date": post.get("date"),
                })
                continue
            cached = dict(post)
            cached["_discovery_url"] = request_url
            post_cache[url] = cached
            urls.append(url)
        if page >= expected_pages:
            break
    accounted_records = len(urls) + len(duplicate_records)
    if expected_total is None or accounted_records != expected_total:
        raise RuntimeError(
            f"Zimpapers count mismatch: expected {expected_total}, "
            f"unique {len(urls)}, duplicate records {len(duplicate_records)}"
        )
    evidence["publisher_duplicate_canonical_records"] = duplicate_records
    evidence["publisher_duplicate_canonical_record_count"] = len(duplicate_records)
    evidence["discovered_urls_after_filter"] = len(urls)
    evidence["coverage_complete"] = True
    evidence["boundary_reached"] = True
    return urls


def article_from_cache(url: str, *, source_name: str, post_cache: dict[str, dict]) -> dict:
    post = post_cache.get(url)
    if post is None:
        raise RuntimeError(f"Zimpapers post was not cached during discovery: {url}")
    post_id = post.get("id")
    if not isinstance(post_id, int):
        raise RuntimeError(f"Zimpapers cached post has no integer id: {url}")
    article_url = (
        BASE + f"/wp-json/wp/v2/posts/{post_id}?"
        + urlencode({"_fields": "id,date,slug,link,title,content,categories"})
    )
    full_post, _ = _same_origin_json(article_url)
    if not isinstance(full_post, dict) or full_post.get("id") != post_id:
        raise RuntimeError(f"Zimpapers article API identity mismatch for {url}")
    full_post["_discovery_url"] = article_url
    post_cache[url] = full_post
    article, _ = wordpress_rest_article(url, source=source_name, post=full_post)
    article["publisher_host"] = "heraldonline.co.zw"
    article["publisher_category_ids"] = full_post.get("categories") or []
    return article
