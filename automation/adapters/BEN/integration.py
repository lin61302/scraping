"""Fail-closed pure environmental-category candidate for L'Intégration."""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters import candidate_common as common


class Config:
    source_name = "l-integration.com"
    country_code = "BEN"
    is_env = True
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = True
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
PEER_ORDER_APPROVED = False
RECORDED_NOTION_BOUNDARY = FETCH_START = ACCEPT_START = date(2026, 8, 18)
OPERATIONAL_THROUGH = date(2026, 8, 18)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "43c6af2ed07571e86d677a5cf8f4bf9a03bebc2e4c1279c226fab09af348f369"
CURRENT_ROUTE_EVIDENCE_SHA256 = "f0d73feb5674f061a0587ed2b75eec207d4d7ff98b5063062f27844dd2e32b0d"
ROBOTS_EVIDENCE_SHA256 = "3251ec319fb45f6c55c969f7f31bb4beb51e9b1b6f4fbdce651b14b6e37bc000"
ARTICLE_EVIDENCE_SHA256 = "ed31742ba71aecacd0d9624aefdff1022724cf5c464f049a2f60f7b0875f7036"
POLICY_EVIDENCE_SHA256 = "479f12c23786870c544c8ae45975bb1e583a73b1083603c0665757fa3493ea30"
ALLOWED_HOSTS = ("l-integration.com", "www.l-integration.com")
CANONICAL_HOST = "l-integration.com"
ROBOTS_URL = "https://l-integration.com/robots.txt"
LISTING_TEMPLATE = "https://l-integration.com/category/environnement/page/{page}/"
EXPECTED_LANGUAGES = ("fr",)
TITLE_SELECTORS = ("main h1", "article h1", "h1", ".entry-title")
BODY_SELECTORS = ("article .entry-content", ".entry-content", "article")
MONTHS = {
    "janvier": 1,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
}

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
                "method": "reviewed_environment_category_pending_article_metadata_policy_peer_review",
                "accept_start": ACCEPT_START.isoformat(),
                "listing_template": LISTING_TEMPLATE,
                **_gates(),
                "coverage_complete": False,
                "first_pass_verified_through": None,
                "strict_verified_through": None,
                "production_writes": False,
            }
        )
    raise RouteProfileUnverifiedError("L'Intégration live observation remains fail-closed")


def _parse_french_date(text: str) -> date | None:
    match = re.search(r"\b(\d{1,2})\s+([a-zéûôîàèùç]+)\s+(20\d{2})\b", text.casefold())
    if not match or match.group(2) not in MONTHS:
        return None
    return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))


def _parse_listing_html(payload: bytes) -> list[dict]:
    soup = BeautifulSoup(payload, "html.parser")
    rows = []
    for item in soup.select("article.item-list"):
        link = item.select_one("a[href]")
        target = common.canonicalize_url(
            urljoin("https://l-integration.com/", link.get("href")) if link else None,
            allowed_hosts=ALLOWED_HOSTS,
            canonical_host=CANONICAL_HOST,
        )
        published = _parse_french_date(item.get_text(" ", strip=True))
        if target is None or published is None:
            raise RuntimeError("L'Intégration category row lacks a canonical URL or French publication date")
        rows.append({"url": target, "published": published})
    if not rows:
        raise RuntimeError("L'Intégration category contained no reviewed article rows")
    if [row["published"] for row in rows] != sorted(
        (row["published"] for row in rows), reverse=True
    ):
        raise RuntimeError("L'Intégration category is not newest-first")
    return rows


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    if start > end or (end - start).days > common.MAX_WINDOW_DAYS:
        raise ValueError("L'Intégration window is invalid or exceeds 366 inclusive days")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    selected: set[str] = set()
    receipts = []
    lower_crossed = False
    newest: date | None = None
    for page in range(1, 41):
        route = LISTING_TEMPLATE.format(page=page)
        common.require_allowed(robots, route)
        response = _request(route, max_bytes=common.MAX_LISTING_BYTES)
        rows = _parse_listing_html(response.content)
        dates = [row["published"] for row in rows]
        newest = max(dates) if newest is None else max(newest, max(dates))
        receipts.append(
            {
                "page": page,
                "url": route,
                "status": response.status_code,
                "bytes": len(response.content),
                "sha256": common.sha256(response.content),
                "rows": len(rows),
                "newest": max(dates).isoformat(),
                "oldest": min(dates).isoformat(),
            }
        )
        selected.update(row["url"] for row in rows if row["published"] >= start - timedelta(days=2))
        if min(dates) < start:
            lower_crossed = True
            break
    if not lower_crossed or newest is None or newest < end:
        raise RuntimeError("L'Intégration category does not prove the acceptance window")
    urls = sorted(selected)
    evidence.update(
        {
            "mode": "pure_observation_no_production_writes",
            "method": "reviewed_newest_first_environment_category_with_article_page_date_validation",
            "pages": receipts,
            "robots_sha256": common.sha256(robots_response.content),
            "selected": len(urls),
            "selected_url_set_sha256": common.url_set_sha256(urls),
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
        structured_article_required=False,
    )


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("L'Intégration adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
