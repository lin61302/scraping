"""Gated pure adapter candidate for North Coast Times Jamaica.

The historical numbered-sitemap profile is useful engineering evidence, but
current robots, publisher policy, route frontier, and article fields have not
been captured locally.  Every live entry point therefore fails before network
access until those four reviews are recorded in this file.
"""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "northcoasttimesja.com"
    country_code = "JAM"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = True
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 8, 28)
OPERATIONAL_THROUGH = date(2026, 8, 28)
FETCH_START = date(2026, 8, 1)
ACCEPT_START = date(2026, 8, 28)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "325cb4f605872be7384d9473f31c7de04007c323461cba39922cfc18545f5299"
ROUTE_PROFILE_EVIDENCE = "current_20261015_post_sitemap4_sha256_213b0918ca081d40e29d199485605958bf48b179d9a24a55a339b81a7a76e7b5"
ROBOTS_EVIDENCE_SHA256 = "21e0ddc2c6bdfbfa326792837fa4c158970903e00b7971ba59833aa7d9ab908a"
ARTICLE_EVIDENCE_SHA256 = (
    "35fa8a81587a48469e0b08eb1b1fcfbe730c9aa96e8acd228259a779af33ea14",
    "fe2da392c0d128aa0727b198e70271f49ee7d704666e8f34e8bf74881ca8d11e",
)
ALLOWED_HOSTS = ("northcoasttimesja.com", "www.northcoasttimesja.com")
CANONICAL_HOST = "northcoasttimesja.com"
ROBOTS_URL = "https://northcoasttimesja.com/robots.txt"
CANDIDATE_SITEMAPS = ("https://northcoasttimesja.com/post-sitemap4.xml",)
EXPECTED_LANGUAGES = ("en",)
DENY_PATHS = ("/category/", "/tag/", "/author/", "/feed/", "/wp-admin/")
TITLE_SELECTORS = ("article h1.entry-title", "article h1", "main h1", "h1")
BODY_SELECTORS = (".entry-content", "article .entry-content", ".post-content", "article")

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
            "method": "candidate_numbered_wordpress_sitemap_profile_pending_current_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": list(CANDIDATE_SITEMAPS),
            "route_profile_evidence": ROUTE_PROFILE_EVIDENCE,
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError(
        "North Coast Times live observation is disabled pending route, article, robots, and publisher-policy review"
    )


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_sitemap_window(
        source_name=Config.source_name, start=start, end=end, evidence=evidence,
        robots_url=ROBOTS_URL, sitemap_urls=CANDIDATE_SITEMAPS,
        allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST,
        fetch=_request, deny_paths=DENY_PATHS,
    )


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(
        url=url, payload=payload, source_name=Config.source_name,
        country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS,
        canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES,
        title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS,
        response_url=response_url,
    )


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(
        url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST,
    )
    if target != url.rstrip("/"):
        raise ValueError("North Coast Times adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
