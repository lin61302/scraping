"""Fail-closed pure candidate for BeritaSatu's news archive.

The current robots file was captured, but both the news route and policy route
returned publisher 403 responses to the truthful bounded observer.  No live
article profile was reached.  All live entry points remain closed before
network access.
"""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "beritasatu.com"
    country_code = "IDN"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 6, 15)
OPERATIONAL_THROUGH = date(2026, 6, 15)
FETCH_START = date(2026, 6, 15)
ACCEPT_START = date(2026, 6, 15)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "1a5c71556639098a44bfb247976a2821cdf1d980be9943280cddcd7fee0c6726"
ROBOTS_EVIDENCE_SHA256 = "b94a6d2b9b7e8690b2534d3e6135cefb1d91f25bbeb4472c91d2a688d4d3bec0"
BLOCKED_ROUTE_EVIDENCE_SHA256 = "54b60e71f53a65db2555c7b75972bf4975e8a06c0b9da486414f24c23df3f03a"
BLOCKED_POLICY_EVIDENCE_SHA256 = "36405e9968a01bab722b125dd0049008bad98fe38e5c47a6930ba34a413d6af3"
ALLOWED_HOSTS = ("beritasatu.com", "www.beritasatu.com")
CANONICAL_HOST = "www.beritasatu.com"
ROBOTS_URL = "https://www.beritasatu.com/robots.txt"
LISTING_TEMPLATE = "https://www.beritasatu.com/news?page={page}"
EXPECTED_LANGUAGES = ("id",)
ITEM_SELECTOR = "article"
LINK_SELECTOR = "a[href]"
DATE_SELECTORS = ("time[datetime]", "meta[itemprop='datePublished'][content]")
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = (".article-content", ".content-article", "article")

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
            "method": "candidate_news_archive_blocked_by_publisher_403",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_listing_template": LISTING_TEMPLATE,
            "robots_evidence_sha256": ROBOTS_EVIDENCE_SHA256,
            "blocked_route_evidence_sha256": BLOCKED_ROUTE_EVIDENCE_SHA256,
            "blocked_policy_evidence_sha256": BLOCKED_POLICY_EVIDENCE_SHA256,
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError(
        "BeritaSatu live observation is disabled after the reviewed publisher 403"
    )


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_category_window(
        source_name=Config.source_name,
        start=start,
        end=end,
        evidence=evidence,
        robots_url=ROBOTS_URL,
        listing_template=LISTING_TEMPLATE,
        allowed_hosts=ALLOWED_HOSTS,
        canonical_host=CANONICAL_HOST,
        fetch=_request,
        item_selector=ITEM_SELECTOR,
        link_selector=LINK_SELECTOR,
        date_selectors=DATE_SELECTORS,
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
        raise ValueError("BeritaSatu adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
