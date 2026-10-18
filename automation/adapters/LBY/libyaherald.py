"""Fail-closed pure candidate for Libya Herald."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "libyaherald.com"
    country_code = "LBY"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = False
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 15)
OPERATIONAL_THROUGH = date(2026, 7, 15)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "132ce4442c949fe9265ad33452766c390c81254aa404cdb697e9acbe99e0cff6"
CURRENT_ROUTE_EVIDENCE_SHA256 = "5a6c4104cf91ef893f5f5327ae262284a0465feee41511d04abb38632dee9ea5"
ROBOTS_EVIDENCE_SHA256 = "f5eaa487d83505ac91f636dd3b6431daba127d6a1225dd9f8dff094e03375b4b"
ARTICLE_EVIDENCE_SHA256 = "e68d0bc8fffe58e1e8f7d38f96eb83de56d51d97f405e1e217e82c4ec78896ba"
POLICY_EVIDENCE_SHA256 = "b41c4aba13a443e08cb6048f1afa89cab1cf65755fe62dcab148ed7bc140afb6"
ALLOWED_HOSTS = ("libyaherald.com", "www.libyaherald.com")
CANONICAL_HOST = "libyaherald.com"
ROBOTS_URL = "https://libyaherald.com/robots.txt"
CANDIDATE_SITEMAPS = ("https://libyaherald.com/wp-sitemap.xml",)
EXPECTED_LANGUAGES = ("en",)
TITLE_SELECTORS = ("main h1", "article h1", "h1")
BODY_SELECTORS = ("main article", ".post-content", ".entry-content", ".article-content")

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


def _require_live_gates(evidence: dict | None = None) -> None:
    if all(_gates().values()):
        return
    if evidence is not None:
        evidence.update({
            "mode": "blocked_prelaunch_no_production_writes",
            "method": "publisher_inventory_pending_route_robots_policy_review",
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": CANDIDATE_SITEMAPS,
            **_gates(),
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("Libya Herald live observation remains fail-closed")


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
        raise ValueError("Libya Herald adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
