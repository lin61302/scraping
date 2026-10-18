"""Gated pure adapter candidate for El Sureste (Mexico)."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "elsureste.com.mx"
    country_code = "MEX"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 6, 17)
OPERATIONAL_THROUGH = date(2026, 6, 17)
FETCH_START = date(2026, 6, 1)
ACCEPT_START = date(2026, 6, 17)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "46e96af14f7a5db9b3c57cea74a7f5afd544f850e8e94cb933aaa024df22d7fd"
ROUTE_PROFILE_EVIDENCE = "historical_not_current_post_sitemaps_40_to_44"
CURRENT_ROUTE_EVIDENCE_SHA256 = "6af8328dceeec85b5e623cfb238958032203a7683f9f2e659e409f68d0e2ed0c"
ROBOTS_EVIDENCE_SHA256 = "a9e799bc9c5daaad259ea09944454bfc2dca282de49235cb546842f8f1782738"
ARTICLE_EVIDENCE_SHA256 = (
    "de5fea5ae9bd43e00dba609868816a2766f135a9d41af9d9ddb0b5cb8afbf40c",
    "ca3065fce1812529f4e82f30f1a6b32fd386c0dbb0fae7e076163f6e1211a150",
)
ALLOWED_HOSTS = ("elsureste.com.mx", "www.elsureste.com.mx")
CANONICAL_HOST = "www.elsureste.com.mx"
ROBOTS_URL = "https://www.elsureste.com.mx/robots.txt"
CANDIDATE_SITEMAPS = ("https://www.elsureste.com.mx/post-sitemap44.xml",)
EXPECTED_LANGUAGES = ("es",)
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
        "El Sureste live observation is disabled pending route, article, robots, and publisher-policy review"
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
        raise ValueError("El Sureste adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
