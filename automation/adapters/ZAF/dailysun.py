"""Fail-closed pure browser-route candidate for Daily Sun."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "dailysun.co.za"
    country_code = "ZAF"
    is_env = False
    browser_worker = True
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 17)
OPERATIONAL_THROUGH = date(2026, 8, 17)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "6ba055c411bee93a84e11136cfc75fa181bc3b387a3aaa1f7e0cd1fabbfe2cc1"
CURRENT_ROUTE_EVIDENCE_SHA256 = "cabde879ee1a6c47dc57a0c375e5b413d3a33877e5fa7734d490194e053e1cce"
ROBOTS_EVIDENCE_SHA256 = "382a59474b82080a19d078fb621431b49ea5b662bb91eca5dd3f72933eeb0459"
ARTICLE_EVIDENCE_SHA256 = "1107260604545f9cb7a4feba39fc39db68f708f2b84d68d60ad33ff43df4ffd8"
POLICY_EVIDENCE_SHA256 = "53ff2919a11202a43755074dc2c797a0bdb4e1d4fc48bcb3dfd03f3663a379b9"
ALLOWED_HOSTS = ("snl24.com", "www.snl24.com")
CANONICAL_HOST = "www.snl24.com"
ROBOTS_URL = "https://www.snl24.com/robots.txt"
LISTING_TEMPLATE = "https://www.snl24.com/news/page/{page}/"
EXPECTED_LANGUAGES = ("en", "zu")
TITLE_SELECTORS = ("main h1", "article h1", "h1")
BODY_SELECTORS = ("article", ".article-body", ".article-content")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {
        "route_profile_approved": ROUTE_PROFILE_APPROVED,
        "article_profile_approved": ARTICLE_PROFILE_APPROVED,
        "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED,
        "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED,
        "peer_order_approved": PEER_ORDER_APPROVED,
    }


def _require_live_gates(evidence: dict | None = None) -> None:
    if all(_gates().values()):
        return
    if evidence is not None:
        evidence.update(
            {
                "mode": "blocked_prelaunch_no_production_writes",
                "method": "reviewed_current_snl24_route_pending_browser_pagination_article_policy_peer_review",
                "accept_start": ACCEPT_START.isoformat(),
                "listing_template": LISTING_TEMPLATE,
                "browser_worker": True,
                **_gates(),
                "coverage_complete": False,
                "first_pass_verified_through": None,
                "strict_verified_through": None,
                "production_writes": False,
            }
        )
    raise RouteProfileUnverifiedError("Daily Sun live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_category_window(
        source_name=Config.source_name,
        start=start,
        end=end,
        evidence=evidence,
        robots_url=ROBOTS_URL,
        listing_template=LISTING_TEMPLATE,
        allowed_hosts=ALLOWED_HOSTS,
        canonical_host=CANONICAL_HOST,
        fetch=_request,
        item_selector="article",
        link_selector="a[href]",
        date_selectors=("time[datetime]",),
        max_pages=80,
    )


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
        raise ValueError("Daily Sun adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
