"""Exact, audit-pinned recovery for five theSun URLs with a 301/410 page chain.

The independent evidence report is an allowlist, not a substitute for fresh
publisher requests. Its SHA-256 is pinned here; any report change fails closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


POLICY = "thesun_exact_five_301_410_wp_post_recovery"
REPORT_SHA256 = "57c71130a28f071cb9034dee162e293db45237ab00774dc900ad8fded16286c4"
REPORT_PATH = Path(__file__).resolve().parents[1] / "reports" / "MYS_THESUN_FIVE_410_REST_EVIDENCE_2026-09-17.json"
LOOKUP_FIELDS = "id,slug,status,link,date,date_gmt,title,content,excerpt"
POST_FIELDS = "id,date,date_gmt,modified,slug,status,link,title,content,excerpt"
URLS = frozenset({
    "https://thesun.my/business/corporate-news/fitch-ratings-assigns-first-time-credit-rating-to-hdbank-recognises-its-strong-financial-profile-2",
    "https://thesun.my/business/corporate-news/hdbank-posts-31-rise-in-first-half-profit-assets-exceed-vnd1-quadrillion-2",
    "https://thesun.my/business/corporate-news/bridge-data-centres-and-morong-electric-jointly-launch-the-worlds-first-fully-prefabricated-power-module-for-ai-data-centres",
    "https://thesun.my/business/corporate-news/digital-entertainment-leadership-forum-2026-concludes-three-day-run-with-over-4000-participants-exploring-als-role-in-reshaping-digital-entertainment-and-cultural-creative-industries",
    "https://thesun.my/business/msia-can-weather-oil-price-swings",
})
FIELD_SOURCES = {
    "title": "wp-json.post.title.rendered",
    "date_publish": "wp-json.post.date",
    "maintext": "wp-json.post.content.rendered",
}


def assert_report_unchanged() -> None:
    if hashlib.sha256(REPORT_PATH.read_bytes()).hexdigest() != REPORT_SHA256:
        raise ValueError("Five-post theSun audit report changed")


def specs() -> dict[str, dict]:
    # Check the on-disk report on *every* access, including after parsing was cached.
    assert_report_unchanged()
    return _verified_specs(REPORT_SHA256)


@lru_cache(maxsize=1)
def _verified_specs(verified_digest: str) -> dict[str, dict]:
    if verified_digest != REPORT_SHA256:
        raise ValueError("Five-post theSun audit digest changed")
    assert_report_unchanged()
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    records = report.get("records")
    if report.get("schema_version") != 1 or not isinstance(records, list) or len(records) != 5:
        raise ValueError("Five-post theSun audit report has the wrong shape")
    result = {}
    for row in records:
        url = row.get("url") if isinstance(row, dict) else None
        if not isinstance(url, str) or not url.startswith("https://thesun.my/") or url in result:
            raise ValueError("Five-post theSun audit report has an invalid URL allowlist")
        if row.get("canonical_url") != f"{url}/":
            raise ValueError("Five-post theSun audit report has an invalid canonical URL")
        result[url] = row
    if set(result) != URLS:
        raise ValueError("Five-post theSun audit report changed its exact URL allowlist")
    return result


def _utc_unique(observations: list[dict]) -> bool:
    values = [item.get("observed_at") for item in observations]
    if not all(isinstance(value, str) for value in values) or len(set(values)) != len(values):
        return False
    try:
        parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    except ValueError:
        return False
    return all(value.tzinfo is not None and value.utcoffset() == timedelta(0) for value in parsed)


def _exact_endpoint(value: object, expected: str) -> bool:
    if not isinstance(value, str):
        return False
    actual = urlparse(value)
    target = urlparse(expected)
    return bool(
        actual.scheme == target.scheme == "https"
        and actual.netloc == target.netloc == "thesun.my"
        and actual.path == target.path
        and not actual.params and not actual.fragment
        and parse_qs(actual.query, keep_blank_values=True)
        == parse_qs(target.query, keep_blank_values=True)
    )


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(value, "html.parser").get_text(" ", strip=True)).strip()


def validate_article_recovery(article: dict, evidence: dict | None) -> bool:
    """Replay the exact article, page chain and authoritative WP evidence."""
    url = article.get("url")
    if url not in URLS:
        return article.get("recovery_policy") != POLICY
    allowlist = specs()
    assert_report_unchanged()
    row = allowlist[url]
    evidence = evidence if isinstance(evidence, dict) else {}
    wp = row["wordpress"]
    summary = wp["records"][0]
    post = row["wordpress_exact_post"]
    captured_record = post["record"]
    title = summary["title"]
    body = post["normalized_maintext"]
    if not (
        article.get("recovery_policy") == POLICY
        and article.get("url") == url
        and article.get("source_domain") == "thesun.my"
        and article.get("country_code") == "MYS"
        and article.get("title") == title
        and article.get("date_publish") == summary["date"]
        and article.get("maintext") == body
        and article.get("field_sources") == FIELD_SOURCES
        and article.get("collection") is None
        and article.get("parser_error") is None
        and evidence.get("http_status") == 200
        and evidence.get("publisher_response_recoveries") == [{
            "kind": POLICY, "url": url, "post_id": summary["id"],
            "audit_sha256": REPORT_SHA256,
        }]
        and evidence.get("titles") == [{
            "kind": "wordpress_title", "source": FIELD_SOURCES["title"], "raw": title,
        }]
        and evidence.get("dates") == [{
            "kind": "published", "source": FIELD_SOURCES["date_publish"], "raw": summary["date"],
        }]
        and evidence.get("bodies") == [{
            "kind": "wordpress_content", "source": FIELD_SOURCES["maintext"], "text": body,
        }]
        and len(body) == summary["normalized_body_chars"] == post["normalized_maintext_chars"]
        and len(body.split()) == summary["normalized_body_words"] == post["normalized_maintext_words"]
        and hashlib.sha256(body.encode()).hexdigest()
        == summary["normalized_body_sha256"] == post["normalized_maintext_sha256"]
    ):
        return False

    pairs = evidence.get("publisher_redirect_pairs")
    if not (isinstance(pairs, list) and len(pairs) == 3 and all(isinstance(pair, list) and len(pair) == 2 for pair in pairs)):
        return False
    observed = []
    empty_sha = hashlib.sha256(b"").hexdigest()
    for pair in pairs:
        redirect, gone = pair
        if not (
            isinstance(redirect, dict) and isinstance(gone, dict)
            and redirect.get("request_url") == redirect.get("response_url") == url
            and redirect.get("status_code") == 301
            and redirect.get("location") == row["canonical_url"]
            and redirect.get("response_bytes") == 0
            and redirect.get("response_sha256") == empty_sha
            and redirect.get("direct_request") is True
            and redirect.get("history_count") == 0
            and gone.get("request_url") == gone.get("response_url") == row["canonical_url"]
            and gone.get("status_code") == 410
            and gone.get("location") is None
            and isinstance(gone.get("response_bytes"), int) and gone["response_bytes"] > 0
            and isinstance(gone.get("response_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", gone["response_sha256"])
            and gone.get("direct_request") is True
            and gone.get("history_count") == 0
        ):
            return False
        observed.extend(pair)
    if not _utc_unique(observed):
        return False

    lookup = evidence.get("wordpress_slug_lookup")
    expected_lookup_url = wp["request_url"]
    if not (
        isinstance(lookup, dict)
        and _exact_endpoint(lookup.get("endpoint"), expected_lookup_url)
        and lookup.get("status_code") == 200
        and lookup.get("direct_request") is True
        and lookup.get("history_count") == 0
        and lookup.get("payload") == wp["full_payload"]
        and lookup.get("response_bytes") == wp["response_bytes"]
        and lookup.get("response_sha256") == wp["response_sha256"]
    ):
        return False

    live_post = evidence.get("wordpress_exact_post")
    if not (
        isinstance(live_post, dict)
        and _exact_endpoint(live_post.get("endpoint"), post["request_url"])
        and live_post.get("status_code") == 200
        and live_post.get("direct_request") is True
        and live_post.get("history_count") == 0
        and live_post.get("record") == captured_record
        and live_post.get("normalized_title") == title
        and live_post.get("normalized_maintext") == body
        and live_post.get("response_bytes") == post["response_bytes"]
        and live_post.get("response_sha256") == post["response_sha256"]
        and isinstance(captured_record.get("title"), dict)
        and _clean_html(captured_record["title"].get("rendered", "")) == title
        and isinstance(captured_record.get("content"), dict)
        and captured_record["content"].get("protected") is False
        and _clean_html(captured_record["content"].get("rendered", "")) == body
        and captured_record.get("id") == summary["id"]
        and captured_record.get("slug") == summary["slug"]
        and captured_record.get("status") == summary["status"] == "publish"
        and captured_record.get("link") == summary["link"] == row["canonical_url"]
        and captured_record.get("date") == summary["date"]
        and captured_record.get("date_gmt") == summary["date_gmt"]
    ):
        return False
    return True
