"""Fail-closed pure candidate for Diario Libre monthly news sitemaps."""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urlparse

from adapters import candidate_common as common


class Config:
    source_name = "diariolibre.com"
    country_code = "DOM"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = True
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 6, 23)
OPERATIONAL_THROUGH = date(2026, 6, 23)
FETCH_START = date(2026, 6, 1)
ACCEPT_START = date(2026, 6, 23)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "6bd8565a9196f85a7a7f715946be75dca9108a3d0c1d4e4de1c88177c1806fc7"
CURRENT_ROUTE_EVIDENCE_SHA256 = "e1faba3defee78ae3dadb37827737fdf62cb6323f9d10f0e696bd3b2071141bd"
ROBOTS_EVIDENCE_SHA256 = "ebb96d72eee80ec0eedda779e23d6320641c9f7977f5a814c5042d1d42b9fdd2"
ARTICLE_EVIDENCE_SHA256 = "a8a1d1bc9e631dec38d57df6b1d439bf40445fb9fe0d886d0842f1124f601cdb"
POLICY_EVIDENCE_SHA256 = "1ff3ddcf651090af6b2418437921793e01f56c08162066ee6b7660a546c69fda"
ALLOWED_HOSTS = ("diariolibre.com", "www.diariolibre.com")
CANONICAL_HOST = "www.diariolibre.com"
ROBOTS_URL = "https://www.diariolibre.com/robots.txt"
MONTHLY_SITEMAP_TEMPLATE = "https://www.diariolibre.com/sitemapnoticias/{year:04d}{month:02d}"
EXPECTED_LANGUAGES = ("es",)
DENY_PATHS = ("/revista/", "/deportes/", "/entretenimiento/", "/videos/")
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = (".article-body", ".article-content", "article")
PATH_DAY = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})(?:/|$)")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError
ArticleProfileUnverifiedError = common.ArticleProfileUnverifiedError
AccessBlockedError = common.AccessBlockedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {
        "route_profile_approved": ROUTE_PROFILE_APPROVED,
        "article_profile_approved": ARTICLE_PROFILE_APPROVED,
        "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED,
        "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED,
    }


def _require_live_gates(evidence: dict | None = None) -> None:
    gates = _gates()
    if all(gates.values()):
        return
    if evidence is not None:
        evidence.update({
            "mode": "blocked_prelaunch_no_production_writes",
            "method": "candidate_monthly_news_sitemaps_pending_route_article_robots_and_policy_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": _sitemap_urls(FETCH_START, FIRST_PASS_TARGET),
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            "current_route_evidence_sha256": CURRENT_ROUTE_EVIDENCE_SHA256,
            "robots_evidence_sha256": ROBOTS_EVIDENCE_SHA256,
            "article_evidence_sha256": ARTICLE_EVIDENCE_SHA256,
            "policy_evidence_sha256": POLICY_EVIDENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError(
        "Diario Libre live observation is disabled pending current route and policy review"
    )


def _sitemap_urls(start: date, end: date) -> list[str]:
    if start > end or (end - start).days > common.MAX_WINDOW_DAYS:
        raise ValueError("Diario Libre window is invalid or exceeds 366 inclusive days")
    cursor = date(start.year, start.month, 1)
    finish = date(end.year, end.month, 1)
    urls = []
    while cursor <= finish:
        urls.append(MONTHLY_SITEMAP_TEMPLATE.format(year=cursor.year, month=cursor.month))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    if len(urls) > 8:
        raise ValueError("Diario Libre observation exceeds eight calendar months")
    return urls


def _path_day(url: str) -> date | None:
    match = PATH_DAY.search(urlparse(url).path)
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def _discover_monthly_sitemaps(start: date, end: date, evidence: dict) -> list[str]:
    urls = _sitemap_urls(FETCH_START, end)
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    selected: dict[str, date] = {}
    receipts = []
    for sitemap_url in urls:
        common.require_allowed(robots, sitemap_url)
        response = _request(sitemap_url, max_bytes=common.MAX_SITEMAP_BYTES)
        kind, rows = common.parse_sitemap(response.content)
        if kind != "urlset":
            raise RuntimeError("Diario Libre monthly sitemap is not a urlset")
        expected_month = sitemap_url.rsplit("/", 1)[-1]
        seen = set()
        for row in rows:
            normalized = common.canonicalize_url(
                row["url"], allowed_hosts=ALLOWED_HOSTS,
                canonical_host=CANONICAL_HOST, deny_paths=DENY_PATHS,
            )
            if normalized is None:
                continue
            published = _path_day(normalized)
            if published is None or published.strftime("%Y%m") != expected_month:
                raise RuntimeError("Diario Libre monthly sitemap has an invalid or out-of-month article URL")
            if normalized in seen:
                raise RuntimeError("Diario Libre monthly sitemap repeats a canonical URL")
            seen.add(normalized)
            if start - timedelta(days=2) <= published <= end:
                selected[normalized] = published
        receipts.append({
            "month": expected_month,
            "url": sitemap_url,
            "status": response.status_code,
            "bytes": len(response.content),
            "sha256": common.sha256(response.content),
            "raw_rows": len(rows),
            "accepted_article_rows": len(seen),
        })
    result = sorted(selected)
    if not result:
        raise RuntimeError("Diario Libre monthly sitemaps selected no URLs")
    evidence.update({
        "mode": "pure_observation_no_production_writes",
        "method": "reviewed_monthly_news_sitemaps_with_url_path_inventory_dates_and_article_page_validation",
        "source": Config.source_name,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "requested_month_sitemaps": receipts,
        "robots_url": ROBOTS_URL,
        "robots_sha256": common.sha256(robots_response.content),
        "selected": len(result),
        "selected_url_set_sha256": common.url_set_sha256(result),
        "sitemap_lastmod_present": False,
        "url_path_date_used_for_inventory_membership_only": True,
        "article_page_date_is_authoritative": True,
        "out_of_acceptance_window_rows_are_audit_only": True,
        "coverage_complete": True,
        "first_pass_verified_through": None,
        "strict_verified_through": None,
        "production_writes": False,
    })
    return result


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return _discover_monthly_sitemaps(start, end, evidence)


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(
        url=url, payload=payload, source_name=Config.source_name,
        country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS,
        canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES,
        title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS,
        response_url=response_url,
    )


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Diario Libre adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
