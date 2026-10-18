"""Fail-closed pure candidate for 24.kg's publisher sitemap inventory."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "24.kg"
    country_code = "KGZ"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = OPERATIONAL_THROUGH = FETCH_START = ACCEPT_START = date(2026, 6, 30)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "a6c5cf905f7436a572a52970ed16e9b0c1a8e63d288beaa5b767ad924f8511fe"
CURRENT_ROUTE_EVIDENCE_SHA256 = "697eabaecd76ea53e698cb4e62eca532ef9b13ccb7921f262f9ca89556f81f28"
ROBOTS_EVIDENCE_SHA256 = "04631e9f49627c38d9531672074e54283341d0210c26cbb42217c828283f9a11"
ARTICLE_EVIDENCE_SHA256 = "d05f95035a6a602b99d6b2f157a12952a65cfaef4d70f9e287ef8ebda98374cf"
POLICY_EVIDENCE_SHA256 = "72680028600d99e94bd11300812a0c39f961b8de51ef5dc595dc8e4604265dd5"
ALLOWED_HOSTS = ("24.kg", "www.24.kg")
CANONICAL_HOST = "24.kg"
ROBOTS_URL = "https://24.kg/robots.txt"
CANDIDATE_SITEMAPS = ("https://24.kg/sitemaps/after_2022/sitemap_rus_.xml",)
EXPECTED_LANGUAGES = ("ru", "ky")
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = ("article", "main article", ".article-content")

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
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "publisher_sitemap_pending_current_route_article_policy_review", "accept_start": ACCEPT_START.isoformat(), "candidate_sitemaps": list(CANDIDATE_SITEMAPS), **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("24.kg live observation remains fail-closed")


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
        raise ValueError("24.kg adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
