"""Fail-closed pure candidate for Koha's WordPress post sitemaps."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "koha.mk"
    country_code = "MKD"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 6)
OPERATIONAL_THROUGH = date(2026, 7, 7)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "fe48d09a875d201b213d49e49e6029bc52bf03550bcfdbfb999488e96c522a21"
CURRENT_ROUTE_EVIDENCE_SHA256 = "4846bcf80def177d3d31ce636221f71a9c0ecaf178313f16defca97ad1830efb"
ROBOTS_EVIDENCE_SHA256 = "39eec22b59ebac499680c9c4f221ee35bfdf4da05803effc81489cd194f0391f"
POLICY_EVIDENCE_SHA256 = "4846bcf80def177d3d31ce636221f71a9c0ecaf178313f16defca97ad1830efb"
ALLOWED_HOSTS = ("koha.mk", "www.koha.mk")
CANONICAL_HOST = "koha.mk"
ROBOTS_URL = "https://koha.mk/robots.txt"
CANDIDATE_SITEMAPS = ("https://koha.mk/post-sitemap.xml",)
EXPECTED_LANGUAGES = ("sq", "mk")
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = ("article", ".entry-content", ".td-post-content")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError
AccessBlockedError = common.AccessBlockedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {"route_profile_approved": ROUTE_PROFILE_APPROVED, "article_profile_approved": ARTICLE_PROFILE_APPROVED, "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED, "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED}


def _require_live_gates(evidence: dict | None = None) -> None:
    if all(_gates().values()):
        return
    if evidence is not None:
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "wordpress_post_sitemap_pending_challenge_and_policy_review", "accept_start": ACCEPT_START.isoformat(), "candidate_sitemaps": list(CANDIDATE_SITEMAPS), **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Koha live observation remains fail-closed")


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
        raise ValueError("Koha adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
