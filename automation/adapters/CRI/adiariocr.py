"""Fail-closed pure adapter candidate for A Diario CR's Ambiente section.

Current first-party evidence proves that the category is live and robots does
not disallow it.  The single reviewed listing page does not cross the required
2026-05-20 lower boundary, the article probe selected an invalid relative-link
alias, and no publisher-policy decision exists.  Live entry points therefore
remain closed before network access.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters import candidate_common as common


class Config:
    source_name = "adiariocr.com"
    country_code = "CRI"
    is_env = True
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = False
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 5, 20)
OPERATIONAL_THROUGH = date(2026, 5, 20)
FETCH_START = date(2026, 5, 20)
ACCEPT_START = date(2026, 5, 20)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "b6ff2c8e870d8d2255fecd409c0c012decf43347cdb94e50cb969b2c311d10cc"
CURRENT_ROUTE_EVIDENCE_SHA256 = "b060611eac8c454c70745ac1ae0bc9dde9e5f7e16a0387f8ad7ae6778f4286be"
ROBOTS_EVIDENCE_SHA256 = "1f9102b80fc7a9b0b0574aeb4c483da5172a41f87421c8350acd487d963387b1"
FAILED_ARTICLE_PROBE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ALLOWED_HOSTS = ("adiariocr.com", "www.adiariocr.com")
CANONICAL_HOST = "adiariocr.com"
ROBOTS_URL = "https://adiariocr.com/robots.txt"
LISTING_ROOT = "https://adiariocr.com/ambiente/"
LISTING_TEMPLATE = "https://adiariocr.com/ambiente/page/{page}/"
EXPECTED_LANGUAGES = ("es",)
ITEM_SELECTOR = ".post"
LINK_SELECTOR = ".entry-title a[href]"
TITLE_SELECTORS = ("article h1.entry-title", "h1.entry-title", "main h1", "h1")
BODY_SELECTORS = (".entry-content", ".post-content", "article")
MAX_PAGES = 120

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError
ArticleProfileUnverifiedError = common.ArticleProfileUnverifiedError
AccessBlockedError = common.AccessBlockedError


_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_SPANISH_DATE = re.compile(r"\b(\d{1,2})\s+de\s+([a-záéíóúüñ]+)\s+de\s+(\d{4})\b", re.I)


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
            "method": "candidate_environment_category_pending_boundary_article_and_policy_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
            "candidate_listing_root": LISTING_ROOT,
            "candidate_listing_template": LISTING_TEMPLATE,
            "current_route_evidence_sha256": CURRENT_ROUTE_EVIDENCE_SHA256,
            "robots_evidence_sha256": ROBOTS_EVIDENCE_SHA256,
            "failed_article_probe_sha256": FAILED_ARTICLE_PROBE_SHA256,
            "legacy_reference_sha256": LEGACY_REFERENCE_SHA256,
            **gates,
            "coverage_complete": False,
            "first_pass_verified_through": None,
            "strict_verified_through": None,
            "production_writes": False,
        })
    raise RouteProfileUnverifiedError(
        "A Diario CR live observation is disabled pending route-boundary, article, and publisher-policy review"
    )


def _fold(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(ch)
    )


def _parse_listing_date(value: str) -> date | None:
    match = _SPANISH_DATE.search(value or "")
    if not match:
        return None
    month = _MONTHS.get(_fold(match.group(2)))
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _listing_url(page: int) -> str:
    return LISTING_ROOT if page == 1 else LISTING_TEMPLATE.format(page=page)


def _parse_listing(payload: bytes) -> list[tuple[str, date]]:
    soup = BeautifulSoup(payload, "html.parser")
    rows = []
    for item in soup.select(ITEM_SELECTOR):
        link = item.select_one(LINK_SELECTOR)
        normalized = common.canonicalize_url(
            link.get("href") if link else None,
            allowed_hosts=ALLOWED_HOSTS,
            canonical_host=CANONICAL_HOST,
        )
        published = _parse_listing_date(item.get_text(" ", strip=True))
        if normalized is None or published is None:
            raise RuntimeError("A Diario CR category row lacks a canonical URL or Spanish publication date")
        rows.append((normalized, published))
    if not rows:
        raise RuntimeError("A Diario CR category page contains no reviewed rows")
    return rows


def _discover_category_window(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > common.MAX_WINDOW_DAYS:
        raise ValueError("A Diario CR window is invalid or exceeds 366 inclusive days")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    selected: dict[str, date] = {}
    pages = []
    newest = None
    previous_oldest = None
    lower_boundary_reached = False
    for page in range(1, MAX_PAGES + 1):
        url = _listing_url(page)
        common.require_allowed(robots, url)
        response = _request(url, max_bytes=common.MAX_LISTING_BYTES)
        rows = _parse_listing(response.content)
        dates = [published for _, published in rows]
        if dates != sorted(dates, reverse=True):
            raise RuntimeError("A Diario CR category page is not newest-first")
        if previous_oldest is not None and max(dates) > previous_oldest:
            raise RuntimeError("A Diario CR pagination is not monotonically newest-first")
        previous_oldest = min(dates)
        newest = max(dates) if newest is None else max(newest, max(dates))
        pages.append({
            "page": page,
            "url": url,
            "status": response.status_code,
            "bytes": len(response.content),
            "sha256": common.sha256(response.content),
            "rows": len(rows),
            "newest": max(dates).isoformat(),
            "oldest": min(dates).isoformat(),
        })
        for article_url, published in rows:
            if published >= start - timedelta(days=2):
                selected[article_url] = published
        if min(dates) < start:
            lower_boundary_reached = True
            break
    if not lower_boundary_reached:
        raise RuntimeError("A Diario CR category did not cross the lower boundary within the page cap")
    if newest is None or newest < end:
        raise RuntimeError("A Diario CR category did not reach the target end")
    urls = sorted(selected)
    if not urls:
        raise RuntimeError("A Diario CR category selected no URLs")
    evidence.update({
        "mode": "pure_observation_no_production_writes",
        "method": "reviewed_newest_first_environment_category_with_spanish_dates",
        "source": Config.source_name,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "pages": pages,
        "robots_url": ROBOTS_URL,
        "robots_sha256": common.sha256(robots_response.content),
        "selected": len(urls),
        "selected_url_set_sha256": common.url_set_sha256(urls),
        "listing_date_used_as_publication_date": False,
        "article_page_date_is_authoritative": True,
        "out_of_acceptance_window_rows_are_audit_only": True,
        "coverage_complete": True,
        "first_pass_verified_through": None,
        "strict_verified_through": None,
        "production_writes": False,
    })
    return urls


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    evidence.update({"requested_start": start.isoformat(), "requested_end": end.isoformat()})
    _require_live_gates(evidence)
    return _discover_category_window(start, end, evidence)


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
        raise ValueError("A Diario CR adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
