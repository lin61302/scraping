"""Production-disabled bounded pure sitemap candidate for Eye Radio."""
from __future__ import annotations

import json
import os
from datetime import date, timezone

from bs4 import BeautifulSoup

from adapters import candidate_common as common


class Config:
    source_name = "eyeradio.org"
    country_code = "SSD"
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
LEGACY_REFERENCE_SHA256 = "5e290f9b082279997ff1143704d195c4fc31633cdde37fa72eb7f9c520d0c3e2"
CURRENT_ROUTE_EVIDENCE_SHA256 = "49669f78ada4abc2bf51ed2dabe3655067f812536d890d4364c9122c80987d9a"
ROBOTS_EVIDENCE_SHA256 = "ba0ed489ab18bd7cfb6e4409b3f8f716185841cd75da6255e0ce5f597c2a034c"
ARTICLE_EVIDENCE_SHA256 = "ff3c321cb855d541bd0678b8f3022a5c904503199ac3fdac554ab1fd4ae9df85"
POLICY_EVIDENCE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ALLOWED_HOSTS = ("eyeradio.org", "www.eyeradio.org")
CANONICAL_HOST = "eyeradio.org"
ROBOTS_URL = "https://eyeradio.org/robots.txt"
CANDIDATE_SITEMAPS = ("https://eyeradio.org/post-sitemap.xml",)
EXPECTED_LANGUAGES = ("en",)
TITLE_SELECTORS = ("main h1", "article h1", "h1", ".entry-title")
BODY_SELECTORS = ("article.single-article .single-content", ".single-content", "article")
EXCLUDED_SECTIONS = {"entertainment", "sports"}

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _gates() -> dict[str, bool]:
    return {"route_profile_approved": ROUTE_PROFILE_APPROVED, "article_profile_approved": ARTICLE_PROFILE_APPROVED, "robots_policy_reviewed": ROBOTS_POLICY_REVIEWED, "publisher_policy_reviewed": PUBLISHER_POLICY_REVIEWED, "peer_order_approved": PEER_ORDER_APPROVED}


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
        evidence.update({"mode": "blocked_prelaunch_no_production_writes", "method": "reviewed_single_sitemap_and_article_profile_pending_publisher_policy_and_peer_review", "accept_start": ACCEPT_START.isoformat(), "candidate_sitemaps": CANDIDATE_SITEMAPS, "excluded_sections": sorted(EXCLUDED_SECTIONS), **_gates(), "coverage_complete": False, "first_pass_verified_through": None, "strict_verified_through": None, "production_writes": False})
    raise RouteProfileUnverifiedError("Eye Radio live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_sitemap_window(source_name=Config.source_name, start=start, end=end, evidence=evidence, robots_url=ROBOTS_URL, sitemap_urls=CANDIDATE_SITEMAPS, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, fetch=_request)


def _blogposting(soup: BeautifulSoup) -> dict:
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        values = value.get("@graph", []) if isinstance(value, dict) else []
        if isinstance(value, dict):
            values = [value, *values]
        for item in values:
            kinds = item.get("@type") if isinstance(item, dict) else None
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if "BlogPosting" in kinds:
                return item
    return {}


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    article = common.parse_article_html(url=url, payload=payload, source_name=Config.source_name, country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES, title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS, response_url=response_url, structured_article_required=False)
    soup = BeautifulSoup(payload, "html.parser")
    item = _blogposting(soup)
    headline = common.clean(item.get("headline") or item.get("name"))
    published = common.parse_datetime(item.get("datePublished"))
    meta = soup.select_one('meta[property="article:published_time"][content]')
    meta_published = common.parse_datetime(meta.get("content") if meta else None)
    language = common.clean(item.get("inLanguage"))
    sections = item.get("articleSection")
    sections = sections if isinstance(sections, list) else [sections]
    normalized_sections = {common.clean(value).casefold() for value in sections if common.clean(value)}
    errors = [value for value in (article.get("parser_error") or "").split(";") if value and value != "insufficient_independent_publication_dates"]
    if not item or headline != common.clean(article.get("title")):
        errors.append("visible_vs_blogposting_headline_disagreement")
    if published is None or meta_published is None or abs((published.astimezone(timezone.utc) - meta_published.astimezone(timezone.utc)).total_seconds()) > 1:
        errors.append("meta_vs_blogposting_publication_date_disagreement")
    if not language or language.split("-", 1)[0].casefold() != "en":
        errors.append("unexpected_blogposting_language")
    if normalized_sections & EXCLUDED_SECTIONS:
        errors.append("excluded_publisher_section")
    article["parser_error"] = ";".join(dict.fromkeys(errors)) or None
    article["field_evidence"].update({"blogposting_headline": headline, "blogposting_date_published": item.get("datePublished"), "blogposting_language": language, "blogposting_sections": sorted(normalized_sections), "excluded_sections": sorted(EXCLUDED_SECTIONS)})
    return article


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Eye Radio adapter refused a noncanonical URL")
    article_request_url = target + "/"
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, article_request_url)
    response = _request(article_request_url, max_bytes=common.MAX_ARTICLE_BYTES)
    if response.url != article_request_url:
        raise common.AccessBlockedError("Eye Radio returned an unexpected article response URL")
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
