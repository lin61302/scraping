"""Fail-closed pure candidate for Articulo 66's newest-first category."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "articulo66.com"
    country_code = "NIC"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = OPERATIONAL_THROUGH = FETCH_START = ACCEPT_START = date(2026, 7, 2)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "ad20fefb2cf0f610be095c98051021c4be13eb3a4ff8b39c659665161871f62d"
CURRENT_ROUTE_EVIDENCE_SHA256 = "1178878a8b5255edacca9da23dce5152c64bb32f115fb1333ebb1708061f6abf"
ROBOTS_EVIDENCE_SHA256 = "ec701a4ddf3269b30eb8a5f9bf98d4267f540785cfe3ffa2450e18309867a6cc"
ARTICLE_EVIDENCE_SHA256 = "b37450ba0f433c352ccf6a311e652c006ee1f8c1f883b34e913d4bb060689feb"
POLICY_EVIDENCE_SHA256 = "aa26e9f989939a10b368f9aff16613c6d1bf27d35cdabd867efa3483c4811e72"
ALLOWED_HOSTS = ("articulo66.com", "www.articulo66.com")
CANONICAL_HOST = "www.articulo66.com"
ROBOTS_URL = "https://www.articulo66.com/robots.txt"
LISTING_TEMPLATE = "https://www.articulo66.com/categorias/nacionales/page/{page}/"
EXPECTED_LANGUAGES = ("es",)
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = ("article", ".entry-content", ".post-content")

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
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "newest_first_category_pending_boundary_article_policy_review", "accept_start": ACCEPT_START.isoformat(), "listing_template": LISTING_TEMPLATE, **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Articulo 66 live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_category_window(source_name=Config.source_name, start=start, end=end, evidence=evidence, robots_url=ROBOTS_URL, listing_template=LISTING_TEMPLATE, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, fetch=_request, item_selector="article", link_selector="a[href]", date_selectors=("time[datetime]", "meta[itemprop='datePublished']"), max_pages=120)


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(url=url, payload=payload, source_name=Config.source_name, country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES, title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS, structured_article_required=False, response_url=response_url)


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Articulo 66 adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
