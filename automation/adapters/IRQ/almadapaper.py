"""Fail-closed pure candidate for Al Mada."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "almadapaper.net"
    country_code = "IRQ"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 6)
OPERATIONAL_THROUGH = date(2026, 8, 6)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "0698ff3f46412f095aa93ae459e857efbc232ca0704ac7d5a06ba347a5060c5e"
CURRENT_ROUTE_EVIDENCE_SHA256 = "459709ea72f85165aaf835b9fefe857595fa2338e4886aa716cbc509e72700a6"
ROBOTS_EVIDENCE_SHA256 = "84a7d4ce72853d133c4981da8328052fe4165fcfc100c9d2cdabf1a53a454e94"
ARTICLE_EVIDENCE_SHA256 = "faef2c531b4846cc31ed1e284db5fc27b5ac60d70e5492465dcabbf35b8a965a"
POLICY_EVIDENCE_SHA256 = "38f0be6c39282258745a6be80a3f9b330b1f78805894b4ac347486f3e3685f92"
ALLOWED_HOSTS = ("almadapaper.net", "www.almadapaper.net")
CANONICAL_HOST = "almadapaper.net"
ROBOTS_URL = "https://almadapaper.net/robots.txt"
CANDIDATE_SITEMAPS = (
    "https://almadapaper.net/post-sitemap3156.xml",
    "https://almadapaper.net/post-sitemap3157.xml",
    "https://almadapaper.net/post-sitemap3178.xml",
    "https://almadapaper.net/post-sitemap3179.xml",
    "https://almadapaper.net/post-sitemap3180.xml",
)
EXPECTED_LANGUAGES = ("ar",)
TITLE_SELECTORS = ("main h1", "article h1", "h1", ".entry-title")
BODY_SELECTORS = ("main article", "article", ".entry-content", ".post-content", ".td-post-content")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {"route_profile_approved": ROUTE_PROFILE_APPROVED, "article_profile_approved": ARTICLE_PROFILE_APPROVED, "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED, "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED, "peer_order_approved": PEER_ORDER_APPROVED}


def _require_live_gates(evidence: dict | None = None) -> None:
    if all(_gates().values()):
        return
    if evidence is not None:
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "reviewed_sitemap_index_pending_full_bounded_route_article_policy_and_peer_review", "accept_start": ACCEPT_START.isoformat(), "candidate_sitemaps": CANDIDATE_SITEMAPS, **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Al Mada live observation remains fail-closed")


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
        raise ValueError("Al Mada adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
