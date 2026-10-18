"""Fail-closed pure candidate for OOS Belarus monthly archives.

The historical method used monthly archives.  Live entry points stay closed
until the current archive pagination, article profile, robots file, and
publisher policy have all been reviewed.  This module has no production write
surface.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters import candidate_common as common


class Config:
    source_name = "oos.by"
    country_code = "BLR"
    is_env = True
    access_block_circuit_threshold = 1
    production_writes = False
    launch_ready = False


PRODUCTION_WRITES = False
ROUTE_PROFILE_APPROVED = False
ARTICLE_PROFILE_APPROVED = True
ROBOTS_POLICY_REVIEWED = True
PUBLISHER_POLICY_REVIEWED = False
RECORDED_NOTION_BOUNDARY = date(2026, 6, 25)
OPERATIONAL_THROUGH = date(2026, 6, 25)
FETCH_START = date(2026, 6, 1)
ACCEPT_START = date(2026, 6, 25)
FIRST_PASS_TARGET = date(2026, 9, 30)
LEGACY_REFERENCE_SHA256 = "2f89d694c17bef0d5fb1b6aaf1c9bc3ef129e3e5fe291f06b7d2f2a191a6485e"
CURRENT_ROUTE_EVIDENCE_SHA256 = "6182019a4c5471f7cf8f0c12471f3a6bbd5cc591fff6f7b4d46ea4f3e15e493d"
ROBOTS_EVIDENCE_SHA256 = "4641a122ef79f01c65e491266e4c28f1ae0f30c5ef88686cbf75d5baf7319f70"
ARTICLE_EVIDENCE_SHA256 = "ef3deed8a963853986912ebce670a96fa8cb26cfbcda429f4a19402c20a0d9a8"
POLICY_EVIDENCE_SHA256 = "c4ff778f3dace3ac2b021f2dc7ba8518639f25f80110ab4005cba140549a6478"
ALLOWED_HOSTS = ("oos.by", "www.oos.by")
CANONICAL_HOST = "www.oos.by"
ROBOTS_URL = "https://www.oos.by/robots.txt"
ARCHIVE_ROOT = "https://www.oos.by/{year:04d}/{month:02d}/"
ARCHIVE_PAGE = "https://www.oos.by/{year:04d}/{month:02d}/page/{page}/"
EXPECTED_LANGUAGES = ("en", "ru")
ITEM_SELECTOR = ".e-loop-item, article"
LINK_SELECTOR = ".elementor-heading-title a[href], h2.entry-title a[href], h3.entry-title a[href], .entry-title a[href]"
DATE_SELECTORS = (
    ".elementor-post-info__item--type-date", "time.entry-date[datetime]", "time[datetime]",
)
TITLE_SELECTORS = ("article h1.entry-title", "h1.entry-title", "main h1", "h1")
BODY_SELECTORS = (
    ".elementor-widget-theme-post-content", ".entry-content", ".post-content", "article",
)
# Empty until first-party evidence proves the exact finite page count for each
# required month.  Tests may monkeypatch this immutable review input.
REVIEWED_MONTH_PAGE_COUNTS: dict[str, int] = {}

_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_RU_DATE = re.compile(r"\b(\d{1,2})\s+([а-яё]+),?\s+(20\d{2})\b", re.I)

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
            "method": "candidate_monthly_archive_pending_pagination_article_robots_and_policy_review",
            "requested_start": evidence.get("requested_start"),
            "requested_end": evidence.get("requested_end"),
            "fetch_overlap_start": FETCH_START.isoformat(),
            "accept_start": ACCEPT_START.isoformat(),
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
        "OOS Belarus live observation is disabled pending archive pagination and profile review"
    )


def _month_keys(start: date, end: date) -> list[str]:
    if start > end or (end - start).days > common.MAX_WINDOW_DAYS:
        raise ValueError("OOS Belarus window is invalid or exceeds 366 inclusive days")
    cursor = date(start.year, start.month, 1)
    finish = date(end.year, end.month, 1)
    keys = []
    while cursor <= finish:
        keys.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return keys


def _archive_url(month: str, page: int) -> str:
    year, raw_month = map(int, month.split("-"))
    if page == 1:
        return ARCHIVE_ROOT.format(year=year, month=raw_month)
    return ARCHIVE_PAGE.format(year=year, month=raw_month, page=page)


def _archive_date(raw: object) -> date | None:
    parsed = common.parse_datetime(raw)
    if parsed is not None:
        return parsed.date()
    value = unicodedata.normalize("NFC", str(raw or "").strip().casefold())
    match = _RU_DATE.search(value)
    if not match or match.group(2) not in _RU_MONTHS:
        return None
    try:
        return date(int(match.group(3)), _RU_MONTHS[match.group(2)], int(match.group(1)))
    except ValueError:
        return None


def _parse_archive(payload: bytes) -> list[tuple[str, date]]:
    soup = BeautifulSoup(payload, "html.parser")
    rows = []
    for item in soup.select(ITEM_SELECTOR):
        link = item.select_one(LINK_SELECTOR)
        normalized = common.canonicalize_url(
            link.get("href") if link else None,
            allowed_hosts=ALLOWED_HOSTS,
            canonical_host=CANONICAL_HOST,
        )
        raw_date = None
        for selector in DATE_SELECTORS:
            node = item.select_one(selector)
            if node:
                raw_date = node.get("datetime") or node.get("content") or node.get_text(" ", strip=True)
                if raw_date:
                    break
        published = _archive_date(raw_date)
        if normalized is None or published is None:
            raise RuntimeError("OOS Belarus archive row lacks canonical URL or ISO publication date")
        rows.append((normalized, published))
    if not rows:
        raise RuntimeError("OOS Belarus reviewed archive page contains no rows")
    return rows


def _discover_monthly_archives(start: date, end: date, evidence: dict) -> list[str]:
    months = _month_keys(start, end)
    if set(months) != set(REVIEWED_MONTH_PAGE_COUNTS):
        raise RuntimeError("OOS Belarus lacks reviewed finite page counts for the exact requested months")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    robots_text = robots_response.content.decode("utf-8", "replace")
    if any(count > 1 for count in REVIEWED_MONTH_PAGE_COUNTS.values()) and re.search(
        r"(?im)^\s*Disallow:\s*\*/page/\*\s*$", robots_text,
    ):
        raise AccessBlockedError(
            "OOS Belarus robots policy disallows the required paginated archive route"
        )
    selected: dict[str, date] = {}
    receipts = []
    for month in months:
        page_count = REVIEWED_MONTH_PAGE_COUNTS[month]
        if not 1 <= page_count <= 40:
            raise RuntimeError("OOS Belarus reviewed month page count is outside the safety cap")
        for page in range(1, page_count + 1):
            url = _archive_url(month, page)
            common.require_allowed(robots, url)
            response = _request(url, max_bytes=common.MAX_LISTING_BYTES)
            rows = _parse_archive(response.content)
            if any(published.strftime("%Y-%m") != month for _, published in rows):
                raise RuntimeError("OOS Belarus archive contains an out-of-month article")
            receipts.append({
                "month": month, "page": page, "url": url,
                "status": response.status_code, "bytes": len(response.content),
                "sha256": common.sha256(response.content), "rows": len(rows),
            })
            for article_url, published in rows:
                if article_url in selected:
                    raise RuntimeError("OOS Belarus monthly archives repeat a canonical URL")
                selected[article_url] = published
    urls = sorted(url for url, day in selected.items() if start - timedelta(days=2) <= day <= end)
    if not urls:
        raise RuntimeError("OOS Belarus monthly archives selected no URLs")
    evidence.update({
        "mode": "pure_observation_no_production_writes",
        "method": "reviewed_finite_monthly_archives_with_article_page_date_validation",
        "source": Config.source_name,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "requested_whole_months": months,
        "archive_pages": receipts,
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
    return _discover_monthly_archives(start, end, evidence)


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
        raise ValueError("OOS Belarus adapter refused a noncanonical URL")
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, target)
    response = _request(target, max_bytes=common.MAX_ARTICLE_BYTES)
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
