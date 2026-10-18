"""Gated pure adapter candidate for Réalités' environment section.

The local record identifies the exact environmental category and a clean
historical update, but it does not preserve current category markup, article
fields, robots rules, or publisher policy.  Live entry points fail before any
request until all four evidence gates are reviewed.
"""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "realites.com.tn"
    country_code = "TUN"
    is_env = True
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2025, 7, 14)
OPERATIONAL_THROUGH = date(2026, 1, 13)
# The 2026-01-13 note says content was updated only through December 2025.
# Start on January 1 rather than promoting the report date to a coverage date.
FETCH_START = date(2026, 1, 1)
ACCEPT_START = date(2026, 1, 1)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "f2ad9c57d2e9cc25409481243f0c8fb9d09bb64206e2428fe991c5aa498db492"
ROUTE_PROFILE_EVIDENCE = "historical_not_current_environment_category_route"
CURRENT_ROUTE_EVIDENCE_SHA256 = "6e130dd311a3fc99b1e6df8447bc2f645a145e16d5d46f52a3ca9493da51d8c1"
ROBOTS_EVIDENCE_SHA256 = "bb45e3dd3a86c65c49b92c42ab53de592e173301f50c084a79d05cbddd78f97e"
ARTICLE_EVIDENCE_SHA256 = (
    "3bc4e25e345a7ac33a83011e9483ca6a5f957fd05d84679d1ee31b2519d62fae",
    "59f358740c5abfc0b87d7f5af0f65a61b62b7a30633467b4ec33cf9cb0047eb3",
)
ALLOWED_HOSTS = ("realites.com.tn", "www.realites.com.tn")
CANONICAL_HOST = "realites.com.tn"
ROBOTS_URL = "https://realites.com.tn/robots.txt"
LISTING_TEMPLATE = "https://realites.com.tn/fr/category/environnement/page/{page}/"
EXPECTED_LANGUAGES = ("fr",)
ITEM_SELECTOR = "article"
LINK_SELECTOR = "h2 a[href], h3 a[href], a.entry-title[href]"
DATE_SELECTORS = (
    'time[itemprop="datePublished"][datetime]', "time[datetime]",
    'meta[itemprop="datePublished"][content]',
)
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
            "method": "candidate_environment_category_profile_pending_current_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_listing_template": LISTING_TEMPLATE,
            "route_profile_evidence": ROUTE_PROFILE_EVIDENCE,
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError(
        "Réalités live observation is disabled pending route, article, robots, and publisher-policy review"
    )


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_category_window(
        source_name=Config.source_name, start=start, end=end, evidence=evidence,
        robots_url=ROBOTS_URL, listing_template=LISTING_TEMPLATE,
        allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST,
        fetch=_request, item_selector=ITEM_SELECTOR, link_selector=LINK_SELECTOR,
        date_selectors=DATE_SELECTORS,
    )


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(
        url=url, payload=payload, source_name=Config.source_name,
        country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS,
        canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES,
        title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS,
        structured_article_required=False,
        response_url=response_url,
    )


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(
        url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST,
    )
    if target != url.rstrip("/"):
        raise ValueError("Réalités adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
