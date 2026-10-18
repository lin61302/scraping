"""Fail-closed pure candidate for Bolivia.com's Actualidad sitemap."""
from __future__ import annotations

from datetime import date

from adapters import candidate_common as common


class Config:
    source_name = "bolivia.com"
    country_code = "BOL"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 6, 22)
OPERATIONAL_THROUGH = date(2026, 6, 22)
FETCH_START = date(2026, 6, 22)
ACCEPT_START = date(2026, 6, 22)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "b85e102cbb4e0137ec42319027d80e12a566dc083b5e16d53dd743081005c86c"
CURRENT_ROUTE_EVIDENCE_SHA256 = "58e0955fa02f0161822c817d97ebf3c986428bd6468b0840ddd890b49a0c530d"
ROBOTS_EVIDENCE_SHA256 = "7f9fdbdefd5f28127b7e32324a6e94d9e636e3204c4864d7721a8f6f5793c8da"
ARTICLE_EVIDENCE_SHA256 = "9af3806cae0b7e3408df5cef8e393c9476daba2e2f7be7274c38273d9189d6ce"
POLICY_EVIDENCE_SHA256 = "9c3f6a41d37cbbdfd76fe08312ae5bc5007af9f55cf3d66e7bb020d2ad1b87b7"
ALLOWED_HOSTS = ("bolivia.com", "www.bolivia.com")
CANONICAL_HOST = "www.bolivia.com"
ROBOTS_URL = "https://www.bolivia.com/robots.txt"
CANDIDATE_SITEMAPS = ("https://www.bolivia.com/sitemaps/actualidad/sitemap.xml",)
EXPECTED_LANGUAGES = ("es",)
DENY_PATHS = ("/deportes/", "/entretenimiento/", "/horoscopo/", "/futbol/")
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = (".article-body", ".nota-contenido", "article")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError
ArticleProfileUnverifiedError = common.ArticleProfileUnverifiedError
AccessBlockedError = common.AccessBlockedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


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
            "method": "candidate_actualidad_sitemap_pending_route_article_robots_and_policy_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_sitemaps": list(CANDIDATE_SITEMAPS),
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            "current_route_evidence_sha256": CURRENT_ROUTE_EVIDENCE_SHA256,
            "robots_evidence_sha256": ROBOTS_EVIDENCE_SHA256,
            "article_evidence_sha256": ARTICLE_EVIDENCE_SHA256,
            "policy_evidence_sha256": POLICY_EVIDENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError(
        "Bolivia.com live observation is disabled pending current route and policy review"
    )


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return common.discover_sitemap_window(
        source_name=Config.source_name, start=start, end=end, evidence=evidence,
        robots_url=ROBOTS_URL, sitemap_urls=CANDIDATE_SITEMAPS,
        allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST,
        fetch=_request, deny_paths=DENY_PATHS,
    )


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    return common.parse_article_html(
        url=url, payload=payload, source_name=Config.source_name,
        country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS,
        canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES,
        title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS,
        response_url=response_url,
    )


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Bolivia.com adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
