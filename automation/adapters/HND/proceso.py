"""Fail-closed pure candidate for Proceso Digital."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "proceso.hn"
    country_code = "HND"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = True
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 29)
OPERATIONAL_THROUGH = date(2026, 7, 29)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "5c7a7eafbe92b5903fbf02c55261d39d1d02653259026bf7952fdf72637a1ef7"
CURRENT_ROUTE_EVIDENCE_SHA256 = "fc2845dcc22b7cd117fcf7be0ca07da24690922ea7f2dd2ea9e2860d0e5849ad"
ROBOTS_EVIDENCE_SHA256 = "13b1204a93fdae1b8434ae7177956e6fa98d5643846df0734820fe264429e025"
ARTICLE_EVIDENCE_SHA256 = "2e49dac833301a9ccb6c3b10db3c3e1aa1f33402ac83d99c2e5443a1765c3529"
POLICY_EVIDENCE_SHA256 = "89ad6497cab53325d088f6ef6b232fcb1a4bd0616331228cad07b5f5f1314477"
ALLOWED_HOSTS = ("proceso.hn", "www.proceso.hn")
CANONICAL_HOST = "proceso.hn"
ROBOTS_URL = "https://proceso.hn/robots.txt"
CANDIDATE_SITEMAPS = tuple(f"https://proceso.hn/post-sitemap{index}.xml" for index in range(410, 418))
EXPECTED_LANGUAGES = ("es", "en")
TITLE_SELECTORS = ("main h1", "article h1", "h1", ".entry-title")
BODY_SELECTORS = ("main article", "article", ".entry-content", ".post-content")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {"route_profile_approved": ROUTE_PROFILE_APPROVED, "article_profile_approved": ARTICLE_PROFILE_APPROVED, "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED, "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED, "peer_order_approved": PEER_ORDER_APPROVED}


def _require_live_gates(evidence: dict | None = None) -> None:
    if all(_gates().values()):
        return
    if evidence is not None:
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "reviewed_sitemap_inventory_pending_article_policy_and_peer_review", "accept_start": ACCEPT_START.isoformat(), "candidate_sitemaps": CANDIDATE_SITEMAPS, **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Proceso Digital live observation remains fail-closed")


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
        raise ValueError("Proceso Digital adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
