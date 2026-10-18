"""Fail-closed pure candidate for Angola Online."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "angola-online.net"
    country_code = "AGO"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = False
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 10)
OPERATIONAL_THROUGH = date(2026, 8, 10)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "2b9386fdb6df126fe5044246b8a6308fef1ee89c83fec5cc709a9b845412dcc2"
CURRENT_ROUTE_EVIDENCE_SHA256 = "14e437132380132e83e2802c9ce4b3293b1055cee9d13531b1118be1ea0e6053"
ROBOTS_EVIDENCE_SHA256 = "7de32aabd08366d9a2268fe13e1bbc67048f8af3d9c9881a14a154c6d9f5d234"
ARTICLE_EVIDENCE_SHA256 = "a7cd4d6b3e97b80dfb428ddc15968be9032b19f6327d877cf9b2ba7e7a8c7d87"
POLICY_EVIDENCE_SHA256 = "5f3bb73365cde38064b10d1b7791ec566b651f2afa842941a2429d5151de42c4"
ALLOWED_HOSTS = ("angola-online.net", "www.angola-online.net")
CANONICAL_HOST = "angola-online.net"
ROBOTS_URL = "https://angola-online.net/robots.txt"
LISTING_TEMPLATE = "https://angola-online.net/noticias?page={page}"
EXPECTED_LANGUAGES = ("pt",)
TITLE_SELECTORS = ("main h1", "article h1", "h1", ".entry-title")
BODY_SELECTORS = ("main article", "article", ".entry-content", ".post-content")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {"route_profile_approved": ROUTE_PROFILE_APPROVED, "article_profile_approved": ARTICLE_PROFILE_APPROVED, "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED, "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED, "peer_order_approved": PEER_ORDER_APPROVED}


def _require_live_gates(evidence: dict | None = None) -> None:
    if all(_gates().values()):
        return
    if evidence is not None:
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "legacy_paginated_news_route_blocked_by_publisher_challenge_pending_all_current_gates", "accept_start": ACCEPT_START.isoformat(), "listing_template": LISTING_TEMPLATE, **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Angola Online live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_category_window(source_name=Config.source_name, start=start, end=end, evidence=evidence, robots_url=ROBOTS_URL, listing_template=LISTING_TEMPLATE, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, fetch=_request, item_selector="article", link_selector="a[href]", date_selectors=("time[datetime]", "meta[itemprop='datePublished'][content]"), max_pages=40)


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(url=url, payload=payload, source_name=Config.source_name, country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES, title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS, response_url=response_url)


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Angola Online adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
