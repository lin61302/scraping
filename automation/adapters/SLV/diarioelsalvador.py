"""Fail-closed pure candidate for Diario El Salvador."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "diarioelsalvador.com"
    country_code = "SLV"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 23)
OPERATIONAL_THROUGH = date(2026, 7, 23)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "38a9097264e9892bc3eb5086f7971f05d2b7b1cdff920a58577818eb48e25d82"
CURRENT_ROUTE_EVIDENCE_SHA256 = "318c1f3b6e90db59e8b14c5e655d17f7136e17875813ace2ca6ff5e43fad05cf"
ROBOTS_EVIDENCE_SHA256 = "8bdbba0083ce61661f434f58fbbd67322d36d47839a6642fad89abc276dc25fb"
ARTICLE_EVIDENCE_SHA256 = "58b5bffd7e01197e0bc5cbbac80beaa6a07129400d2e42744ae4453d4a92fc3b"
POLICY_EVIDENCE_SHA256 = "91e77e18290264985724f6a856edce9f4dd1d0a43bfb06d68e4f83a053e5a7df"
ALLOWED_HOSTS = ("diarioelsalvador.com", "www.diarioelsalvador.com")
CANONICAL_HOST = "diarioelsalvador.com"
ROBOTS_URL = "https://diarioelsalvador.com/robots.txt"
CANDIDATE_SITEMAPS = ("https://diarioelsalvador.com/sitemap.xml",)
EXPECTED_LANGUAGES = ("es",)
TITLE_SELECTORS = ("main h1", "article h1", "h1")
BODY_SELECTORS = (".article-body", ".article-content", ".content-news", "main article", "article")

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
        "peer_order_approved": PEER_ORDER_APPROVED,
    }


def _require_live_gates(evidence: dict | None = None) -> None:
    if all(_gates().values()):
        return
    if evidence is not None:
        evidence.update({
            "mode": "blocked_prelaunch_no_production_writes",
            "method": "publisher_inventory_pending_route_policy_and_peer_order_review",
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": CANDIDATE_SITEMAPS,
            **_gates(),
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("Diario El Salvador live observation remains fail-closed")


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
        raise ValueError("Diario El Salvador adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
