"""Production-disabled bounded pure sitemap candidate for Eagle Online Uganda."""
from __future__ import annotations

import html
import os
import unicodedata
from datetime import date

from bs4 import BeautifulSoup

from adapters import candidate_common as common


class Config:
    source_name = "eagle.co.ug"
    country_code = "UGA"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False
    observation_ready = True


PRODUCTION_WRITES = False
OBSERVATION_READY = True
BOUNDED_ROUTE_INVENTORY = True
ROUTE_PROFILE_APPROVED = True
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 10)
OPERATIONAL_THROUGH = date(2026, 8, 10)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "d0aa21f2f9f56f8e74368d0d3ff3b9332fa9c39c4cbcaaf80df6b6edcb057e25"
CURRENT_ROUTE_EVIDENCE_SHA256 = "48ebc2f39c17fcf9c82169efbd84a12ec9464adee1917574e7c433b0aaeed242"
ROBOTS_EVIDENCE_SHA256 = "142aca2b1e8cd0245162141c7bcd702c610a66dc4f799644b304525c982edb42"
ARTICLE_EVIDENCE_SHA256 = "f689928c2d0534b38c97851f97505ea31bd527a1f961dca34f7d1010ed78bb73"
POLICY_EVIDENCE_SHA256 = "6504977be2a922dd72f4d6687d3c9c180fe03187c1d2dfd49f4c61fa9704c490"
ALLOWED_HOSTS = ("eagle.co.ug", "www.eagle.co.ug")
CANONICAL_HOST = "eagle.co.ug"
ROBOTS_URL = "https://eagle.co.ug/robots.txt"
CANDIDATE_SITEMAPS = ("https://eagle.co.ug/post-sitemap25.xml",)
EXPECTED_LANGUAGES = ("en",)
TITLE_SELECTORS = ("main h1", "article h1", "h1", ".tdb-title-text")
BODY_SELECTORS = (".td-post-content", ".tdb-block-inner", "article")
OPINION_SECTIONS = {"opinion", "opinions"}

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _title_key(raw: object) -> str:
    """Normalize presentation-only differences while retaining semantic text."""
    value = unicodedata.normalize("NFKC", html.unescape(str(raw or ""))).casefold()
    return " ".join(
        "".join(" " if ch.isspace() or unicodedata.category(ch).startswith("P") else ch for ch in value).split()
    )


def _reconcile_title(article: dict) -> None:
    visible = common.clean(article.get("title"))
    structured = common.clean(article.get("field_evidence", {}).get("jsonld_headline"))
    matched = bool(visible and structured and _title_key(visible) == _title_key(structured))
    errors = [value for value in (article.get("parser_error") or "").split(";") if value]
    if matched:
        errors = [value for value in errors if value != "visible_vs_jsonld_title_disagreement"]
    article["parser_error"] = ";".join(dict.fromkeys(errors)) or None
    article["field_evidence"]["title_reconciliation"] = {
        "method": "unicode_nfkc_entity_whitespace_case_and_punctuation_only",
        "matched": matched,
        "semantic_edits_allowed": False,
    }


def _gates() -> dict[str, bool]:
    return {
        "route_profile_approved": ROUTE_PROFILE_APPROVED,
        "article_profile_approved": ARTICLE_PROFILE_APPROVED,
        "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED,
        "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED,
        "peer_order_approved": PEER_ORDER_APPROVED,
    }


def _observation_gates() -> dict[str, bool]:
    return {
        "controller_opt_in_or_production_ready": (
            os.environ.get("SCRAPER_BOUNDED_OBSERVATION") == "1"
            or all(_gates().values())
        ),
        "observation_flag_enabled": (
            OBSERVATION_READY is True and Config.observation_ready is True
        ),
        "production_writes_disabled": (
            PRODUCTION_WRITES is False and Config.production_writes is False
        ),
        "bounded_route_inventory": BOUNDED_ROUTE_INVENTORY,
        "bounded_acceptance_window": (
            ACCEPT_START <= FIRST_PASS_TARGET
            and (FIRST_PASS_TARGET - ACCEPT_START).days <= common.MAX_WINDOW_DAYS
        ),
        "article_profile_approved": ARTICLE_PROFILE_APPROVED,
        "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED,
    }


def _require_live_gates(evidence: dict | None = None) -> None:
    production_gates = _gates()
    observation_gates = _observation_gates()
    if Config.observation_ready and all(observation_gates.values()):
        if evidence is not None:
            evidence.update(
                {
                    "admission": "bounded_pure_observation",
                    "observation_ready": True,
                    "launch_ready": False,
                    "promotion_eligible": False,
                    "production_gates_satisfied": all(production_gates.values()),
                    "first_pass_verified_through": None,
                    "strict_verified_through": None,
                    "production_writes": False,
                    **production_gates,
                    **observation_gates,
                }
            )
        return
    if evidence is not None:
        evidence.update(
            {
                "mode": "blocked_prelaunch_no_production_writes",
                "method": "reviewed_single_post_sitemap_and_article_profile_pending_publisher_policy_and_peer_review",
                "accept_start": ACCEPT_START.isoformat(),
                "candidate_sitemaps": CANDIDATE_SITEMAPS,
                **_gates(),
                "coverage_complete": False,
                "first_pass_verified_through": None,
                "strict_verified_through": None,
                "production_writes": False,
            }
        )
    raise RouteProfileUnverifiedError("Eagle Online live observation remains fail-closed")


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
        response_url=response_url,
    )
    soup = BeautifulSoup(payload, "html.parser")
    item = common.article_jsonld(soup) or {}
    sections = item.get("articleSection")
    sections = sections if isinstance(sections, list) else [sections]
    normalized = {common.clean(value).casefold() for value in sections if common.clean(value)}
    article["collection"] = "opinion-articles" if normalized & OPINION_SECTIONS else "articles"
    article["field_evidence"]["publisher_sections"] = sorted(normalized)
    article["field_evidence"]["opinion_sections"] = sorted(OPINION_SECTIONS)
    _reconcile_title(article)
    return article


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Eagle Online adapter refused a noncanonical URL")
    article_request_url = target + "/"
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, article_request_url)
    response = _request(article_request_url, max_bytes=common.MAX_ARTICLE_BYTES)
    if response.url != article_request_url:
        raise common.AccessBlockedError("Eagle Online returned an unexpected article response URL")
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
