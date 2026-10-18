"""Fail-closed pure adapter candidate for Le Vert.

The current sitemap index and robots file are preserved as first-party
evidence.  The relevant post shards and article profile were not fetched in
this bounded review, and no publisher-policy decision exists.  Live entry
points therefore remain closed before network access.
"""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "levert.ma"
    country_code = "MAR"
    is_env = True
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 5, 26)
OPERATIONAL_THROUGH = date(2026, 9, 2)
FETCH_START = date(2026, 5, 26)
ACCEPT_START = date(2026, 5, 26)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "de3111b257ff82405c01668048fabb198c35a8d96f248e7c97459f315862fc16"
SITEMAP_INDEX_EVIDENCE_SHA256 = "9a81bf0c9731f5ad0e97a75a5cb94ff35adf36d64d046aa12c7d9eb8f69a1548"
ROBOTS_EVIDENCE_SHA256 = "9311bab30d8b5d05f5fa5563a251b8c96b8470873bb77c62d3d17c146d7acab7"
ALLOWED_HOSTS = ("levert.ma", "www.levert.ma")
CANONICAL_HOST = "levert.ma"
ROBOTS_URL = "https://levert.ma/robots.txt"
SITEMAP_INDEX_URL = "https://levert.ma/sitemap_index.xml"
CANDIDATE_SITEMAPS = (
    "https://levert.ma/post-sitemap6.xml",
    "https://levert.ma/post-sitemap7.xml",
)
EXPECTED_LANGUAGES = ("fr", "ar")
DENY_PATHS = ("/category/", "/tag/", "/author/", "/feed/", "/wp-admin/")
TITLE_SELECTORS = ("article h1.entry-title", "h1.entry-title", "main h1", "h1")
BODY_SELECTORS = (".entry-content", ".post-content", "article")

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
            "method": "candidate_explicit_recent_post_sitemaps_pending_shard_article_and_policy_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
            "sitemap_index_url": SITEMAP_INDEX_URL,
            "candidate_sitemaps": list(CANDIDATE_SITEMAPS),
            "sitemap_index_evidence_sha256": SITEMAP_INDEX_EVIDENCE_SHA256,
            "robots_evidence_sha256": ROBOTS_EVIDENCE_SHA256,
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError(
        "Le Vert live observation is disabled pending post-shard, article, and publisher-policy review"
    )


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
        deny_paths=DENY_PATHS,
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
    target = common.canonicalize_url(
        url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST,
    )
    if target != url.rstrip("/"):
        raise ValueError("Le Vert adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
