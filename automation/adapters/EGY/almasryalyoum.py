"""Fail-closed pure candidate for Al-Masry Al-Youm monthly news sitemaps."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "almasryalyoum.com"
    country_code = "EGY"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = True
RECORDED_NOTION_BOUNDARY = OPERATIONAL_THROUGH = FETCH_START = ACCEPT_START = date(2026, 9, 2)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "9fa1533ca9c43ea1dbdaf7ccc025c3f66bb14f995d890764e18776fca33074cc"
CURRENT_ROUTE_EVIDENCE_SHA256 = "d69191af11c9e92018aed136e80cf6cebd46f0913583141e4b162e0ab606b689"
ROBOTS_EVIDENCE_SHA256 = "e9d523bf032867d26add789c698ae037042be24db9eb5133dd08e6d5b353e9c9"
ARTICLE_EVIDENCE_SHA256 = "676321adddc07e37cd11ae44d3f8d3a460412ed53df1a32513fa50f0877879df"
POLICY_EVIDENCE_SHA256 = "b94f087a95855b9aef574f678e28dfe45d6b113cb273f9337b643f92793a035f"
ALLOWED_HOSTS = ("almasryalyoum.com", "www.almasryalyoum.com")
CANONICAL_HOST = "www.almasryalyoum.com"
ROBOTS_URL = "https://www.almasryalyoum.com/robots.txt"
CANDIDATE_SITEMAPS = (
    "https://www.almasryalyoum.com/sitemapxmls/news/news-2026-7.xml",
    "https://www.almasryalyoum.com/sitemapxmls/news/news-2026-8.xml",
    "https://www.almasryalyoum.com/sitemapxmls/news/news-2026-9.xml",
)
EXPECTED_LANGUAGES = ("ar",)
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = ("article", ".article-content", ".news-details", "main")

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
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "monthly_news_sitemaps_pending_bounded_route_article_policy_review", "accept_start": ACCEPT_START.isoformat(), "candidate_sitemaps": list(CANDIDATE_SITEMAPS), **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Al-Masry Al-Youm live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_sitemap_window(source_name=Config.source_name, start=start, end=end, evidence=evidence, robots_url=ROBOTS_URL, sitemap_urls=CANDIDATE_SITEMAPS, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, fetch=_request, deny_paths=("/avideo/", "/album/", "/caricatures/"))


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(url=url, payload=payload, source_name=Config.source_name, country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES, title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS, response_url=response_url)


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, deny_paths=("/avideo/", "/album/", "/caricatures/"))
    if target != url.rstrip("/"):
        raise ValueError("Al-Masry Al-Youm adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
