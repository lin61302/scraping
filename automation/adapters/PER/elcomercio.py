"""Fail-closed pure daily-archive candidate for El Comercio Peru."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters import candidate_common as common


class Config:
    source_name = "elcomercio.pe"
    country_code = "PER"
    is_env = False
    browser_worker = True
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 10)
OPERATIONAL_THROUGH = date(2026, 8, 10)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "0335eb8e4c7665217b1733dc73e806c70f627088de9ad94b7fe746a98fbc700f"
CURRENT_ROUTE_EVIDENCE_SHA256 = "aa1e880b38fb9a48baf11f0566e9de33ad8e336e9cab3682ea8ede79b3f2c457"
ROBOTS_EVIDENCE_SHA256 = "31fca30ce71fd6db303dba9aea3a3a9c48aecd9def37924b2508775dc85bf891"
ARTICLE_EVIDENCE_SHA256 = "c617190569010cc03607706b5615b0eab6ca4a41d0125fac191c415d358ac958"
POLICY_EVIDENCE_SHA256 = "5b76d2683292dbbe9af3c6cbfbfba9f9831591cc51e5aa9249dad75fad10ba7e"
ALLOWED_HOSTS = ("elcomercio.pe", "www.elcomercio.pe")
CANONICAL_HOST = "elcomercio.pe"
ROBOTS_URL = "https://elcomercio.pe/robots.txt"
ARCHIVE_TEMPLATE = "https://elcomercio.pe/archivo/todas/{day}/"
EXPECTED_LANGUAGES = ("es",)
TITLE_SELECTORS = ("main h1", "h1")
BODY_SELECTORS = (".story-contents__content", ".story-contents", ".sc__content")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {"route_profile_approved": ROUTE_PROFILE_APPROVED, "article_profile_approved": ARTICLE_PROFILE_APPROVED, "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED, "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED, "peer_order_approved": PEER_ORDER_APPROVED}


def _require_live_gates(evidence: dict | None = None) -> None:
    if all(_gates().values()):
        return
    if evidence is not None:
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "reviewed_daily_archive_candidate_pending_intervening_day_and_browser_completeness_policy_peer_review", "accept_start": ACCEPT_START.isoformat(), "archive_template": ARCHIVE_TEMPLATE, "browser_worker": True, **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("El Comercio live observation remains fail-closed")


def _parse_archive_page(payload: bytes, *, archive_day: date) -> list[dict]:
    soup = BeautifulSoup(payload, "html.parser")
    rows = []
    for item in soup.select(".story-item"):
        link = item.select_one("a.story-item__title[href]")
        stamp = item.select_one("time[itemprop='datePublished'][datetime]")
        visible = item.select_one("time[itemprop='datePublished']")
        raw_url = urljoin("https://elcomercio.pe/", link.get("href")) if link else None
        normalized = common.canonicalize_url(raw_url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
        parsed = common.parse_datetime(stamp.get("datetime") if stamp else None)
        visible_text = " ".join(visible.get_text(" ", strip=True).split()) if visible else ""
        expected = archive_day.strftime("%d/%m/%Y")
        if normalized is None or parsed is None or expected not in visible_text:
            raise RuntimeError("El Comercio archive row lacks a canonical article URL or exact visible archive date")
        rows.append({"url": normalized, "datetime": stamp.get("datetime"), "visible_date": expected})
    if not rows:
        raise RuntimeError("El Comercio archive page contained no reviewed story rows")
    return rows


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    if start > end or (end - start).days > common.MAX_WINDOW_DAYS:
        raise ValueError("El Comercio window is invalid or exceeds 366 inclusive days")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    urls = set()
    receipts = []
    day = start
    while day <= end:
        route = ARCHIVE_TEMPLATE.format(day=day.isoformat())
        common.require_allowed(robots, route)
        response = _request(route, max_bytes=common.MAX_LISTING_BYTES)
        rows = _parse_archive_page(response.content, archive_day=day)
        urls.update(row["url"] for row in rows)
        receipts.append({"day": day.isoformat(), "url": route, "status": response.status_code, "bytes": len(response.content), "sha256": common.sha256(response.content), "rows": len(rows)})
        day += timedelta(days=1)
    result = sorted(urls)
    evidence.update({"mode": "pure_observation_no_production_writes", "method": "reviewed_exact_daily_archive_pages_with_article_page_date_validation", "browser_worker": True, "days": receipts, "robots_sha256": common.sha256(robots_response.content), "selected": len(result), "selected_url_set_sha256": common.url_set_sha256(result), "coverage_complete": True, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    return result


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(url=url, payload=payload, source_name=Config.source_name, country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES, title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS, response_url=response_url)


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("El Comercio adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
