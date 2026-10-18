"""Fail-closed pure candidate for The Cambodia Daily Khmer."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "cambodiadaily.com"
    country_code = "KHM"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 15)
OPERATIONAL_THROUGH = date(2026, 7, 15)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "ca51e8b7fe10d73a01d484e1e55d3b1956f3e046731be6494b79d3baf5e8ea30"
CURRENT_ROUTE_EVIDENCE_SHA256 = "8f9fd5ac53da523e32e2179d827397f77d3ccf2389a1be47e45df8d4dc2e3ff6"
ROBOTS_EVIDENCE_SHA256 = "8c8cb57adb47a1b2126d3062c9f15b68c6c86eeff3a15a5b06edb7d5dd013939"
ARTICLE_EVIDENCE_SHA256 = "e0082b52ba3996bc2df622c9a9b453b51104d1d0b73261201fb9d8a3ee222ea1"
POLICY_EVIDENCE_SHA256 = "608cb80c830c37e67a8dfb49e1f3eadb2387e9ebbdd495cfba0c75f627ee9abd"
ALLOWED_HOSTS = ("cambodiadaily.com", "www.cambodiadaily.com")
CANONICAL_HOST = "www.cambodiadaily.com"
ROBOTS_URL = "https://www.cambodiadaily.com/robots.txt"
CANDIDATE_SITEMAPS = ("https://www.cambodiadaily.com/post-sitemap.xml",)
EXPECTED_LANGUAGES = ("km",)
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = (".td-post-content", ".entry-content", ".post-content", "main article")

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
            "method": "publisher_post_sitemap_pending_current_shard_article_policy_review",
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": CANDIDATE_SITEMAPS,
            **_gates(),
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("Cambodia Daily live observation remains fail-closed")


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
        raise ValueError("Cambodia Daily adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
