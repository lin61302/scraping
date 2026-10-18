"""Gated pure adapter candidate for Echaab (Algeria)."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "echaab.dz"
    country_code = "DZA"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = False
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 9, 3)
OPERATIONAL_THROUGH = date(2026, 9, 3)
FETCH_START = date(2026, 9, 1)
ACCEPT_START = date(2026, 9, 3)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "d6cc7a53eb3e1e504f49fe0222913c2741ff2644870bae39e82b656333c5b549"
ROUTE_PROFILE_EVIDENCE = "historical_not_current_post_sitemaps_77_to_81"
ALLOWED_HOSTS = ("echaab.dz", "www.echaab.dz")
CANONICAL_HOST = "www.echaab.dz"
ROBOTS_URL = "https://www.echaab.dz/robots.txt"
CANDIDATE_SITEMAPS = ("https://www.echaab.dz/post-sitemap81.xml",)
EXPECTED_LANGUAGES = ("ar",)
DENY_PATHS = ("/category/", "/tag/", "/author/", "/feed/", "/wp-admin/")
TITLE_SELECTORS = ("article h1.entry-title", "article h1", "main h1", "h1")
BODY_SELECTORS = ("article .entry-content", ".post-content", "article")

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
        "Echaab live observation is disabled pending route, article, robots, and publisher-policy review"
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
        raise ValueError("Echaab adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
