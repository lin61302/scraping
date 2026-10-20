"""Production-disabled bounded pure candidate for The Liberian Investigator."""
from __future__ import annotations

import os
from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "liberianinvestigator.com"
    country_code = "LBR"
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
PUBLISHER_POLICY_REVIEWED = True
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 22)
OPERATIONAL_THROUGH = date(2026, 7, 22)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "9ff96c5f70d9c3d8cbeb10edb5b83ea2cb65951d95fbaefe43a60ec7941c38c6"
CURRENT_ROUTE_EVIDENCE_SHA256 = "318c1f3b6e90db59e8b14c5e655d17f7136e17875813ace2ca6ff5e43fad05cf"
ROBOTS_EVIDENCE_SHA256 = "5dfe0ad9963d7441590e7c6fef1197a7e067e0939482127f5bf93c5168c419e5"
ARTICLE_EVIDENCE_SHA256 = "e8d089e232e892aefa59b7d8da46a589145f08cfe475fde643afd676e31085a3"
POLICY_EVIDENCE_SHA256 = "c71e09d95182079a1e8fcfc949e71ae63ad99f7bba173a43b04d81a85e247eea"
ALLOWED_HOSTS = ("liberianinvestigator.com", "www.liberianinvestigator.com")
CANONICAL_HOST = "liberianinvestigator.com"
ROBOTS_URL = "https://liberianinvestigator.com/robots.txt"
CANDIDATE_SITEMAPS = (
    "https://liberianinvestigator.com/post-sitemap7.xml",
    "https://liberianinvestigator.com/post-sitemap8.xml",
)
EXPECTED_LANGUAGES = ("en",)
TITLE_SELECTORS = ("main h1", "article h1", "h1")
BODY_SELECTORS = (".entry-content", ".content-inner", "main article", "article")

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
        "peer_order_approved": PEER_ORDER_APPROVED,
    }


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
        evidence.update({
            "mode": "blocked_prelaunch_no_production_writes",
            "method": "publisher_inventory_pending_peer_order_review",
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": CANDIDATE_SITEMAPS,
            **_gates(),
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("Liberian Investigator live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_sitemap_window(
        source_name=Config.source_name,
        start=start,
        end=end,
        evidence=evidence,
        robots_url=ROBOTS_URL,
        sitemap_urls=CANDIDATE_SITEMAPS,
        allowed_hosts=ALLOWED_HOSTS,
        canonical_host=CANONICAL_HOST,
        fetch=_request,
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
        raise ValueError("Liberian Investigator adapter refused a noncanonical URL")
    article_request_url = target + "/"
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, article_request_url)
    response = _request(article_request_url, max_bytes=common.MAX_ARTICLE_BYTES)
    if response.url != article_request_url:
        raise common.AccessBlockedError("Liberian Investigator returned an unexpected article response URL")
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
