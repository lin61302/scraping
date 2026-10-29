"""Production-disabled bounded pure sitemap candidate for Eye Radio."""
from __future__ import annotations

import hashlib
import html
import json
import os
import time
import unicodedata
from datetime import date
from urllib.parse import urlparse

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
# The sole reviewed redirect identity is represented only by the SHA-256 of its
# canonical source URL.  The private identity ledger remains outside code.
REVIEWED_REDIRECT_SOURCE_HASHES = frozenset({
    "fe5ae0850654e8536f0bfb372abfcc8648bea9b570a523b5e7952be30bfe9b34",
})

RouteProfileUnverifiedError = common.RouteProfileUnverifiedError


def _request(url: str, *, max_bytes: int, timeout: int = 45):
    return common.bounded_request(url, allowed_hosts=ALLOWED_HOSTS, max_bytes=max_bytes, timeout=timeout)


def _request_redirect_probe(url: str, *, max_bytes: int, timeout: int = 45):
    """Perform one bounded, no-follow request for the single reviewed identity."""
    if not common._host_allowed(url, ALLOWED_HOSTS):
        raise ValueError("Eye Radio redirect probe refused a non-allowlisted URL")
    # The robots request immediately precedes this exceptional probe.  A full
    # interval is deliberately conservative and avoids a second pacing surface.
    if common.MIN_REQUEST_INTERVAL_SECONDS > 0:
        time.sleep(common.MIN_REQUEST_INTERVAL_SECONDS)
    response = common.requests.get(
        url,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
        headers={
            "User-Agent": common.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.1",
        },
    )
    try:
        if response.status_code in {401, 403, 407, 429, 451}:
            raise common.AccessBlockedError("publisher denied the reviewed redirect probe")
        if 300 <= response.status_code < 400:
            response.scrape_transport = "requests_stream_bounded_manual_redirect_probe"
            return response
        response.raise_for_status()
        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                if int(raw_length) > max_bytes:
                    raise RuntimeError("publisher response exceeds the configured byte cap")
            except ValueError as exc:
                raise RuntimeError("publisher returned an invalid Content-Length") from exc
        chunks: list[bytes] = []
        observed = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            observed += len(chunk)
            if observed > max_bytes:
                raise RuntimeError("publisher response exceeds the configured byte cap")
            chunks.append(chunk)
        payload = b"".join(chunks)
        if not payload:
            raise RuntimeError("publisher returned an empty response")
        response._content = payload
        response._content_consumed = True
        response.scrape_transport = "requests_stream_bounded_manual_redirect_probe"
        return response
    finally:
        response.close()


def _title_key(raw: object) -> str:
    """Normalize presentation-only differences while retaining semantic text."""
    value = unicodedata.normalize("NFKC", html.unescape(str(raw or ""))).casefold()
    return " ".join(
        "".join(" " if ch.isspace() or unicodedata.category(ch).startswith("P") else ch for ch in value).split()
    )


def _error_list(article: dict) -> list[str]:
    return [value for value in (article.get("parser_error") or "").split(";") if value]


def _canonical_jsonld_identity(item: dict) -> set[str]:
    values: list[object] = [item.get("url"), item.get("@id")]
    main = item.get("mainEntityOfPage")
    if isinstance(main, dict):
        values.extend((main.get("@id"), main.get("url")))
    else:
        values.append(main)
    result = set()
    for value in values:
        normalized = common.canonicalize_url(
            value,
            allowed_hosts=ALLOWED_HOSTS,
            canonical_host=CANONICAL_HOST,
        )
        if normalized:
            result.add(normalized)
    return result


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


def _blogposting(soup: BeautifulSoup, url: str) -> tuple[dict, dict]:
    candidates = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        values = list(value) if isinstance(value, list) else []
        if isinstance(value, dict):
            values = [value, *(value.get("@graph", []) if isinstance(value.get("@graph"), list) else [])]
        for item in values:
            kinds = item.get("@type") if isinstance(item, dict) else None
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if "BlogPosting" in kinds:
                candidates.append(item)
    matching = [item for item in candidates if url in _canonical_jsonld_identity(item)]
    if len(matching) == 1:
        return matching[0], {
            "candidate_count": len(candidates),
            "canonical_match_count": 1,
            "selection": "unique_canonical_match",
        }
    return {}, {
        "candidate_count": len(candidates),
        "canonical_match_count": len(matching),
        "selection": "none_or_ambiguous",
    }


def _same_publisher_calendar_day(raw_values: list[object]) -> tuple[bool, str | None]:
    parsed = [common.parse_datetime(value) for value in raw_values if common.clean(value)]
    if len(parsed) < 2 or any(value is None for value in parsed):
        return False, None
    days = {value.date().isoformat() for value in parsed if value is not None}
    return len(days) == 1, next(iter(days)) if len(days) == 1 else None


