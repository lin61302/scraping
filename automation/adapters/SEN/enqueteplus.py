"""Fail-closed pure candidate for EnQuete Plus."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "enqueteplus.com"
    country_code = "SEN"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 26)
OPERATIONAL_THROUGH = date(2026, 8, 26)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "5e5820ee8639d3db1d03e0ad54506161f38051e00e36cbaedfb71813e2e44bd7"
CURRENT_ROUTE_EVIDENCE_SHA256 = "2d5f0c7630770983a04441d4a3a129f74b75cbd07f29c8a8e3109e4e7c4a95eb"
ROBOTS_EVIDENCE_SHA256 = "278e83bcf567badfebcdea4d5d20ca9898e4449fe4eb2e3b5a08227b4ca9b762"
ARTICLE_EVIDENCE_SHA256 = "dc618a5b17517591991ee88b62f1afa04683156d2e2409e3c3d71356aa10613b"
POLICY_EVIDENCE_SHA256 = "d763022b472818a83f37a85bd2be2f67b87593046176f0283a48e1da9895ac04"
ALLOWED_HOSTS = ("enqueteplus.com", "www.enqueteplus.com")
CANONICAL_HOST = "www.enqueteplus.com"
ROBOTS_URL = "https://www.enqueteplus.com/robots.txt"
CANDIDATE_SITEMAPS = ("https://www.enqueteplus.com/sitemap.xml",)
EXPECTED_LANGUAGES = ("fr",)
TITLE_SELECTORS = ("main h1", "article h1", "h1")
BODY_SELECTORS = ("main article", ".field-name-body", ".entry-content", ".article-content")

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
            "method": "publisher_inventory_pending_route_article_robots_policy_review",
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": CANDIDATE_SITEMAPS,
            **_gates(),
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("EnQuete Plus live observation remains fail-closed")


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
        raise ValueError("EnQuete Plus adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
