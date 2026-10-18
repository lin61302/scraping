"""Fail-closed pure candidate for Umuseke."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "umuseke.rw"
    country_code = "RWA"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = True
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 6)
OPERATIONAL_THROUGH = date(2026, 8, 6)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "74a39e38da0f5fc6a5d8f02caceab54ef7d9be53d198e107ed0a8cac92d5371f"
CURRENT_ROUTE_EVIDENCE_SHA256 = "5f8e814a29056df4146dbb165906980603d6c009210fba17c0ccaa0f8d9f3118"
ROBOTS_EVIDENCE_SHA256 = "e14f077f88a2d8a758a44f9da8386abb44b8fc28b5ff2d98b45cfb14309b8b6a"
ARTICLE_EVIDENCE_SHA256 = "07125c2be6227b6b9d196b753b8a5a254db5328a9c629e272ae8bc6b2785d3cd"
POLICY_EVIDENCE_SHA256 = "c12a7cd3f020793e9e60521e0fcd74292dcff6f619807fc4d021949580fccbe1"
ALLOWED_HOSTS = ("umuseke.rw", "www.umuseke.rw")
CANONICAL_HOST = "umuseke.rw"
ROBOTS_URL = "https://umuseke.rw/robots.txt"
CANDIDATE_SITEMAPS = ("https://umuseke.rw/wp-sitemap-posts-post-10.xml",)
EXPECTED_LANGUAGES = ("rw", "en", "fr")
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
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "reviewed_single_sitemap_shard_pending_article_policy_and_peer_review", "accept_start": ACCEPT_START.isoformat(), "candidate_sitemaps": CANDIDATE_SITEMAPS, **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Umuseke live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_sitemap_window(source_name=Config.source_name, start=start, end=end, evidence=evidence, robots_url=ROBOTS_URL, sitemap_urls=CANDIDATE_SITEMAPS, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, fetch=_request)


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(url=url, payload=payload, source_name=Config.source_name, country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES, title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS, response_url=response_url)


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Umuseke adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
