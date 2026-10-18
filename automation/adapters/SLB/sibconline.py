"""Production-disabled bounded pure candidate for SIBC Online."""
from __future__ import annotations

import os
from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "sibconline.com.sb"
    country_code = "SLB"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False
    observation_ready = True


PRODUCTION_WRITES = False
OBSERVATION_READY = True
BOUNDED_ROUTE_INVENTORY = True
ROUTE_PROFILE_APPROVED = True
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 29)
OPERATIONAL_THROUGH = date(2026, 7, 29)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "092d2d41f4f017fe7be250a7410978b9fd8de7e2dcd600e1b6fddafe278b26af"
CURRENT_ROUTE_EVIDENCE_SHA256 = "fc2845dcc22b7cd117fcf7be0ca07da24690922ea7f2dd2ea9e2860d0e5849ad"
ROBOTS_EVIDENCE_SHA256 = "70e7e560434f354dfaf5de5285104d7b158ef4298766b8066aab26f5faaf2091"
ARTICLE_EVIDENCE_SHA256 = "4c1373d603794dbac5af8db81edf44742ab4bafc260940b68de3d049b6f7abf6"
POLICY_EVIDENCE_SHA256 = "0da985ed35c95550f14aec838d957e7b242964047641d22792feb7f669c3ae94"
ALLOWED_HOSTS = ("sibconline.com.sb", "www.sibconline.com.sb")
CANONICAL_HOST = "www.sibconline.com.sb"
ROBOTS_URL = "https://www.sibconline.com.sb/robots.txt"
CANDIDATE_SITEMAPS = ("https://www.sibconline.com.sb/sitemaps/posts-1.xml",)
EXPECTED_LANGUAGES = ("en",)
TITLE_SELECTORS = ("main h1", "article h1", "h1", ".entry-title", ".post-title")
BODY_SELECTORS = ("main article", "article", ".entry-content", ".post-content")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {"route_profile_approved": ROUTE_PROFILE_APPROVED, "article_profile_approved": ARTICLE_PROFILE_APPROVED, "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED, "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED, "peer_order_approved": PEER_ORDER_APPROVED}


def _observation_gates() -> dict[str, bool]:
    return {
        "controller_opt_in_or_production_ready": (
            os.environ.get("SCRAPER_BOUNDED_OBSERVATION") == "1"
            or all(_gates().values())
        ),
        "observation_flag_enabled": (
            OBSERVATION_READY is True and Config.observation_ready is True
        ),
        "production_writes_disabled": (
            PRODUCTION_WRITES is False and Config.production_writes is False
        ),
        "bounded_route_inventory": BOUNDED_ROUTE_INVENTORY,
        "bounded_acceptance_window": (
            ACCEPT_START <= FIRST_PASS_TARGET
            and (FIRST_PASS_TARGET - ACCEPT_START).days <= common.MAX_WINDOW_DAYS
        ),
        "article_profile_approved": ARTICLE_PROFILE_APPROVED,
        "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED,
    }


def _require_live_gates(evidence: dict | None = None) -> None:
    production_gates = _gates()
    observation_gates = _observation_gates()
    if Config.observation_ready and all(observation_gates.values()):
        if evidence is not None:
            evidence.update(
                {
                    "admission": "bounded_pure_observation",
                    "observation_ready": True,
                    "launch_ready": False,
                    "promotion_eligible": False,
                    "production_gates_satisfied": all(production_gates.values()),
                    "first_pass_verified_through": None,
                    "strict_verified_through": None,
                    "production_writes": False,
                    **production_gates,
                    **observation_gates,
                }
            )
        return
    if evidence is not None:
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "reviewed_sitemap_inventory_pending_policy_and_peer_review", "accept_start": ACCEPT_START.isoformat(), "candidate_sitemaps": CANDIDATE_SITEMAPS, **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("SIBC Online live observation remains fail-closed")


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
        raise ValueError("SIBC Online adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