def _parse_article_html(url: str, payload: bytes, *, response_url: str | None = None) -> dict:
    article = common.parse_article_html(url=url, payload=payload, source_name=Config.source_name, country_code=Config.country_code, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST, expected_languages=EXPECTED_LANGUAGES, title_selectors=TITLE_SELECTORS, body_selectors=BODY_SELECTORS, response_url=response_url, structured_article_required=False)
    soup = BeautifulSoup(payload, "html.parser")
    item, selection = _blogposting(soup, url.rstrip("/"))
    headline = common.clean(item.get("headline") or item.get("name"))
    published = common.parse_datetime(item.get("datePublished"))
    meta = soup.select_one('meta[property="article:published_time"][content]')
    meta_raw = meta.get("content") if meta else None
    meta_published = common.parse_datetime(meta_raw)
    language = common.clean(item.get("inLanguage"))
    sections = item.get("articleSection")
    sections = sections if isinstance(sections, list) else [sections]
    normalized_sections = {common.clean(value).casefold() for value in sections if common.clean(value)}
    errors = [value for value in (article.get("parser_error") or "").split(";") if value and value != "insufficient_independent_publication_dates"]
    visible_title = common.clean(article.get("title"))
    og_title = common.clean(article.get("field_evidence", {}).get("og_title"))
    title_match = bool(headline and visible_title and _title_key(headline) == _title_key(visible_title))
    if og_title and visible_title and _title_key(og_title) == _title_key(visible_title):
        errors = [value for value in errors if value != "visible_vs_og_title_disagreement"]
    if not item or not title_match:
        errors.append("visible_vs_blogposting_headline_disagreement")
    same_day, calendar_day = _same_publisher_calendar_day([item.get("datePublished"), meta_raw])
    page_raw_dates = [
        value for value in article.get("field_evidence", {}).get("publication_dates", {}).values()
        if common.clean(value)
    ]
    page_same_day, _ = _same_publisher_calendar_day(page_raw_dates)
    if page_same_day:
        errors = [value for value in errors if value != "page_publication_dates_disagree"]
    if published is None or meta_published is None or not same_day:
        errors.append("meta_vs_blogposting_publication_date_disagreement")
    if not language or language.split("-", 1)[0].casefold() != "en":
        errors.append("unexpected_blogposting_language")
    if normalized_sections & EXCLUDED_SECTIONS:
        errors.append("excluded_publisher_section")
    article["parser_error"] = ";".join(dict.fromkeys(errors)) or None
    article["field_evidence"].update({
        "blogposting_selection": selection,
        "blogposting_headline": headline,
        "blogposting_date_published": item.get("datePublished"),
        "blogposting_language": language,
        "blogposting_sections": sorted(normalized_sections),
        "excluded_sections": sorted(EXCLUDED_SECTIONS),
        "title_reconciliation": {
            "method": "unicode_nfkc_entity_whitespace_case_and_punctuation_only",
            "matched": title_match,
            "semantic_edits_allowed": False,
        },
        "publication_date_reconciliation": {
            "matched_same_publisher_calendar_day": same_day,
            "publisher_calendar_day": calendar_day,
            "sources": ["meta[property=article:published_time]", "jsonld.BlogPosting.datePublished"],
            "different_calendar_days_allowed": False,
        },
    })
    return article


def _redirect_target(location: object) -> tuple[str, str]:
    if not isinstance(location, str) or not location.strip():
        raise common.AccessBlockedError("Eye Radio reviewed redirect omitted a valid target")
    raw = location.strip()
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError as exc:
        raise common.AccessBlockedError("Eye Radio reviewed redirect target was malformed") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != CANONICAL_HOST
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise common.AccessBlockedError("Eye Radio reviewed redirect target violated the same-host HTTPS contract")
    canonical = common.canonicalize_url(raw, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if canonical is None or raw not in {canonical, canonical + "/"}:
        raise common.AccessBlockedError("Eye Radio reviewed redirect target was root, media, or noncanonical")
    return canonical, canonical + ("/" if raw.endswith("/") else "")


def scrape_url(url: str) -> dict:
    _require_live_gates()
    target = common.canonicalize_url(url, allowed_hosts=ALLOWED_HOSTS, canonical_host=CANONICAL_HOST)
    if target != url.rstrip("/"):
        raise ValueError("Eye Radio adapter refused a noncanonical URL")
    article_request_url = target + "/"
    robots_response, robots = common.robots_for(ROBOTS_URL, _request)
    common.require_allowed(robots, article_request_url)
    source_hash = hashlib.sha256(target.encode("utf-8")).hexdigest()
    reviewed_redirect_identity = source_hash in REVIEWED_REDIRECT_SOURCE_HASHES
    response = (
        _request_redirect_probe(article_request_url, max_bytes=common.MAX_ARTICLE_BYTES)
        if reviewed_redirect_identity
        else _request(article_request_url, max_bytes=common.MAX_ARTICLE_BYTES)
    )
    if 300 <= response.status_code < 400:
        if not reviewed_redirect_identity or response.status_code not in {301, 302, 307, 308}:
            raise common.AccessBlockedError("Eye Radio returned an unreviewed redirect")
        redirect_target, redirect_request_url = _redirect_target(response.headers.get("Location"))
        common.require_allowed(robots, redirect_request_url)
        redirected = _request(redirect_request_url, max_bytes=common.MAX_ARTICLE_BYTES)
        if redirected.url != redirect_request_url:
            raise common.AccessBlockedError("Eye Radio reviewed redirect attempted a second hop")
        article = _parse_article_html(redirect_target, redirected.content, response_url=redirected.url)
        article["url"] = target
        article["field_evidence"]["redirect_repair"] = {
            "reviewed_source_sha256": source_hash,
            "target_sha256": hashlib.sha256(redirect_target.encode("utf-8")).hexdigest(),
            "initial_status": response.status_code,
            "hop_count": 1,
            "https_same_host": True,
            "target_robots_allowed": True,
            "original_identity_preserved": True,
            "raw_urls_recorded": False,
        }
        article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
        return article
    if response.url != article_request_url:
        raise common.AccessBlockedError("Eye Radio returned an unexpected article response URL")
    article = _parse_article_html(target, response.content, response_url=response.url)
    article["field_evidence"]["robots_sha256"] = common.sha256(robots_response.content)
    return article
