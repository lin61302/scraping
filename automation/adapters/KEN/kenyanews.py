"""Fail-closed pure candidate for Kenya News Agency's environment category."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "kenyanews.go.ke"
    country_code = "KEN"
    is_env = True
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = True
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 8)
OPERATIONAL_THROUGH = date(2026, 7, 12)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "4aad478d59877bf64c8fddf2bd16f345f71d8f16ffe24c061652c42ada8534c2"
CURRENT_ROUTE_EVIDENCE_SHA256 = "20da14b89e91796c0c2835da8d8f6358ae56ae9d1d13ef7fb3691771b2532264"
ROBOTS_EVIDENCE_SHA256 = "9311bab30d8b5d05f5fa5563a251b8c96b8470873bb77c62d3d17c146d7acab7"
ARTICLE_EVIDENCE_SHA256 = "14d3befbe4f670cbe04d1a6232a6bedf840247f0eecd29c3cbfdd8ba53881c93"
POLICY_EVIDENCE_SHA256 = "0a101fc92c18f4aad3be516388e22da46bba11cc71cbebbff724e394ea28ec0a"
ALLOWED_HOSTS = ("kenyanews.go.ke", "www.kenyanews.go.ke")
CANONICAL_HOST = "www.kenyanews.go.ke"
ROBOTS_URL = "https://www.kenyanews.go.ke/robots.txt"
LISTING_TEMPLATE = "https://www.kenyanews.go.ke/category/environment/page/{page}/"
EXPECTED_LANGUAGES = ("en",)
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
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "environment_category_pending_complete_pagination_article_policy_review", "accept_start": ACCEPT_START.isoformat(), "listing_template": LISTING_TEMPLATE, **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Kenya News Agency live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_category_window(source_name=Config.source_name, start=start, end=end, evidence=evidence, robots_url=ROBOTS_URL, listing_template=LISTING_TEMPLATE, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, fetch=_request, item_selector="article", link_selector="h2 a[href], h3 a[href], a[href]", date_selectors=("time.entry-date[datetime]", "time[datetime]"), max_pages=80)


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(url=url, payload=payload, source_name=Config.source_name, country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES, title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS, response_url=response_url)


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Kenya News Agency adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
