"""Fail-closed pure paginated-sitemap candidate for 7sur7."""
from __future__ import annotations

from datetime import date

from bs4 import BeautifulSoup

from adapters import candidate_common as common


class Config:
    source_name = "7sur7.cd"
    country_code = "COD"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = True
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 12)
OPERATIONAL_THROUGH = date(2026, 8, 12)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "d4430acb6c5746d9c83743f5ac269dc3e4ac3048320eea64ba1b5c1db0d12556"
CURRENT_ROUTE_EVIDENCE_SHA256 = "33eceaf99a0d59a9aa8913985fcab05fd4c76be910c376aa2b6a7a8cd0693a1c"
ROBOTS_EVIDENCE_SHA256 = "773fb8d35bb9a39d35335ee6db8dc5c912d2aacbfb823152d9c61cd647dd902d"
ARTICLE_EVIDENCE_SHA256 = "82ed1231065c34c9729015663279b5a229fe2b4042c1a9271fa5921fa6db3c18"
POLICY_EVIDENCE_SHA256 = "dfb6cd89e204a7da2e3a689e6aff9a6c9d1d53bd75ee877e3416792a2becd0d2"
ALLOWED_HOSTS = ("7sur7.cd", "www.7sur7.cd")
CANONICAL_HOST = "7sur7.cd"
ROBOTS_URL = "https://7sur7.cd/robots.txt"
CANDIDATE_SITEMAPS = (
    "https://7sur7.cd/sitemap.xml?page=37",
    "https://7sur7.cd/sitemap.xml?page=38",
)
EXPECTED_LANGUAGES = ("fr",)
TITLE_SELECTORS = ("main h1", "article h1", "h1", ".page-title")
BODY_SELECTORS = ("article .field--name-body", ".field--name-body", "main article", "article")
DENY_PATHS = ("/sport", "/culture")
EXCLUDED_SECTIONS = {"culture", "sport", "sports"}

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {
        "route_profile_approved": ROUTE_PROFILE_APPROVED,
        "article_profile_approved": ARTICLE_PROFILE_APPROVED,
        "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED,
        "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED,
        "peer_order_approved": PEER_ORDER_APPROVED,
    }


def _require_live_gates(evidence: dict | None = None) -> None:
    if all(_gates().values()):
        return
    if evidence is not None:
        evidence.update(
            {
                "mode": "blocked_prelaunch_no_production_writes",
                "method": "reviewed_exact_paginated_sitemap_pair_and_article_profile_pending_publisher_policy_and_peer_review",
                "accept_start": ACCEPT_START.isoformat(),
                "candidate_sitemaps": CANDIDATE_SITEMAPS,
                "deny_paths": DENY_PATHS,
                **_gates(),
                "coverage_complete": False,
                "first_pass_verified_through": None,
                "strict_verified_through": None,
                "production_writes": False,
            }
        )
    raise RouteProfileUnverifiedError("7sur7 live observation remains fail-closed")


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
    article = common.parse_article_html(
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
    # The publisher places the visible publication time in the page header just
    # above ``article``. Cross-check it explicitly against the article meta tag.
    errors = [item for item in (article.get("parser_error") or "").split(";") if item]
    if errors == ["insufficient_independent_publication_dates"]:
        soup = BeautifulSoup(payload, "html.parser")
        meta = soup.select_one('meta[property="article:published_time"][content]')
        visible = soup.select_one("time[datetime]")
        meta_date = common.parse_datetime(meta.get("content") if meta else None)
        visible_date = common.parse_datetime(visible.get("datetime") if visible else None)
        if meta_date and visible_date and meta_date.timestamp() == visible_date.timestamp():
            article["parser_error"] = None
            article["field_evidence"]["publisher_header_time"] = visible.get("datetime")
            article["field_evidence"]["publication_date_cross_check"] = "meta equals visible header time"
    soup = BeautifulSoup(payload, "html.parser")
    section_node = soup.select_one('meta[property="article:section"][content]')
    section = common.clean(section_node.get("content") if section_node else None)
    normalized_section = section.casefold() if section else None
    errors = [item for item in (article.get("parser_error") or "").split(";") if item]
    if normalized_section in EXCLUDED_SECTIONS:
        errors.append("excluded_publisher_section")
    article["parser_error"] = ";".join(dict.fromkeys(errors)) or None
    article["field_evidence"]["publisher_section"] = section
    article["field_evidence"]["excluded_sections"] = sorted(EXCLUDED_SECTIONS)
    return article


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("7sur7 adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
