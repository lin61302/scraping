"""Production-disabled bounded pure candidate for Newsroom Panama."""
from __future__ import annotations

import hashlib
import html
import os
import re
import unicodedata
from datetime import date

from bs4 import BeautifulSoup

from adapters import candidate_common as common


class Config:
    source_name = "newsroompanama.com"
    country_code = "PAN"
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
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 7, 21)
OPERATIONAL_THROUGH = date(2026, 7, 21)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "d7cb70c4502efe1d4b5cbdd7e5873262e89357d747cb0cc988afb49fbf3993ee"
CURRENT_ROUTE_EVIDENCE_SHA256 = "318c1f3b6e90db59e8b14c5e655d17f7136e17875813ace2ca6ff5e43fad05cf"
ROBOTS_EVIDENCE_SHA256 = "1ec16f91fa5fbfaa1cdb68ad33cb5dbb277e223ad1872d249a035e1f64520860"
ARTICLE_EVIDENCE_SHA256 = "bfc35b270e8548ca71d1e31e23798fb1e8ccac068554f995f628e23751bf1e89"
POLICY_EVIDENCE_SHA256 = "72db6a786eb89d5eb04a248fc072f9ea41e218efe0836d2a56dbafff844631c2"
ALLOWED_HOSTS = ("newsroompanama.com", "www.newsroompanama.com")
CANONICAL_HOST = "newsroompanama.com"
ROBOTS_URL = "https://newsroompanama.com/robots.txt"
CANDIDATE_SITEMAPS = (
    "https://newsroompanama.com/post-sitemap30.xml",
    "https://newsroompanama.com/post-sitemap31.xml",
)
EXPECTED_LANGUAGES = ("en",)
TITLE_SELECTORS = ("main h1", "article h1", "h1")
BODY_SELECTORS = (
    "main article .entry-content",
    "article .entry-content",
    ".entry-content",
    ".post-content",
    ".td-post-content",
    "[itemprop='articleBody']",
    "main article",
    "article",
)

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError
AccessBlockedError = common.AccessBlockedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _title_key(raw: object) -> str:
    """Normalize only presentation differences; retain every letter and digit."""
    value = unicodedata.normalize("NFKC", html.unescape(str(raw or ""))).casefold()
    return " ".join(
        "".join(" " if ch.isspace() or unicodedata.category(ch).startswith("P") else ch for ch in value).split()
    )


def _error_list(article: dict) -> list[str]:
    return [value for value in (article.get("parser_error") or "").split(";") if value]


def _set_errors(article: dict, errors: list[str]) -> None:
    article["parser_error"] = ";".join(dict.fromkeys(errors)) or None


def _reconcile_title(article: dict) -> None:
    visible = common.clean(article.get("title"))
    structured = common.clean(article.get("field_evidence", {}).get("jsonld_headline"))
    matched = bool(visible and structured and _title_key(visible) == _title_key(structured))
    errors = _error_list(article)
    if matched:
        errors = [value for value in errors if value != "visible_vs_jsonld_title_disagreement"]
    _set_errors(article, errors)
    article["field_evidence"]["title_reconciliation"] = {
        "method": "unicode_nfkc_entity_whitespace_case_and_punctuation_only",
        "matched": matched,
        "semantic_edits_allowed": False,
    }


def _body_text(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return common.clean(BeautifulSoup(html.unescape(raw), "html.parser").get_text(" ", strip=True)) or ""


def _body_key(raw: object) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(str(raw or ""))).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).split())


def _maybe_use_structured_body(article: dict, soup: BeautifulSoup) -> None:
    """Use JSON-LD only when it contains a substantive visible body fragment."""
    item = common.article_jsonld(soup) or {}
    visible = common.clean(article.get("maintext")) or ""
    structured = _body_text(item.get("articleBody"))
    visible_key = _body_key(visible)
    structured_key = _body_key(structured)
    visible_words = set(visible_key.split())
    structured_words = set(structured_key.split())
    containment = (
        len(visible_words & structured_words) / len(visible_words)
        if visible_words else None
    )
    exact_containment = bool(
        len(visible) >= 40
        and visible_key
        and structured_key
        and visible_key in structured_key
    )
    eligible = bool(
        len(structured) >= 120
        and exact_containment
        and containment is not None
        and containment >= 0.95
    )
    applied = bool(len(visible) < 120 and eligible)
    evidence = {
        "considered": len(visible) < 120,
        "applied": applied,
        "minimum_structured_characters": 120,
        "visible_characters": len(visible),
        "structured_characters": len(structured),
        "visible_word_containment": containment,
        "normalized_visible_contained": exact_containment,
        "structured_body_sha256": hashlib.sha256(structured.encode("utf-8")).hexdigest() if structured else None,
    }
    if applied:
        article["maintext"] = structured
        article["field_sources"]["maintext"] = "jsonld.articleBody_strict_visible_containment_fallback"
        errors = [
            value for value in _error_list(article)
            if value not in {"insufficient_visible_maintext", "visible_vs_structured_body_disagreement"}
        ]
        _set_errors(article, errors)
    article["field_evidence"]["structured_body_fallback"] = evidence


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
        evidence.update({
            "mode": "blocked_prelaunch_no_production_writes",
            "method": "publisher_inventory_pending_policy_and_peer_order_review",
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": CANDIDATE_SITEMAPS,
            **_gates(),
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("Newsroom Panama live observation remains fail-closed")


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
    _reconcile_title(article)
    _maybe_use_structured_body(article, soup)
    return article


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Newsroom Panama adapter refused a noncanonical URL")
    article_request_url = target + "/"
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, article_request_url)
    response = _request(article_request_url, max_bytes=common.MAX_ARTICLE_BYTES)
    if response.url != article_request_url:
        raise common.AccessBlockedError("Newsroom Panama returned an unexpected article response URL")
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
