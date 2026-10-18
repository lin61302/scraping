"""Fail-closed pure candidate for Guardian Nigeria."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from adapters import candidate_common as common


class Config:
    source_name = "guardian.ng"
    country_code = "NGA"
    is_env = False
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 14)
OPERATIONAL_THROUGH = date(2026, 8, 14)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "93f4e2caf366208b77a15efcef53c7a60620e076847ff759f0866759cfb0cd89"
CURRENT_ROUTE_EVIDENCE_SHA256 = "193f0a531883b8e4d41f0a80d18aa78ab2b2bae14649175a6004b608f1ca66de"
ROBOTS_EVIDENCE_SHA256 = "aa9ab10ecb497ea11017117d68a24fafe41023c2ceaa0188efa69a6cc03b133c"
ARTICLE_EVIDENCE_SHA256 = "eaf2cb238bf4d0df6cb73379244f5dd029e033fc9645de488b922b401f4a15b4"
POLICY_EVIDENCE_SHA256 = "10013e5621541bb78304a19a70d07906998b8d9e4975811f9dc016f577d1d861"
ALLOWED_HOSTS = ("guardian.ng", "www.guardian.ng")
CANONICAL_HOST = "guardian.ng"
ROBOTS_URL = "https://guardian.ng/robots.txt"
SITEMAP_INDEX = "https://guardian.ng/sitemap_index.xml"
REQUIRED_INTERVENING_SITEMAPS = tuple(
    f"https://guardian.ng/post-sitemap{number}.xml" for number in range(2075, 2103)
)
EXPECTED_LANGUAGES = ("en",)
TITLE_SELECTORS = ("main h1", "article h1", "h1")
BODY_SELECTORS = ("article .post-content", "article", ".article-content")

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
                "method": "reviewed_sitemap_index_and_endpoint_samples_pending_all_intervening_shards_policy_peer_review",
                "accept_start": ACCEPT_START.isoformat(),
                "sitemap_index": SITEMAP_INDEX,
                "required_intervening_sitemaps": REQUIRED_INTERVENING_SITEMAPS,
                **_gates(),
                "coverage_complete": False,
                "first_pass_verified_through": None,
                "strict_verified_through": None,
                "production_writes": False,
            }
        )
    raise RouteProfileUnverifiedError("Guardian Nigeria live observation remains fail-closed")


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    if start > end or (end - start).days > common.MAX_WINDOW_DAYS:
        raise ValueError("Guardian Nigeria window is invalid or exceeds 366 inclusive days")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    appearances: dict[str, list[dict]] = {}
    receipts = []
    dates = []
    for route in REQUIRED_INTERVENING_SITEMAPS:
        common.require_allowed(robots, route)
        response = _request(route, max_bytes=common.MAX_SITEMAP_BYTES)
        kind, rows = common.parse_sitemap(response.content)
        if kind != "urlset":
            raise RuntimeError("Guardian Nigeria reviewed shard is not a urlset")
        receipts.append(
            {
                "url": route,
                "status": response.status_code,
                "bytes": len(response.content),
                "sha256": common.sha256(response.content),
                "rows": len(rows),
            }
        )
        for position, row in enumerate(rows, start=1):
            target = common.canonicalize_url(
                row["url"], allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST
            )
            modified = common.parse_datetime(row.get("lastmod"))
            if target is None or modified is None:
                raise RuntimeError("Guardian Nigeria shard has a noncanonical URL or invalid lastmod")
            dates.append(modified.date())
            appearances.setdefault(target, []).append(
                {"sitemap": route, "position": position, "lastmod": row["lastmod"]}
            )
    if min(dates) >= start or max(dates) < end:
        raise RuntimeError("Guardian Nigeria exact shard set does not cross the acceptance window")
    urls = sorted(
        url
        for url, rows in appearances.items()
        if any(
            (common.parse_datetime(row["lastmod"]) or datetime.min).date()
            >= start - timedelta(days=2)
            for row in rows
        )
    )
    evidence.update(
        {
            "mode": "pure_observation_no_production_writes",
            "method": "reviewed_exact_intervening_sitemap_shards_with_article_page_date_validation",
            "sitemap_endpoints": receipts,
            "robots_sha256": common.sha256(robots_response.content),
            "raw_entries": sum(item["rows"] for item in receipts),
            "unique_entries": len(appearances),
            "selected": len(urls),
            "selected_url_set_sha256": common.url_set_sha256(urls),
            "inventory_lower_boundary": min(dates).isoformat(),
            "inventory_upper_boundary": max(dates).isoformat(),
            "sitemap_lastmod_used_as_publication_date": False,
            "article_page_date_is_authoritative": True,
            "coverage_complete": True,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        }
    )
    return urls


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
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Guardian Nigeria adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
