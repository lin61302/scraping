"""Fail-closed pure candidate for Ziarul de Garda's annual post sitemap."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "zdg.md"
    country_code = "MDA"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = True
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 6, 23)
OPERATIONAL_THROUGH = date(2026, 6, 23)
FETCH_START = ACCEPT_START = date(2026, 6, 23)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "e1347cc88e2c0a7dca3addf1dbf69fd8ab46c5d10c0c49863ab2491f7335a8"
CURRENT_ROUTE_EVIDENCE_SHA256 = "dd52bd36eff31b3ff5920877740a59ffc98d297bb1288acbb353b53748ccefac"
ROBOTS_EVIDENCE_SHA256 = "05a530ba29e5f66bad6abfc162d4260a360a0b263fc1246c6093744d7d284544"
ARTICLE_EVIDENCE_SHA256 = "1cb5e1e12cd5c4602a79b6b281dabaebe862ad5c3d0e848036edaab233da7671"
POLICY_EVIDENCE_SHA256 = "de3d077047e0bd033e900a204c6d13a2cb286bb8e2af1638cd90d79389934c6c"
ALLOWED_HOSTS = ("zdg.md", "www.zdg.md")
CANONICAL_HOST = "www.zdg.md"
ROBOTS_URL = "https://www.zdg.md/robots.txt"
CANDIDATE_SITEMAPS = ("https://www.zdg.md/sitemap-posttype-post.2026.xml",)
EXPECTED_LANGUAGES = ("ro",)
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = ("article", ".entry-content", ".post-content")

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
    gates = _gates()
    if all(gates.values()):
        return
    if evidence is not None:
        evidence.update({
            "mode": "blocked_prelaunch_no_production_writes",
            "method": "annual_post_sitemap_pending_article_and_publisher_policy_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": list(CANDIDATE_SITEMAPS),
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            "current_route_evidence_sha256": CURRENT_ROUTE_EVIDENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("ZDG live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_sitemap_window(
        source_name=Config.source_name, start=start, end=end, evidence=evidence,
        robots_url=ROBOTS_URL, sitemap_urls=CANDIDATE_SITEMAPS,
        allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, fetch=_request,
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
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("ZDG adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
