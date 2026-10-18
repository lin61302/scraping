"""Fail-closed pure candidate for El Universo daily news sitemaps."""
from __future__ import annotations

from datetime import date, timedelta
from xml.etree import ElementTree

from adapters import candidate_common as common


class Config:
    source_name = "eluniverso.com"
    country_code = "ECU"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 6, 29)
OPERATIONAL_THROUGH = date(2026, 6, 29)
FETCH_START = ACCEPT_START = date(2026, 6, 29)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "fbe7a31d37a3e825bf10b806635d7061fc9e63a724ae4dbe266ad89ae9daee20"
CURRENT_ROUTE_EVIDENCE_SHA256 = "b3d50b9682524ca6a2262d35ca7ac82aa4ec388b0bf60424252f8f704f5336e0"
ROBOTS_EVIDENCE_SHA256 = "2d08636bc5f9eb873eb05033adf50ccef7ac40e90162b1ccfad6444d0f363270"
ARTICLE_EVIDENCE_SHA256 = "618e3e1d2ec5bdd8e94ccd7a3963d9c17c967a06e8abe07bc92dd1ece8aeedf6"
POLICY_EVIDENCE_SHA256 = "c8e47f9043ec6255d7b7734e6ffa1fb50cfe8a91f64ed6908a518a24a3d365b7"
ALLOWED_HOSTS = ("eluniverso.com", "www.eluniverso.com")
CANONICAL_HOST = "www.eluniverso.com"
ROBOTS_URL = "https://www.eluniverso.com/robots.txt"
DAILY_SITEMAP_TEMPLATE = "https://www.eluniverso.com/sitemaps/{day}/noticias.xml/?outputType=xml"
EXPECTED_LANGUAGES = ("es",)
DENY_PATHS = ("/deportes/", "/larevista/", "/entretenimiento/")
TITLE_SELECTORS = ("article h1", "main h1", "h1")
BODY_SELECTORS = (".article-body", ".article-content", "article")

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError
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


def _daily_sitemap_urls(start: date, end: date) -> list[str]:
    if start > end or (end - start).days > common.MAX_WINDOW_DAYS:
        raise ValueError("El Universo window is invalid or exceeds 366 inclusive days")
    count = (end - start).days + 1
    if count > 120:
        raise ValueError("El Universo observation exceeds 120 daily endpoints")
    return [DAILY_SITEMAP_TEMPLATE.format(day=(start + timedelta(days=i)).isoformat()) for i in range(count)]


def _require_live_gates(evidence: dict | None = None) -> None:
    gates = _gates()
    if all(gates.values()):
        return
    if evidence is not None:
        evidence.update({
            "mode": "blocked_prelaunch_no_production_writes",
            "method": "daily_news_sitemaps_pending_full_route_and_publisher_policy_review",
            "candidate_sitemap_count": len(_daily_sitemap_urls(FETCH_START, FIRST_PASS_TARGET)),
            "accept_start": ACCEPT_START.isoformat(),
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            "current_route_evidence_sha256": CURRENT_ROUTE_EVIDENCE_SHA256,
            **gates, "coverage_complete": False,
            "first_pass_verified_through": None, "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError("El Universo live observation remains fail-closed")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _discover_daily_sitemaps(start: date, end: date, evidence: dict) -> list[str]:
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    selected: dict[str, date] = {}
    receipts = []
    for endpoint in _daily_sitemap_urls(start, end):
        common.require_allowed(robots, endpoint)
        response = _request(endpoint, max_bytes=common.MAX_SITEMAP_BYTES)
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise RuntimeError("El Universo returned malformed daily sitemap") from exc
        if _local_name(root.tag) != "urlset":
            raise RuntimeError("El Universo daily endpoint is not a urlset")
        endpoint_day = date.fromisoformat(endpoint.split("/sitemaps/", 1)[1].split("/", 1)[0])
        accepted = 0
        for node in root:
            values = {}
            for child in node.iter():
                name = _local_name(child.tag)
                if name in {"loc", "publication_date"} and child.text:
                    values[name] = child.text.strip()
            normalized = common.canonicalize_url(
                values.get("loc"), allowed_hosts=ALLOWED_HOSTS,
                canonical_host=CANONICAL_HOST, deny_paths=DENY_PATHS,
            )
            published = common.parse_datetime(values.get("publication_date"))
            if normalized is None:
                continue
            if published is None or published.date() != endpoint_day:
                raise RuntimeError("El Universo row lacks matching news publication date")
            selected[normalized] = endpoint_day
            accepted += 1
        if not accepted:
            raise RuntimeError("El Universo daily endpoint has no accepted news rows")
        receipts.append({"url": endpoint, "sha256": common.sha256(response.content), "accepted_rows": accepted})
    urls = sorted(selected)
    evidence.update({
        "mode": "pure_observation_no_production_writes",
        "method": "reviewed_daily_news_sitemaps_with_article_page_date_validation",
        "source": Config.source_name, "requested_start": start.isoformat(),
        "requested_end": end.isoformat(), "sitemap_endpoints": receipts,
        "robots_sha256": common.sha256(robots_response.content),
        "selected": len(urls), "selected_url_set_sha256": common.url_set_sha256(urls),
        "news_publication_date_used_for_inventory_membership_only": True,
        "article_page_date_is_authoritative": True,
        "out_of_acceptance_window_rows_are_audit_only": True,
        "coverage_complete": True, "first_pass_verified_through": None,
        "strict_verified_through": None, "production_writes": False,
    })
    return urls


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return _discover_daily_sitemaps(start, end, evidence)


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
        raise ValueError("El Universo adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
