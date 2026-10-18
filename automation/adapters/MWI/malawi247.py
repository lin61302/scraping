"""Production-disabled bounded pure candidate for 247 Malawi."""
from __future__ import annotations

import os
from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "247malawi.com"
    country_code = "MWI"
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
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 16)
OPERATIONAL_THROUGH = date(2026, 7, 16)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "a4606a77a0731b96c19835c2519fa27bbd56e993ff96d74f64355948cfc19ea2"
CURRENT_ROUTE_EVIDENCE_SHA256 = "2c05b0854e018162d34b9a8b844aac88e709e7e8070b35dd5c98996f6302e020"
ROBOTS_EVIDENCE_SHA256 = "13d531739cdf4f88e270e6933cdb3d3f2aa6d2659da35b306376bd0b105dccf4"
ARTICLE_EVIDENCE_SHA256 = "e1b16273696bd18df9ee97392fb9ad098c29eb6011706daa76982716b4cace0a"
POLICY_EVIDENCE_SHA256 = "019a8aee2716f8352efeaf5842c9d94695c53fe803096dfe9d515d3d945b1351"
ALLOWED_HOSTS = ("247malawi.com", "www.247malawi.com")
CANONICAL_HOST = "www.247malawi.com"
ROBOTS_URL = "https://www.247malawi.com/robots.txt"
CANDIDATE_SITEMAPS = (
    "https://www.247malawi.com/post-sitemap12.xml",
    "https://www.247malawi.com/post-sitemap13.xml",
)
EXPECTED_LANGUAGES = ("en",)
TITLE_SELECTORS = ("main h1", "article h1", "h1")
BODY_SELECTORS = ("main article", ".entry-content", ".post-content", ".td-post-content")

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
            "method": "publisher_inventory_pending_route_article_robots_policy_review",
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": CANDIDATE_SITEMAPS,
            **_gates(),
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("247 Malawi live observation remains fail-closed")


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
        raise ValueError("247 Malawi adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
