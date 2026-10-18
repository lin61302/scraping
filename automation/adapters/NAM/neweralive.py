"""Fail-closed pure candidate for New Era's agriculture archive.

Current first-party evidence proves a parseable category page, article profile,
and permissive robots file.  The single reviewed listing page does not cross
the 2026-05-28 lower boundary and the probed privacy-policy route is a 404, so
live entry points remain closed before network access.
"""
from __future__ import annotations

from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters import candidate_common as common


class Config:
    source_name = "neweralive.na"
    country_code = "NAM"
    is_env = True
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 5, 28)
OPERATIONAL_THROUGH = date(2026, 5, 28)
FETCH_START = date(2026, 5, 28)
ACCEPT_START = date(2026, 5, 28)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "af74417b5c4f3dc9b2e7ae5fc65447f6e1dd69586d193705c0f07b55cfb80ea7"
CURRENT_ROUTE_EVIDENCE_SHA256 = "647bea970639bc19ad5c07e8cae37f705cfe83fb327d62cf653596ad07f1f358"
ROBOTS_EVIDENCE_SHA256 = "e47464ba818ff77f6f64f9b476319c3271659d9a9da73dded90bf75b7d70d999"
ARTICLE_EVIDENCE_SHA256 = "078a330f54e5c32cd6892d292a5406f029219a0680a09a91b496e773584c6a5b"
FAILED_POLICY_EVIDENCE_SHA256 = "21d5576d1f9efc2e1265a447e16aea5137cefb9831de763edc5021e60917f333"
ALLOWED_HOSTS = ("neweralive.na", "www.neweralive.na")
CANONICAL_HOST = "neweralive.na"
ROBOTS_URL = "https://neweralive.na/robots.txt"
LISTING_ROOT = "https://neweralive.na/category/agriculture/"
LISTING_TEMPLATE = "https://neweralive.na/category/agriculture/page/{page}/"
EXPECTED_LANGUAGES = ("en",)
ITEM_SELECTOR = "article"
LINK_SELECTOR = "h3.entry-title a[href]"
DATE_SELECTOR = "abbr.published[title]"
TITLE_SELECTORS = ("h2.entry-title", "article h2.entry-title", "meta[property='og:title']")
BODY_SELECTORS = (".entry-content", "article .entry-content", "article")
MAX_PAGES = 120

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError
ArticleProfileUnverifiedError = common.ArticleProfileUnverifiedError
AccessBlockedError = common.AccessBlockedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(
        url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout,
    )


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
            "method": "candidate_agriculture_category_pending_boundary_and_policy_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_listing_root": LISTING_ROOT,
            "candidate_listing_template": LISTING_TEMPLATE,
            "current_route_evidence_sha256": CURRENT_ROUTE_EVIDENCE_SHA256,
            "robots_evidence_sha256": ROBOTS_EVIDENCE_SHA256,
            "article_evidence_sha256": ARTICLE_EVIDENCE_SHA256,
            "failed_policy_evidence_sha256": FAILED_POLICY_EVIDENCE_SHA256,
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError(
        "New Era live observation is disabled pending archive-boundary and publisher-policy review"
    )


def _listing_url(page: int) -> str:
    return LISTING_ROOT if page == 1 else LISTING_TEMPLATE.format(page=page)


def _parse_listing(payload: bytes) -> list[tuple[str, date]]:
    soup = BeautifulSoup(payload, "html.parser")
    rows = []
    for item in soup.select(ITEM_SELECTOR):
        link = item.select_one(LINK_SELECTOR)
        stamp = item.select_one(DATE_SELECTOR)
        normalized = common.canonicalize_url(
            link.get("href") if link else None,
            allowed_hosts=ALLOWED_HOSTS,
            canonical_host=CANONICAL_HOST,
        )
        parsed = common.parse_datetime(stamp.get("title") if stamp else None)
        if normalized is None or parsed is None:
            raise RuntimeError("New Era category row lacks a canonical URL or ISO publication date")
        rows.append((normalized, parsed.date()))
    if not rows:
        raise RuntimeError("New Era category page contains no reviewed rows")
    return rows


def _discover_category_window(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > common.MAX_WINDOW_DAYS:
        raise ValueError("New Era window is invalid or exceeds 366 inclusive days")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    selected: dict[str, date] = {}
    pages = []
    newest = None
    previous_oldest = None
    lower_boundary_reached = False
    for page in range(1, MAX_PAGES + 1):
        url = _listing_url(page)
        common.require_allowed(robots, url)
        response = _request(url, max_bytes=common.MAX_LISTING_BYTES)
        rows = _parse_listing(response.content)
        dates = [published for _, published in rows]
        if dates != sorted(dates, reverse=True):
            raise RuntimeError("New Era category page is not newest-first")
        if previous_oldest is not None and max(dates) > previous_oldest:
            raise RuntimeError("New Era pagination is not monotonically newest-first")
        previous_oldest = min(dates)
        newest = max(dates) if newest is None else max(newest, max(dates))
        pages.append({
            "page": page,
            "url": url,
            "status": response.status_code,
            "bytes": len(response.content),
            "sha256": common.sha256(response.content),
            "rows": len(rows),
            "newest": max(dates).isoformat(),
            "oldest": min(dates).isoformat(),
        })
        for article_url, published in rows:
            if published >= start - timedelta(days=2):
                selected[article_url] = published
        if min(dates) < start:
            lower_boundary_reached = True
            break
    if not lower_boundary_reached:
        raise RuntimeError("New Era category did not cross the lower boundary within the page cap")
    if newest is None or newest < end:
        raise RuntimeError("New Era category did not reach the target end")
    urls = sorted(selected)
    if not urls:
        raise RuntimeError("New Era category selected no URLs")
    evidence.update({
        "mode": "pure_observation_no_production_writes",
        "method": "reviewed_newest_first_agriculture_category_with_article_page_date_validation",
        "source": Config.source_name,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "pages": pages,
        "robots_url": ROBOTS_URL,
        "robots_sha256": common.sha256(robots_response.content),
        "selected": len(urls),
        "selected_url_set_sha256": common.url_set_sha256(urls),
        "listing_date_used_as_publication_date": False,
        "article_page_date_is_authoritative": True,
        "out_of_acceptance_window_rows_are_audit_only": True,
        "coverage_complete": True,
        "first_pass_verified_through": None,
        "strict_verified_through": None,
        "production_writes": False,
    })
    return urls


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return _discover_category_window(start, end, evidence)


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(
        url=url,
        payload=payload,
        source_name=Config.source_name,
        country_code=Config.country_code,
        allowed_hosts=ALLOWED_HOSTS,
        canonical_host=CANONICAL_HOST,
        expected_languages=EXPECTED_LANGUAGES,
        title_selectors=TITLE_SELECTORS,
        body_selectors=BODY_SELECTORS,
        response_url=response_url,
    )


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("New Era adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
