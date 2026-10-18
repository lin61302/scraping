"""Fail-closed pure candidate for the English Setopati archive.

The reviewed homepage and robots file are reachable, and the publisher policy
page is retained.  The route sample has no reviewed pagination across the
2026-06-11 lower boundary.  The reviewed article lacks canonical, structured
article, language, and independent publication-date evidence.  Live entry
points therefore remain closed before network access.
"""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "en.setopati.com"
    country_code = "NPL"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 6, 11)
OPERATIONAL_THROUGH = date(2026, 6, 11)
FETCH_START = date(2026, 6, 11)
ACCEPT_START = date(2026, 6, 11)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "bfb774d00f68010e3d38e121e865c36ad110042c72bda6226fd4b5548d77a644"
CURRENT_ROUTE_EVIDENCE_SHA256 = "cb876e5d71581198046cb4f49008b80eee02b2ed00fdd3395c16c219e8bd9c76"
ROBOTS_EVIDENCE_SHA256 = "ccfb08693328d74cec1568e28b99e8df270bddb7095089f207951541377759ed"
ARTICLE_EVIDENCE_SHA256 = "04d163e392ee7ed92c3714046b0c4a76ed54c5969c54f5511b59130310ecc27f"
POLICY_EVIDENCE_SHA256 = "b316b2e1c59a9a2dd08545e7bfc38bbc778b1313b6574f63891a4f1426e517f2"
ALLOWED_HOSTS = ("en.setopati.com",)
CANONICAL_HOST = "en.setopati.com"
ROBOTS_URL = "https://en.setopati.com/robots.txt"
LISTING_TEMPLATE = "https://en.setopati.com/political?page={page}"
EXPECTED_LANGUAGES = ("en",)
ITEM_SELECTOR = ".items"
LINK_SELECTOR = "a[href]"
DATE_SELECTORS = ("time[datetime]", "meta[itemprop='datePublished'][content]")
TITLE_SELECTORS = ("span.news-big-title", "meta[property='og:title']")
BODY_SELECTORS = (".editor-box", ".article-content")

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
            "method": "candidate_english_category_pending_route_article_and_policy_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_listing_template": LISTING_TEMPLATE,
            "current_route_evidence_sha256": CURRENT_ROUTE_EVIDENCE_SHA256,
            "robots_evidence_sha256": ROBOTS_EVIDENCE_SHA256,
            "article_evidence_sha256": ARTICLE_EVIDENCE_SHA256,
            "policy_evidence_sha256": POLICY_EVIDENCE_SHA256,
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError(
        "English Setopati live observation is disabled pending route, article, and publisher-policy review"
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
        structured_article_required=False,
        response_url=response_url,
    )


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("English Setopati adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
