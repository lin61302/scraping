"""Fail-closed pure candidate for Ada Derana's dated archive."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "adaderana.lk"
    country_code = "LKA"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = True
RECORDED_NOTION_BOUNDARY = OPERATIONAL_THROUGH = FETCH_START = ACCEPT_START = date(2026, 7, 13)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "e92223603b38987f05406bd8d6d49ec0ea5624fcada5df2dff1738da113e3765"
CURRENT_ROUTE_EVIDENCE_SHA256 = "14f5b7bc05e68efb9352fd154bfbf4bc88dce42a41d3092d1c1bd300fdc53159"
ROBOTS_EVIDENCE_SHA256 = "00fb4e82f4a66623bcf0a7b04feabe09cc264cf962af053eb64ce85aba3dea9e"
ARTICLE_EVIDENCE_SHA256 = "dbe50ab34f1f0a2949f87a73b09da565cc4f62c1340073760545d58cc5896727"
POLICY_EVIDENCE_SHA256 = "842c6c823fbcf92a53839dc29cec828879da187a10e1ffdc45dc07f441a6fdbd"
ALLOWED_HOSTS = ("adaderana.lk", "www.adaderana.lk")
CANONICAL_HOST = "adaderana.lk"
ROBOTS_URL = "https://adaderana.lk/robots.txt"
LISTING_TEMPLATE = "https://adaderana.lk/archive?page={page}&limit=50&year=2026&month=9&day=30"
EXPECTED_LANGUAGES = ("en",)
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = ("article", ".article-content", ".news-content", "main")

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
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "dated_archive_pending_server_route_article_policy_review", "accept_start": ACCEPT_START.isoformat(), "listing_template": LISTING_TEMPLATE, **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Ada Derana live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_category_window(source_name=Config.source_name, start=start, end=end, evidence=evidence, robots_url=ROBOTS_URL, listing_template=LISTING_TEMPLATE, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, fetch=_request, item_selector="article", link_selector="a[href]", date_selectors=("time[datetime]", "meta[itemprop='datePublished']"), max_pages=120)


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(url=url, payload=payload, source_name=Config.source_name, country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES, title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS, response_url=response_url)


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Ada Derana adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
