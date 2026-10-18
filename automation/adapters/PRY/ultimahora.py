"""Fail-closed pure candidate for Ultima Hora monthly sitemaps."""
from __future__ import annotations

from datetime import date, timedelta

from adapters import candidate_common as common


class Config:
    source_name = "ultimahora.com"
    country_code = "PRY"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 6, 29)
OPERATIONAL_THROUGH = date(2026, 6, 29)
FETCH_START = date(2026, 6, 1)
ACCEPT_START = date(2026, 6, 29)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "eb9c8532d2694f00403265502e2d2e6939bef6e5c65a0333fa2d5153f64299fa"
CURRENT_ROUTE_EVIDENCE_SHA256 = "bad2935e926df40486ef5d602dcfe164df6663598cf9320d6ea406048367a30e"
ROBOTS_EVIDENCE_SHA256 = "25674fcc99a59538ca216413a220ae2e78842840f380db174fb5a7a1f4118e25"
ARTICLE_EVIDENCE_SHA256 = "0efbfeccf81c1b0fc4f1c40b8c4fdcb728de7dc07bd06db11163e82c45b6510a"
POLICY_EVIDENCE_SHA256 = "6147d72232b7c54c4efc3c16adb622c92d2b5a8977f476161013bd15f2044405"
ALLOWED_HOSTS = ("ultimahora.com", "www.ultimahora.com")
CANONICAL_HOST = "www.ultimahora.com"
ROBOTS_URL = "https://www.ultimahora.com/robots.txt"
MONTHLY_SITEMAP_TEMPLATE = "https://www.ultimahora.com/sitemap-{year:04d}{month:02d}.xml"
EXPECTED_LANGUAGES = ("es",)
DENY_PATHS = ("/live/", "/videos/", "/tv/")
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = (".article-body", ".article-content", "article")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError
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


def _sitemap_urls(start: date, end: date) -> list[str]:
    if start > end or (end - start).days > common.MAX_WINDOW_DAYS:
        raise ValueError("Ultima Hora window is invalid or exceeds 366 inclusive days")
    cursor = date(start.year, start.month, 1)
    finish = date(end.year, end.month, 1)
    result = []
    while cursor <= finish:
        result.append(MONTHLY_SITEMAP_TEMPLATE.format(year=cursor.year, month=cursor.month))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    if len(result) > 12:
        raise ValueError("Ultima Hora observation exceeds twelve monthly endpoints")
    return result


def _require_live_gates(evidence: dict | None = None) -> None:
    gates = _gates()
    if all(gates.values()):
        return
    if evidence is not None:
        evidence.update({
            "mode": "blocked_prelaunch_no_production_writes",
            "method": "monthly_sitemaps_pending_complete_route_article_and_policy_review",
            "candidate_sitemaps": _sitemap_urls(FETCH_START, FIRST_PASS_TARGET),
            "accept_start": ACCEPT_START.isoformat(),
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            "current_route_evidence_sha256": CURRENT_ROUTE_EVIDENCE_SHA256,
            **gates, "coverage_complete": False,
            "first_pass_verified_through": None, "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("Ultima Hora live observation remains fail-closed")


def _discover_monthly_sitemaps(start: date, end: date, evidence: dict) -> list[str]:
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    selected: dict[str, date] = {}
    receipts = []
    for endpoint in _sitemap_urls(date(start.year, start.month, 1), end):
        common.require_allowed(robots, endpoint)
        response = _request(endpoint, max_bytes=common.MAX_SITEMAP_BYTES)
        kind, rows = common.parse_sitemap(response.content)
        if kind != "urlset":
            raise RuntimeError("Ultima Hora monthly endpoint is not a urlset")
        month = endpoint.rsplit("-", 1)[-1].split(".", 1)[0]
        accepted = 0
        for row in rows:
            normalized = common.canonicalize_url(
                row["url"], allowed_hosts=ALLOWED_HOSTS,
                canonical_host=CANONICAL_HOST, deny_paths=DENY_PATHS,
            )
            modified = common.parse_datetime(row.get("lastmod"))
            if normalized is None:  # The publisher repeats its root URL in monthly files.
                continue
            if modified is None:
                raise RuntimeError("Ultima Hora sitemap row has invalid lastmod")
            day = modified.date()
            if day.strftime("%Y%m") != month:
                continue
            accepted += 1
            if start - timedelta(days=2) <= day <= end:
                selected[normalized] = day
        if not accepted:
            raise RuntimeError("Ultima Hora monthly endpoint has no in-month article rows")
        receipts.append({"url": endpoint, "sha256": common.sha256(response.content), "rows": len(rows), "in_month_rows": accepted})
    urls = sorted(selected)
    if not urls:
        raise RuntimeError("Ultima Hora monthly sitemaps selected no URLs")
    evidence.update({
        "mode": "pure_observation_no_production_writes",
        "method": "reviewed_monthly_sitemaps_with_article_page_date_validation",
        "source": Config.source_name, "requested_start": start.isoformat(),
        "requested_end": end.isoformat(), "sitemap_endpoints": receipts,
        "robots_sha256": common.sha256(robots_response.content),
        "selected": len(urls), "selected_url_set_sha256": common.url_set_sha256(urls),
        "sitemap_lastmod_used_as_publication_date": False,
        "article_page_date_is_authoritative": True,
        "out_of_acceptance_window_rows_are_audit_only": True,
        "coverage_complete": True, "first_pass_verified_through": None,
        "strict_verified_through": None, "production_writes": False,
    })
    return urls


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
        raise ValueError("Ultima Hora adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
