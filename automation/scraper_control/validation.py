from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from .thesun_five import (
    POLICY as THESUN_FIVE_POLICY,
    REPORT_SHA256 as THESUN_FIVE_REPORT_SHA256,
    URLS as THESUN_FIVE_URLS,
    specs as thesun_five_specs,
)


CHALLENGE_PATTERNS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "captcha challenge",
    "complete the captcha",
    "verify you are human",
    "service temporarily unavailable",
    "access denied",
)

TITLE_CHALLENGE = re.compile(
    r"^\s*(?:just a moment(?:\.{0,3})?|access denied|attention required!?|"
    r"checking your browser(?:\.{0,3})?|captcha challenge|complete the captcha|"
    r"verify you are human|service temporarily unavailable|web application firewall)"
    r"(?:\s*[|–—-]\s*cloudflare)?\s*$",
    re.I,
)

BRIEF_PLACEHOLDER_PATTERNS = (
    "details coming",
    "more details soon",
    "developing story",
    "ayrıntılar geliyor",
    "detaylar geliyor",
)

THESUN_REDIRECT_LOOP_POLICY = "thesun_exact_redirect_loop_wp_recovery"
THESUN_REDIRECT_LOOP_URL = (
    "https://thesun.my/education/"
    "education-ministry-drops-non-academic-competitions-to-ease-teachers-workload"
)
THESUN_REDIRECT_LOOP_ALIAS = (
    "https://thesun.my/business/corporate-news/"
    "education-ministry-drops-non-academic-competitions-to-ease-teachers-workload/"
)
THESUN_REDIRECT_LOOP_SLUG = "education-ministry-drops-non-academic-competitions-to-ease-teachers-workload"
THESUN_REDIRECT_LOOP_POST_ID = 669454
THESUN_REDIRECT_LOOP_DATE = "2026-09-02T13:02:50"
THESUN_REDIRECT_LOOP_MODIFIED = "2026-09-02T16:48:58"
THESUN_REDIRECT_LOOP_TITLE = "Education Ministry drops non-academic competitions to ease teachers’ workload"
THESUN_REDIRECT_LOOP_MAIN_TEXT = (
    "Education Minister Datuk Seri Fadhlina Sidek said the move included competitions such as the "
    "best-decorated toilet, most attractive school compound and cleanest classroom, among others. "
    "PETALING JAYA: The Education Ministry will discontinue several competitions and programmes "
    "considered to have little direct impact on students’ learning as part of efforts to reduce "
    "teachers’ administrative burden. Education Minister Datuk Seri Fadhlina Sidek said the move "
    "included competitions such as the best-decorated toilet, most attractive school compound and "
    "cleanest classroom, among others. In a Facebook post, she said such activities required more "
    "than just decorating, as teachers also had to attend meetings, make preparations and complete "
    "documentation and reports. “There are meetings, preparations, documentation and reports. A "
    "lot of time and energy has to be spent on these activities,” she said. Fadhlina said the "
    "ministry would gradually discontinue programmes it considered unnecessary so teachers could "
    "concentrate on their core responsibilities. She stressed that teachers should be able to "
    "devote more time and energy to teaching and educating students. “That is why we are starting "
    "to remove unnecessary programmes one by one so that teachers can focus on what is more "
    "important. “Teaching and educating our children. This is one of the seven initial steps to "
    "reduce the burden on teachers. “The others? We will tell you one by one,” she said. Fadhlina "
    "also indicated that the remaining six measures would be announced gradually through a series "
    "of posts titled “What is Fadhlina doing?”"
)
THESUN_REDIRECT_LOOP_MAIN_TEXT_SHA256 = "1e3fe799f9cadaf87f0d539e09aa355473280fd4f22bbfd8e6cdb9ec62c92ed0"
THESUN_REDIRECT_LOOP_LOOKUP_BYTES = 269
THESUN_REDIRECT_LOOP_LOOKUP_SHA256 = "c24bac161209d8bfa8902105f1517de7ca29f2e1f5f2083ec1d22e6e85e00813"
THESUN_REDIRECT_LOOP_POST_BYTES = 2879
THESUN_REDIRECT_LOOP_POST_SHA256 = "18d34d6d17bb5fa82798cdd56d89f1366b5666a1ee85669a49b1017bde1a1168"
THESUN_REPAIR_DERIVATION = "repair_exact_exceptions_and_exclude_verified_window_spillovers"
THESUN_REMOVED_REDIRECT_POLICY = "thesun_exact_removed_redirect"
THESUN_REMOVED_REDIRECT_URL = (
    "https://thesun.my/news/malaysia-news/people-issues/"
    "pmo-lodges-police-report-over-fake-letter-on-plus-widening-project"
)
THESUN_REMOVED_PARENT_TITLE = "Tuanku Muhriz set to return as Negri Sembilan throne dispute deepens"
THESUN_REMOVED_PARENT_DATE = "2026-05-15T16:26:26"


@dataclass(frozen=True)
class FieldCheck:
    field: str
    status: str
    confidence: float
    codes: List[str]
    evidence: Dict[str, object]


@dataclass(frozen=True)
class ArticleCheck:
    eligible: bool
    fields: List[FieldCheck]

    def as_dict(self):
        return {"eligible": self.eligible, "fields": [asdict(item) for item in self.fields]}


def validate_thesun_redirect_loop_manifest(manifest: Dict[str, object], selected_urls: Sequence[str]) -> bool:
    """Bind exact theSun exception repairs to their selected rows."""
    repairs = manifest.get("repairs")
    mentions_loop = bool(
        manifest.get("redirect_loop_repairs")
        or (
            isinstance(repairs, list)
            and any(
                isinstance(item, dict)
                and (
                    item.get("url") == THESUN_REDIRECT_LOOP_URL
                    or item.get("repair") == THESUN_REDIRECT_LOOP_POLICY
                    or item.get("wordpress_post_id") == THESUN_REDIRECT_LOOP_POST_ID
                )
                for item in repairs
            )
        )
    )
    has_url = THESUN_REDIRECT_LOOP_URL in selected_urls
    if not has_url:
        loop_valid = bool(
            not mentions_loop
            and manifest.get("redirect_loop_repairs") in (None, 0)
        )
    elif not isinstance(repairs, list):
        loop_valid = False
    else:
        loop_entries = [
            item for item in repairs
            if isinstance(item, dict) and (
                item.get("url") == THESUN_REDIRECT_LOOP_URL
                or item.get("repair") == THESUN_REDIRECT_LOOP_POLICY
                or item.get("wordpress_post_id") == THESUN_REDIRECT_LOOP_POST_ID
            )
        ]
        expected = {
            "url": THESUN_REDIRECT_LOOP_URL,
            "repair": THESUN_REDIRECT_LOOP_POLICY,
            "redirect_pairs": 3,
            "wordpress_post_id": THESUN_REDIRECT_LOOP_POST_ID,
            "date_publish": THESUN_REDIRECT_LOOP_DATE,
        }
        loop_valid = bool(
            manifest.get("redirect_loop_repairs") == 1
            and loop_entries == [expected]
        )

    mentions_removed = bool(
        manifest.get("removed_redirect_exclusions")
        or (
            isinstance(repairs, list)
            and any(
                isinstance(item, dict)
                and (
                    item.get("url") == THESUN_REMOVED_REDIRECT_URL
                    or item.get("policy") == THESUN_REMOVED_REDIRECT_POLICY
                    or item.get("parent_malformed_title") == THESUN_REMOVED_PARENT_TITLE
                    or item.get("parent_malformed_date") == THESUN_REMOVED_PARENT_DATE
                )
                for item in repairs
            )
        )
    )
    has_removed_url = THESUN_REMOVED_REDIRECT_URL in selected_urls
    if not has_removed_url:
        removed_valid = bool(
            not mentions_removed
            and manifest.get("removed_redirect_exclusions") in (None, 0)
        )
    elif not isinstance(repairs, list):
        removed_valid = False
    else:
        removed_entries = [
            item for item in repairs
            if isinstance(item, dict) and (
                item.get("url") == THESUN_REMOVED_REDIRECT_URL
                or item.get("policy") == THESUN_REMOVED_REDIRECT_POLICY
                or item.get("parent_malformed_title") == THESUN_REMOVED_PARENT_TITLE
                or item.get("parent_malformed_date") == THESUN_REMOVED_PARENT_DATE
            )
        ]
        expected_removed = {
            "url": THESUN_REMOVED_REDIRECT_URL,
            "policy": THESUN_REMOVED_REDIRECT_POLICY,
            "observations": 3,
            "wordpress_records": 0,
            "parent_malformed_title": THESUN_REMOVED_PARENT_TITLE,
            "parent_malformed_date": THESUN_REMOVED_PARENT_DATE,
        }
        removed_valid = bool(
            manifest.get("removed_redirect_exclusions") == 1
            and removed_entries == [expected_removed]
        )
    selected_five = {url for url in selected_urls if url in THESUN_FIVE_URLS}
    if not selected_five:
        five_valid = bool(
            manifest.get("five_post_repairs") in (None, 0)
            and not (
                isinstance(repairs, list)
                and any(
                    isinstance(item, dict)
                    and (
                        item.get("url") in THESUN_FIVE_URLS
                        or item.get("repair") == THESUN_FIVE_POLICY
                        or item.get("audit_sha256") == THESUN_FIVE_REPORT_SHA256
                    )
                    for item in repairs
                )
            )
        )
    elif not isinstance(repairs, list):
        five_valid = False
    else:
        five_specs = thesun_five_specs()
        five_entries = [
            item for item in repairs
            if isinstance(item, dict) and (
                item.get("url") in five_specs
                or item.get("repair") == THESUN_FIVE_POLICY
                or item.get("audit_sha256") == THESUN_FIVE_REPORT_SHA256
            )
        ]
        expected_five = [
            {
                "url": url,
                "repair": THESUN_FIVE_POLICY,
                "redirect_pairs": 3,
                "wordpress_post_id": five_specs[url]["wordpress"]["records"][0]["id"],
                "date_publish": five_specs[url]["wordpress"]["records"][0]["date"],
                "audit_sha256": THESUN_FIVE_REPORT_SHA256,
            }
            for url in selected_urls if url in selected_five
        ]
        five_valid = bool(
            manifest.get("five_post_repairs", 0) == len(selected_five)
            and five_entries == expected_five
        )
    return bool(loop_valid and removed_valid and five_valid)


def validate_thesun_redirect_loop_recovery(
    article: Dict[str, object], page_evidence: Optional[Dict[str, object]],
) -> bool:
    """Revalidate the one exact theSun redirect-loop repair from stored evidence."""
    if article.get("recovery_policy") != THESUN_REDIRECT_LOOP_POLICY:
        return not (
            article.get("url") == THESUN_REDIRECT_LOOP_URL
            and isinstance(page_evidence, dict)
            and "publisher_redirect_loop" in page_evidence
        )
    evidence = page_evidence if isinstance(page_evidence, dict) else {}
    expected_sources = {
        "title": "wp-json.post.title.rendered",
        "date_publish": "wp-json.post.date",
        "maintext": "wp-json.post.content.rendered",
    }
    expected_link = f"{THESUN_REDIRECT_LOOP_URL}/"
    if not (
        article.get("url") == THESUN_REDIRECT_LOOP_URL
        and article.get("title") == THESUN_REDIRECT_LOOP_TITLE
        and article.get("date_publish") == THESUN_REDIRECT_LOOP_DATE
        and article.get("maintext") == THESUN_REDIRECT_LOOP_MAIN_TEXT
        and article.get("field_sources") == expected_sources
        and article.get("collection") is None
        and article.get("parser_error") is None
        and hashlib.sha256(str(article.get("maintext")).encode()).hexdigest()
        == THESUN_REDIRECT_LOOP_MAIN_TEXT_SHA256
        and evidence.get("http_status") == 200
        and evidence.get("publisher_response_recoveries") == [{
            "kind": THESUN_REDIRECT_LOOP_POLICY,
            "url": THESUN_REDIRECT_LOOP_URL,
            "post_id": THESUN_REDIRECT_LOOP_POST_ID,
        }]
        and evidence.get("titles") == [{
            "kind": "wordpress_title", "source": expected_sources["title"],
            "raw": THESUN_REDIRECT_LOOP_TITLE,
        }]
        and evidence.get("dates") == [{
            "kind": "published", "source": expected_sources["date_publish"],
            "raw": THESUN_REDIRECT_LOOP_DATE,
        }]
        and evidence.get("bodies") == [{
            "kind": "wordpress_content", "source": expected_sources["maintext"],
            "text": THESUN_REDIRECT_LOOP_MAIN_TEXT,
        }]
    ):
        return False

    loop = evidence.get("publisher_redirect_loop")
    empty_sha = hashlib.sha256(b"").hexdigest()
    if not (
        isinstance(loop, list)
        and len(loop) == 3
        and all(isinstance(pair, list) and len(pair) == 2 for pair in loop)
    ):
        return False
    observed_at = []
    for pair in loop:
        for item, request_url, location in (
            (pair[0], THESUN_REDIRECT_LOOP_URL, THESUN_REDIRECT_LOOP_ALIAS),
            (pair[1], THESUN_REDIRECT_LOOP_ALIAS, expected_link),
        ):
            if not (
                isinstance(item, dict)
                and item.get("request_url") == request_url
                and item.get("response_url") == request_url
                and item.get("status_code") == 301
                and item.get("location") == location
                and item.get("response_bytes") == 0
                and item.get("response_sha256") == empty_sha
                and item.get("direct_request") is True
                and item.get("history_count") == 0
                and isinstance(item.get("observed_at"), str)
                and item.get("observed_at")
            ):
                return False
            observed_at.append(item["observed_at"])
    try:
        parsed_observed_at = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in observed_at]
    except ValueError:
        return False
    if not (
        len(set(observed_at)) == 6
        and all(value.tzinfo is not None and value.utcoffset() == timedelta(0) for value in parsed_observed_at)
    ):
        return False

    expected_lookup_record = {
        "id": THESUN_REDIRECT_LOOP_POST_ID,
        "slug": THESUN_REDIRECT_LOOP_SLUG,
        "status": "publish",
        "link": expected_link,
        "date": THESUN_REDIRECT_LOOP_DATE,
    }
    lookup = evidence.get("wordpress_slug_lookup")
    lookup_endpoint = str(lookup.get("endpoint") or "") if isinstance(lookup, dict) else ""
    parsed_lookup = urlparse(lookup_endpoint)
    if not (
        isinstance(lookup, dict)
        and lookup.get("status_code") == 200
        and lookup.get("direct_request") is True
        and lookup.get("history_count") == 0
        and lookup.get("response_bytes") == THESUN_REDIRECT_LOOP_LOOKUP_BYTES
        and lookup.get("response_sha256") == THESUN_REDIRECT_LOOP_LOOKUP_SHA256
        and lookup.get("payload") == [expected_lookup_record]
        and parsed_lookup.scheme == "https"
        and parsed_lookup.netloc == "thesun.my"
        and parsed_lookup.path == "/wp-json/wp/v2/posts"
        and not parsed_lookup.params
        and not parsed_lookup.fragment
        and parse_qs(parsed_lookup.query, keep_blank_values=True) == {
            "slug": [THESUN_REDIRECT_LOOP_SLUG],
            "_fields": ["id,slug,status,link,date"],
            "per_page": ["10"],
        }
    ):
        return False

    post = evidence.get("wordpress_exact_post")
    post_endpoint = str(post.get("endpoint") or "") if isinstance(post, dict) else ""
    parsed_post = urlparse(post_endpoint)
    record = post.get("record") if isinstance(post, dict) else None
    if not (
        isinstance(post, dict)
        and post.get("status_code") == 200
        and post.get("direct_request") is True
        and post.get("history_count") == 0
        and post.get("normalized_title") == THESUN_REDIRECT_LOOP_TITLE
        and post.get("normalized_maintext") == THESUN_REDIRECT_LOOP_MAIN_TEXT
        and post.get("normalized_maintext_chars") == 1599
        and post.get("normalized_maintext_words") == 240
        and post.get("normalized_maintext_sha256") == THESUN_REDIRECT_LOOP_MAIN_TEXT_SHA256
        and post.get("response_sha256") == THESUN_REDIRECT_LOOP_POST_SHA256
        and post.get("response_bytes") == THESUN_REDIRECT_LOOP_POST_BYTES
        and parsed_post.scheme == "https"
        and parsed_post.netloc == "thesun.my"
        and parsed_post.path == f"/wp-json/wp/v2/posts/{THESUN_REDIRECT_LOOP_POST_ID}"
        and not parsed_post.params
        and not parsed_post.fragment
        and parse_qs(parsed_post.query, keep_blank_values=True) == {
            "_fields": ["id,date,modified,slug,status,link,title,content,excerpt"],
        }
        and isinstance(record, dict)
        and set(record) == {"id", "date", "modified", "slug", "status", "link", "title", "content", "excerpt"}
        and record.get("id") == THESUN_REDIRECT_LOOP_POST_ID
        and record.get("date") == THESUN_REDIRECT_LOOP_DATE
        and record.get("modified") == THESUN_REDIRECT_LOOP_MODIFIED
        and record.get("slug") == THESUN_REDIRECT_LOOP_SLUG
        and record.get("status") == "publish"
        and record.get("link") == expected_link
        and record.get("title") == {"rendered": THESUN_REDIRECT_LOOP_TITLE}
        and isinstance(record.get("content"), dict)
        and record["content"].get("protected") is False
        and isinstance(record["content"].get("rendered"), str)
        and isinstance(record.get("excerpt"), dict)
        and record["excerpt"].get("protected") is False
        and isinstance(record["excerpt"].get("rendered"), str)
    ):
        return False
    from bs4 import BeautifulSoup

    rendered = record["content"]["rendered"]
    normalized = re.sub(r"\s+", " ", BeautifulSoup(rendered, "html.parser").get_text(" ", strip=True)).strip()
    return bool(
        normalized == THESUN_REDIRECT_LOOP_MAIN_TEXT
        and hashlib.sha256(normalized.encode()).hexdigest() == THESUN_REDIRECT_LOOP_MAIN_TEXT_SHA256
        and post.get("content_sha256") == hashlib.sha256(rendered.encode()).hexdigest()
    )

def resolve_with_page_evidence(
    article_check: ArticleCheck,
    evidence_checks: Sequence[FieldCheck],
    page_evidence: Optional[Dict[str, object]],
) -> tuple[bool, List[Dict[str, object]]]:
    """Apply narrow, auditable resolutions after independent page comparison.

    Brief news reports are valid even when they fall just below the generic
    word-count threshold. They are accepted only when every semantic page
    comparison passes and the extracted text is an almost exact match to a
    publisher-provided body signal. The original failed check remains in the
    audit record.
    """
    failed = [item for item in article_check.fields if item.status != "pass"]
    evidence_failed = [item for item in evidence_checks if item.status != "pass"]
    if not failed and not evidence_failed:
        return True, []
    if evidence_failed:
        return False, []
    evidence_by_field = {item.field: item for item in evidence_checks}
    body_check = evidence_by_field.get("maintext")
    title_check = evidence_by_field.get("title")
    date_check = evidence_by_field.get("date_publish")
    failed_by_field = {item.field: item for item in failed}
    if set(failed_by_field) == {"title", "maintext"}:
        short_title = failed_by_field["title"]
        short_body = failed_by_field["maintext"]
        title_length = int(short_title.evidence.get("length", 0) or 0)
        words = int(short_body.evidence.get("words", 0) or 0)
        characters = int(short_body.evidence.get("characters", 0) or 0)
        preview = str(short_body.evidence.get("preview", "") or "").casefold()
        http_status = (page_evidence or {}).get("http_status")
        verified_micro_item = (
            short_title.codes == ["title_too_short"]
            and short_body.codes == ["maintext_too_short"]
            and title_length >= 3
            and words >= 10
            and characters >= 75
            and not any(pattern in preview for pattern in BRIEF_PLACEHOLDER_PATTERNS)
            and title_check is not None and title_check.status == "pass"
            and float(title_check.evidence.get("best_similarity", 0.0) or 0.0) >= 0.98
            and body_check is not None and body_check.status == "pass"
            and float(body_check.evidence.get("best_word_overlap", 0.0) or 0.0) >= 0.95
            and date_check is not None and date_check.status == "pass"
            and http_status in (None, 200)
        )
        if verified_micro_item:
            return True, [
                {
                    "code": "evidence_confirmed_short_title",
                    "field": "title",
                    "original_codes": short_title.codes,
                    "length": title_length,
                    "title_similarity": title_check.evidence.get("best_similarity"),
                },
                {
                    "code": "evidence_confirmed_short_article",
                    "field": "maintext",
                    "original_codes": short_body.codes,
                    "words": words,
                    "characters": characters,
                    "body_word_overlap": body_check.evidence.get("best_word_overlap"),
                },
            ]
        return False, []
    if len(failed) != 1:
        return False, []
    short = failed[0]
    words = int(short.evidence.get("words", 0) or 0)
    characters = int(short.evidence.get("characters", 0) or 0)
    preview = str(short.evidence.get("preview", "") or "").casefold()
    http_status = (page_evidence or {}).get("http_status")
    is_verified_brief = (
        short.field == "maintext"
        and short.codes == ["maintext_too_short"]
        and words >= 10
        and characters >= 75
        and not any(pattern in preview for pattern in BRIEF_PLACEHOLDER_PATTERNS)
        and body_check is not None
        and body_check.status == "pass"
        and float(body_check.evidence.get("best_word_overlap", 0.0) or 0.0) >= 0.95
        and title_check is not None and title_check.status == "pass"
        and date_check is not None and date_check.status == "pass"
        and http_status in (None, 200)
    )
    if is_verified_brief:
        return True, [{
            "code": "evidence_confirmed_short_article",
            "field": "maintext",
            "original_codes": short.codes,
            "words": words,
            "characters": characters,
            "body_word_overlap": body_check.evidence.get("best_word_overlap"),
        }]
    title_length = int(short.evidence.get("length", 0) or 0)
    is_verified_short_title = (
        short.field == "title"
        and short.codes == ["title_too_short"]
        and title_length >= 3
        and title_check is not None
        and title_check.status == "pass"
        and float(title_check.evidence.get("best_similarity", 0.0) or 0.0) >= 0.98
        and date_check is not None and date_check.status == "pass"
        and body_check is not None and body_check.status == "pass"
        and http_status in (None, 200)
    )
    if is_verified_short_title:
        return True, [{
            "code": "evidence_confirmed_short_title",
            "field": "title",
            "original_codes": short.codes,
            "length": title_length,
            "title_similarity": title_check.evidence.get("best_similarity"),
        }]
    return False, []


def resolve_policy_exclusion(
    article_check: ArticleCheck,
    evidence_checks: Sequence[FieldCheck],
    page_evidence: Optional[Dict[str, object]],
    reason: object,
    exclusion_evidence: object,
    *,
    article_url: object = None,
) -> tuple[bool, Optional[Dict[str, object]]]:
    """Validate narrow non-article exclusions while retaining field failures."""
    if not isinstance(reason, str) or not reason or not isinstance(exclusion_evidence, dict):
        return False, None
    evidence = page_evidence or {}
    failed = [item for item in article_check.fields if item.status != "pass"]
    failed_by_field = {item.field: set(item.codes) for item in failed}
    passed_fields = {item.field for item in article_check.fields if item.status == "pass"}
    evidence_failed = [item for item in evidence_checks if item.status != "pass"]
    evidence_failed_by_field = {item.field: set(item.codes) for item in evidence_failed}

    if (
        exclusion_evidence.get("policy_type") == "publisher_podcast_description_without_transcript"
        and reason == "excluded_podcast_description_without_transcript"
    ):
        url = str(article_url or "")
        declared_count = exclusion_evidence.get("paragraph_count")
        valid = bool(
            not failed and not evidence_failed
            and url.startswith("https://apublica.org/podcast/")
            and exclusion_evidence.get("publisher_url") == url
            and isinstance(declared_count, int) and 1 <= declared_count <= 3
            and evidence.get("paragraph_count") == declared_count
            and evidence.get("podcast_description_only") is True
            and exclusion_evidence.get("audio_player_present") is True
            and evidence.get("podcast_audio_player") is True
            and exclusion_evidence.get("transcript_markers_present") is False
            and evidence.get("podcast_transcript_markers") is False
            and evidence.get("http_status") == 200
            and evidence.get("titles") and evidence.get("dates") and evidence.get("bodies")
            and exclusion_evidence.get("description_characters")
            == len(str((evidence.get("bodies") or [{}])[0].get("text") or ""))
        )
        return valid, {"mode": "publisher_podcast_description_without_transcript"} if valid else None

    if (
        exclusion_evidence.get("policy_type") == "jb_embedded_media_without_article_prose"
        and reason == "excluded_embedded_media_without_article_prose"
    ):
        # Jornal do Brasil indexes video/social posts as dated article pages.
        # The adapter may skip one only when the publisher DOM has a real
        # embed and no ordinary prose remains outside that embed.
        declared = {
            key: exclusion_evidence.get(key)
            for key in ("provider", "selector", "media_url", "body_text_after_media", "body_only_media")
        }
        media_url = str(declared["media_url"] or "")
        provider_url_ok = bool(
            (declared["provider"] == "youtube" and re.fullmatch(r"https://www\.youtube\.com/embed/[A-Za-z0-9_-]+", media_url))
            or (declared["provider"] == "instagram" and re.fullmatch(r"https://(?:www\.)?instagram\.com/(?:p|reel)/[A-Za-z0-9_-]+/embed/captioned/", media_url))
            or (declared["provider"] == "twitter" and re.fullmatch(r"https://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/\d+", media_url))
        )
        only_expected_failure = bool(
            set(failed_by_field) <= {"maintext"}
            and all(codes <= {"maintext_missing", "maintext_too_short"} for codes in failed_by_field.values())
            and set(evidence_failed_by_field) <= {"maintext"}
            and all(codes <= {"maintext_evidence_missing"} for codes in evidence_failed_by_field.values())
            and {"title", "date_publish"} <= passed_fields
        )
        article_url_text = str(article_url or "")
        valid = bool(
            article_url_text.startswith("https://www.jb.com.br/")
            and exclusion_evidence.get("publisher_url") == article_url_text
            and evidence.get("http_status") == 200
            and evidence.get("titles") and evidence.get("dates")
            and declared["selector"]
            and declared["body_text_after_media"] == ""
            and declared["body_only_media"] is True
            and evidence.get("publisher_media") == declared
            and provider_url_ok and only_expected_failure
        )
        return valid, {"mode": "jb_embedded_media_without_article_prose", "provider": declared["provider"]} if valid else None

    if not failed and not evidence_failed:
        return True, {"mode": "fully_valid_article_policy_exclusion"}

    policy_type = exclusion_evidence.get("policy_type")
    if policy_type == "publisher_external_redirect" and reason == "excluded_external_redirect":
        # A publisher sitemap can retain a local URL whose only content is a
        # permanent redirect to another publisher.  Accept this exclusion only
        # for the one evidenced Malaysiakini legacy award redirect; this keeps a
        # missing date on the external archive page from becoming a fabricated
        # current publication date.
        only_expected_article_failure = (
            set(failed_by_field) == {"date_publish"}
            and bool(failed_by_field["date_publish"])
            and failed_by_field["date_publish"] <= {"date_missing"}
            and {"title", "maintext"} <= passed_fields
        )
        only_expected_evidence_failure = (
            set(evidence_failed_by_field) <= {"date_publish"}
            and all(codes <= {"date_publish_evidence_missing"} for codes in evidence_failed_by_field.values())
        )
        declared = {
            key: exclusion_evidence.get(key)
            for key in ("publisher_url", "final_url", "canonical_url", "redirect_status")
        }
        redirect_matches = bool(
            declared["publisher_url"]
            == "https://www.malaysiakini.com/news/884389-international-press-freedom-award-2000"
            and declared["final_url"] == "https://cpj.org/awards/awards00/"
            and declared["canonical_url"] == declared["final_url"]
            and declared["redirect_status"] == 301
            and any(
                isinstance(item, dict)
                and all(item.get(key) == value for key, value in declared.items())
                for item in evidence.get("publisher_redirects", [])
            )
        )
        valid = bool(
            only_expected_article_failure
            and only_expected_evidence_failure
            and evidence.get("titles")
            and evidence.get("bodies")
            and redirect_matches
        )
        return valid, {"mode": policy_type, "redirect_verified": redirect_matches} if valid else None

    if policy_type == "publisher_social_embed_only" and reason == "excluded_social_embed_only_publisher_item":
        allowed_maintext = {"maintext_missing", "maintext_too_short"}
        only_expected_article_failure = (
            set(failed_by_field) == {"maintext"}
            and bool(failed_by_field["maintext"])
            and failed_by_field["maintext"] <= allowed_maintext
            and {"title", "date_publish"} <= passed_fields
        )
        only_expected_evidence_failure = (
            set(evidence_failed_by_field) <= {"maintext"}
            and all(codes <= {"maintext_evidence_missing"} for codes in evidence_failed_by_field.values())
        )
        declared = {
            key: exclusion_evidence.get(key)
            for key in ("provider", "selector", "status_url", "embedded_text")
        }
        embed_matches = bool(
            declared["provider"] == "twitter"
            and declared["selector"]
            and declared["status_url"]
            and re.match(r"https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/\d+", str(declared["status_url"]))
            and declared["embedded_text"]
            and any(
                isinstance(item, dict)
                and all(item.get(key) == value for key, value in declared.items())
                for item in evidence.get("publisher_social_embeds", [])
            )
        )
        valid = bool(
            only_expected_article_failure
            and only_expected_evidence_failure
            and evidence.get("titles")
            and evidence.get("dates")
            and exclusion_evidence.get("ordinary_paragraph_count") == 0
            and embed_matches
        )
        return valid, {"mode": policy_type, "embed_verified": embed_matches} if valid else None

    if policy_type == "publisher_video_only" and reason == "excluded_video_only_publisher_item":
        allowed_maintext = {"maintext_missing", "maintext_too_short"}
        only_expected_article_failure = (
            set(failed_by_field) == {"maintext"}
            and bool(failed_by_field["maintext"])
            and failed_by_field["maintext"] <= allowed_maintext
            and {"title", "date_publish"} <= passed_fields
        )
        only_expected_evidence_failure = (
            set(evidence_failed_by_field) <= {"maintext"}
            and all(codes <= {"maintext_evidence_missing"} for codes in evidence_failed_by_field.values())
        )
        declared = {
            key: exclusion_evidence.get(key)
            for key in ("provider", "selector", "embed_url", "thumbnail_url", "content_text_empty")
        }
        video_matches = bool(
            declared["provider"] == "youtube"
            and declared["selector"]
            and re.match(r"https://www\.youtube\.com/embed/[A-Za-z0-9_-]+", str(declared["embed_url"] or ""))
            and "ytimg.com/" in str(declared["thumbnail_url"] or "")
            and declared["content_text_empty"] is True
            and any(
                isinstance(item, dict)
                and all(item.get(key) == value for key, value in declared.items())
                for item in evidence.get("publisher_videos", [])
            )
        )
        valid = bool(
            only_expected_article_failure
            and only_expected_evidence_failure
            and evidence.get("titles")
            and evidence.get("dates")
            and video_matches
        )
        return valid, {"mode": policy_type, "video_verified": video_matches} if valid else None

    if policy_type == "publisher_link_only" and reason == "excluded_link_only_publisher_item":
        allowed_maintext = {"maintext_missing", "maintext_too_short"}
        only_expected_article_failure = (
            set(failed_by_field) == {"maintext"}
            and bool(failed_by_field["maintext"])
            and failed_by_field["maintext"] <= allowed_maintext
            and {"title", "date_publish"} <= passed_fields
        )
        only_expected_evidence_failure = (
            set(evidence_failed_by_field) <= {"maintext"}
            and all(codes <= {"maintext_evidence_missing"} for codes in evidence_failed_by_field.values())
        )
        declared = {
            key: exclusion_evidence.get(key)
            for key in ("target_url", "selector", "link_text", "content_text_empty")
        }
        link_matches = bool(
            isinstance(declared["target_url"], str)
            and declared["target_url"].startswith("https://epaper.pakobserver.net/")
            and declared["selector"]
            and declared["link_text"]
            and declared["content_text_empty"] is True
            and any(
                isinstance(item, dict)
                and all(item.get(key) == value for key, value in declared.items())
                for item in evidence.get("publisher_links", [])
            )
        )
        valid = bool(
            only_expected_article_failure
            and only_expected_evidence_failure
            and evidence.get("titles")
            and evidence.get("dates")
            and link_matches
        )
        return valid, {"mode": policy_type, "link_verified": link_matches} if valid else None

    if policy_type == "publisher_category" and reason == "legacy_uninterested_category":
        only_expected_article_failure = (
            set(failed_by_field) == {"maintext"}
            and failed_by_field["maintext"] == {"maintext_too_short"}
            and {"title", "date_publish"} <= passed_fields
        )
        declared_categories = exclusion_evidence.get("categories")
        matched_values = exclusion_evidence.get("matched_policy_values")
        category_matches = bool(
            isinstance(declared_categories, list)
            and isinstance(matched_values, list)
            and matched_values
            and set(matched_values) <= set(declared_categories)
            and declared_categories == evidence.get("publisher_categories")
        )
        valid = bool(
            only_expected_article_failure
            and not evidence_failed
            and evidence.get("titles")
            and evidence.get("dates")
            and evidence.get("bodies")
            and exclusion_evidence.get("source")
            and category_matches
        )
        return valid, {"mode": policy_type, "category_verified": category_matches} if valid else None

    if policy_type == "publisher_image_only" and reason == "excluded_image_only_publisher_item":
        allowed_maintext = {"maintext_missing", "maintext_too_short"}
        title_failure_is_short = (
            "title" not in failed_by_field
            or failed_by_field["title"] == {"title_too_short"}
        )
        only_expected_article_failure = (
            set(failed_by_field) <= {"maintext", "title"}
            and "maintext" in failed_by_field
            and bool(failed_by_field["maintext"])
            and failed_by_field["maintext"] <= allowed_maintext
            and title_failure_is_short
            and "date_publish" in passed_fields
        )
        only_expected_evidence_failure = (
            set(evidence_failed_by_field) <= {"maintext"}
            and all(codes <= {"maintext_evidence_missing"} for codes in evidence_failed_by_field.values())
        )
        publisher_images = evidence.get("publisher_images") or []
        declared_image = exclusion_evidence.get("publisher_image_url")
        image_matches = bool(
            declared_image
            and any(item.get("url") == declared_image for item in publisher_images if isinstance(item, dict))
        )
        gallery_verified = True
        if exclusion_evidence.get("publisher_item_type") == "photo_gallery":
            gallery_verified = bool(
                exclusion_evidence.get("publisher_gallery_selector")
                and isinstance(exclusion_evidence.get("gallery_item_count"), int)
                and exclusion_evidence["gallery_item_count"] >= 2
                and exclusion_evidence.get("structured_article_body_empty") is True
                and any(
                    isinstance(item, dict)
                    and item.get("selector") == exclusion_evidence["publisher_gallery_selector"]
                    and item.get("item_count") == exclusion_evidence["gallery_item_count"]
                    and item.get("image_url") == declared_image
                    and item.get("structured_article_body_empty") is True
                    for item in evidence.get("publisher_galleries", [])
                )
            )
        valid = bool(
            only_expected_article_failure
            and only_expected_evidence_failure
            and evidence.get("titles")
            and evidence.get("dates")
            and ("title" in passed_fields or not evidence_failed_by_field.get("title"))
            and image_matches
            and gallery_verified
            and exclusion_evidence.get("publisher_dom_selector")
        )
        return valid, {"mode": policy_type, "image_verified": image_matches} if valid else None

    if policy_type == "publisher_tombstone" and reason == "publisher_removed_url":
        title_signals = " ".join(
            str(item.get("raw") or "") for item in evidence.get("titles", []) if isinstance(item, dict)
        ).casefold()
        declared_title = str(exclusion_evidence.get("publisher_page_title") or "").casefold()
        tombstone_signal = any(marker in title_signals for marker in ("404", "sayfa bulunamadı", "page not found"))
        valid = bool(
            tombstone_signal
            and declared_title
            and declared_title in title_signals
            and exclusion_evidence.get("sitemap_url")
            and exclusion_evidence.get("page_state") == "publisher_removed"
        )
        return valid, {"mode": policy_type, "tombstone_signal": title_signals[:240]} if valid else None

    if policy_type == "publisher_http_gone" and reason == "publisher_http_gone":
        # A URL returned by the publisher's complete discovery API can later
        # be deliberately withdrawn with HTTP 410.  Keep the record auditable
        # without inventing title, publication date, or article text.  This
        # policy requires repeated direct observations plus an exact WordPress
        # slug lookup; generic 4xx responses do not qualify.
        expected_article_failures = {
            "date_publish": {"date_missing"},
            "title": {"title_missing"},
            "maintext": {"maintext_missing"},
        }
        article_failures_match = bool(
            set(failed_by_field) == set(expected_article_failures)
            and all(failed_by_field[field] == codes for field, codes in expected_article_failures.items())
        )
        expected_evidence_failures = {
            "date_publish": {"date_publish_evidence_missing"},
            "title": {"title_evidence_missing"},
            "maintext": {"maintext_evidence_missing"},
        }
        evidence_failures_match = bool(
            set(evidence_failed_by_field) == set(expected_evidence_failures)
            and all(
                evidence_failed_by_field[field] == codes
                for field, codes in expected_evidence_failures.items()
            )
        )
        publisher_url = exclusion_evidence.get("publisher_url")
        artifact_url = article_url if isinstance(article_url, str) else ""
        publisher_path = urlparse(str(publisher_url or ""))
        publisher_slug = publisher_path.path.rstrip("/").rsplit("/", 1)[-1]
        observations = evidence.get("publisher_http_observations") or []
        declared_observations = exclusion_evidence.get("publisher_http_observations") or []
        repeated_gone = bool(
            isinstance(publisher_url, str)
            and publisher_url == artifact_url
            and publisher_path.scheme == "https"
            and publisher_path.netloc == "thesun.my"
            and bool(publisher_slug)
            and len(observations) >= 3
            and observations == declared_observations
            and len({item.get("observed_at") for item in observations if isinstance(item, dict)}) >= 3
            and all(
                isinstance(item, dict)
                and item.get("url") == publisher_url
                and item.get("status_code") == 410
                and item.get("observed_at")
                and item.get("direct_request") is True
                and item.get("history_count") == 0
                for item in observations
            )
        )
        lookup = exclusion_evidence.get("wordpress_slug_lookup")
        lookup_endpoint = str(lookup.get("endpoint") or "") if isinstance(lookup, dict) else ""
        parsed_lookup = urlparse(lookup_endpoint)
        lookup_query = parse_qs(parsed_lookup.query, keep_blank_values=True)
        exact_lookup_query = {
            "slug": [publisher_slug],
            "_fields": ["id,slug,status,link,date"],
            "per_page": ["10"],
        }
        lookup_matches = bool(
            isinstance(lookup, dict)
            and lookup == evidence.get("wordpress_slug_lookup")
            and lookup.get("status_code") == 200
            and lookup.get("direct_request") is True
            and lookup.get("history_count") == 0
            and lookup.get("slug") == publisher_slug
            and parsed_lookup.scheme == "https"
            and parsed_lookup.netloc == "thesun.my"
            and parsed_lookup.path == "/wp-json/wp/v2/posts"
            and not parsed_lookup.params
            and not parsed_lookup.fragment
            and lookup_query == exact_lookup_query
            and isinstance(lookup.get("records"), list)
            and all(
                isinstance(item, dict)
                and item.get("slug") == lookup.get("slug")
                and bool(str(item.get("status") or "").strip())
                and str(item.get("status") or "").strip().lower() != "publish"
                and str(item.get("link") or "").rstrip("/") == publisher_url.rstrip("/")
                for item in lookup["records"]
            )
        )
        valid = bool(
            article_failures_match
            and evidence_failures_match
            and evidence.get("http_status") == 410
            and repeated_gone
            and lookup_matches
        )
        return valid, {
            "mode": policy_type,
            "repeated_http_410_verified": repeated_gone,
            "wordpress_slug_lookup_verified": lookup_matches,
        } if valid else None

    if (
        policy_type == "thesun_requested_window_spillover"
        and reason == "thesun_requested_window_spillover"
    ):
        # The theSun REST query deliberately overlaps both ends of the
        # requested interval. A real article in that overlap is an explicit
        # discovery spillover only when its complete page evidence confirms
        # the parsed publication day. No other field failure is tolerated.
        article_failures_match = bool(
            set(failed_by_field) == {"date_publish"}
            and failed_by_field["date_publish"] == {"date_out_of_range"}
            and {"title", "maintext"} <= passed_fields
        )
        evidence_by_field = {item.field: item for item in evidence_checks}
        evidence_passes = bool(
            set(evidence_by_field) == {"title", "date_publish", "maintext"}
            and all(item.status == "pass" for item in evidence_by_field.values())
            and not evidence_failed
        )
        article_date = _parse_calendar_date(
            next(
                (
                    item.evidence.get("raw")
                    for item in article_check.fields
                    if item.field == "date_publish"
                ),
                None,
            )
        )
        try:
            requested_start = date.fromisoformat(str(exclusion_evidence.get("requested_start") or ""))
            requested_end = date.fromisoformat(str(exclusion_evidence.get("requested_end") or ""))
        except ValueError:
            requested_start = requested_end = None
        position = (
            "before_window" if article_date and requested_start and article_date < requested_start
            else "after_window" if article_date and requested_end and article_date > requested_end
            else None
        )
        matching_signals = [
            dict(item)
            for item in evidence.get("dates", [])
            if (
                isinstance(item, dict)
                and item.get("kind") == "published"
                and item.get("source") == "jsonld.datePublished"
                and _parse_calendar_date(item.get("raw")) == article_date
            )
        ]
        exact_field_sources = {
            "title": "h1.tdb-title-text",
            "date_publish": "jsonld.datePublished",
            "maintext": ".td-post-content",
        }
        source_signals_match = bool(
            exclusion_evidence.get("publisher_field_sources") == exact_field_sources
            and any(
                isinstance(item, dict)
                and item.get("kind") == "visible_h1"
                and item.get("source") == exact_field_sources["title"]
                and str(item.get("raw") or "").strip()
                for item in evidence.get("titles", [])
            )
            and any(
                isinstance(item, dict)
                and item.get("kind") == exact_field_sources["maintext"]
                and str(item.get("text") or "").strip()
                for item in evidence.get("bodies", [])
            )
        )
        declared_url = exclusion_evidence.get("publisher_url")
        declared_matches = bool(
            isinstance(declared_url, str)
            and declared_url == article_url
            and re.fullmatch(r"https://(?:www\.)?thesun\.my/.+", declared_url)
            and article_date
            and requested_start
            and requested_end
            and requested_start <= requested_end
            and exclusion_evidence.get("article_date") == article_date.isoformat()
            and exclusion_evidence.get("window_position") == position
            and exclusion_evidence.get("http_status") == 200
            and exclusion_evidence.get("publisher_date_signals") == matching_signals
        )
        valid = bool(
            article_failures_match
            and evidence_passes
            and evidence.get("http_status") == 200
            and matching_signals
            and source_signals_match
            and position
            and declared_matches
        )
        return valid, {
            "mode": policy_type,
            "publisher_date_verified": bool(matching_signals),
            "publisher_sources_verified": source_signals_match,
            "window_position": position,
        } if valid else None

    if (
        policy_type == "thesun_exact_removed_redirect"
        and reason == "thesun_exact_removed_redirect"
    ):
        exact_url = (
            "https://thesun.my/news/malaysia-news/people-issues/"
            "pmo-lodges-police-report-over-fake-letter-on-plus-widening-project"
        )
        expected_article_failures = {
            "title": {"title_missing"},
            "date_publish": {"date_missing"},
            "maintext": {"maintext_missing"},
        }
        expected_evidence_failures = {
            "title": {"title_evidence_missing"},
            "date_publish": {"date_publish_evidence_missing"},
            "maintext": {"maintext_evidence_missing"},
        }
        article_failures_match = bool(
            set(failed_by_field) == set(expected_article_failures)
            and all(failed_by_field[key] == codes for key, codes in expected_article_failures.items())
        )
        evidence_failures_match = bool(
            set(evidence_failed_by_field) == set(expected_evidence_failures)
            and all(evidence_failed_by_field[key] == codes for key, codes in expected_evidence_failures.items())
        )
        observations = evidence.get("publisher_redirect_observations")
        lookup = evidence.get("wordpress_slug_lookup")
        expected_slug = "pmo-lodges-police-report-over-fake-letter-on-plus-widening-project"
        repeated_redirect = bool(
            exclusion_evidence.get("publisher_url") == exact_url
            and article_url == exact_url
            and exclusion_evidence.get("publisher_redirect_observations") == observations
            and isinstance(observations, list)
            and len(observations) >= 3
            and len({item.get("observed_at") for item in observations if isinstance(item, dict)}) >= 3
            and all(
                isinstance(item, dict)
                and item.get("url") == exact_url
                and item.get("response_url") == exact_url
                and item.get("status_code") == 301
                and item.get("location") in {"https://thesun.my", "https://thesun.my/"}
                and item.get("response_bytes") == 0
                and item.get("response_sha256") == hashlib.sha256(b"").hexdigest()
                and item.get("direct_request") is True
                and item.get("history_count") == 0
                and item.get("observed_at")
                for item in observations
            )
        )
        lookup_endpoint = str(lookup.get("endpoint") or "") if isinstance(lookup, dict) else ""
        parsed_lookup = urlparse(lookup_endpoint)
        lookup_query = parse_qs(parsed_lookup.query, keep_blank_values=True)
        exact_lookup_query = {
            "slug": [expected_slug],
            "_fields": ["id,slug,status,link,date"],
            "per_page": ["10"],
        }
        slug_lookup_verified = bool(
            isinstance(lookup, dict)
            and exclusion_evidence.get("wordpress_slug_lookup") == lookup
            and lookup.get("status_code") == 200
            and lookup.get("direct_request") is True
            and lookup.get("history_count") == 0
            and lookup.get("slug") == expected_slug
            and lookup.get("records") == []
            and parsed_lookup.scheme == "https"
            and parsed_lookup.netloc == "thesun.my"
            and parsed_lookup.path == "/wp-json/wp/v2/posts"
            and not parsed_lookup.params
            and not parsed_lookup.fragment
            and lookup_query == exact_lookup_query
        )
        valid = bool(
            article_failures_match
            and evidence_failures_match
            and evidence.get("http_status") == 301
            and evidence.get("titles") == []
            and evidence.get("dates") == []
            and evidence.get("bodies") == []
            and repeated_redirect
            and slug_lookup_verified
        )
        return valid, {
            "mode": policy_type,
            "repeated_redirect_verified": repeated_redirect,
            "wordpress_slug_lookup_verified": slug_lookup_verified,
        } if valid else None

    if policy_type == "publisher_unallocated_numeric_slot" and reason == "publisher_unallocated_numeric_slot":
        declared_url = str(exclusion_evidence.get("publisher_url") or "")
        slot_evidence = evidence.get("publisher_empty_numeric_slots", [])
        expected_failures = {
            "title": {"title_missing"},
            "date_publish": {"date_missing"},
            "maintext": {"maintext_missing"},
        }
        article_failures_match = bool(
            set(failed_by_field) == set(expected_failures)
            and all(failed_by_field[field] == codes for field, codes in expected_failures.items())
        )
        evidence_failures_match = bool(
            set(evidence_failed_by_field) <= set(expected_failures)
            and all(
                codes <= {f"{field}_evidence_missing"}
                for field, codes in evidence_failed_by_field.items()
            )
        )
        numeric_slot = bool(re.fullmatch(r"https://jang\.com\.pk/news/\d+", declared_url))
        empty_verified = bool(
            numeric_slot
            and exclusion_evidence.get("http_status") == 200
            and exclusion_evidence.get("response_bytes") == 0
            and int(exclusion_evidence.get("empty_observations", 0) or 0) >= 4
            and any(
                isinstance(item, dict)
                and item.get("publisher_url") == declared_url
                and item.get("http_status") == 200
                and item.get("response_bytes") == 0
                and int(item.get("empty_observations", 0) or 0) >= 4
                for item in slot_evidence
            )
        )
        valid = bool(article_failures_match and evidence_failures_match and empty_verified)
        return valid, {"mode": policy_type, "empty_slot_verified": empty_verified} if valid else None

    if policy_type == "publisher_incomplete_placeholder" and reason == "publisher_incomplete_placeholder":
        only_expected_article_failure = (
            set(failed_by_field) == {"maintext"}
            and failed_by_field["maintext"] == {"maintext_too_short"}
            and {"title", "date_publish"} <= passed_fields
        )
        only_expected_evidence_failure = not evidence_failed
        declared_text = _normalize(str(exclusion_evidence.get("publisher_placeholder_text") or ""))
        declared_marker = _normalize(str(exclusion_evidence.get("publisher_placeholder_marker") or ""))
        declared_selector = str(exclusion_evidence.get("publisher_dom_selector") or "")
        allowed_texts = {"ayrıntılar geliyor", "detaylar geliyor", "details coming", "more details soon"}
        normalized_declared = re.sub(r"[^\w]+", " ", declared_text.casefold(), flags=re.UNICODE).strip()
        normalized_marker = re.sub(r"[^\w]+", " ", declared_marker.casefold(), flags=re.UNICODE).strip()
        body_matches = bool(
            normalized_marker in allowed_texts
            and normalized_marker in normalized_declared
            and declared_selector
            and any(
                re.sub(r"[^\w]+", " ", str(item.get("text") or "").casefold(), flags=re.UNICODE).strip()
                == normalized_declared
                for item in evidence.get("bodies", [])
                if isinstance(item, dict)
            )
        )
        valid = bool(
            only_expected_article_failure
            and only_expected_evidence_failure
            and evidence.get("titles")
            and evidence.get("dates")
            and body_matches
            and evidence.get("http_status") == 200
            and str(exclusion_evidence.get("publisher_url") or "").startswith("https://www.sabah.com.tr/")
        )
        return valid, {"mode": policy_type, "placeholder_verified": body_matches} if valid else None

    if policy_type == "publisher_empty_article" and reason == "excluded_publisher_empty_article":
        # NST occasionally keeps a published article URL while its own article
        # payload declares zero words and replaces the body with a copyright
        # line.  This is a publisher-side empty record, not usable main text.
        only_expected_article_failure = (
            set(failed_by_field) == {"maintext"}
            and failed_by_field["maintext"] == {"maintext_too_short"}
            and {"title", "date_publish"} <= passed_fields
        )
        only_expected_evidence_failure = not evidence_failed
        declared = {
            key: exclusion_evidence.get(key)
            for key in (
                "publisher_url", "embedded_nid", "embedded_status",
                "embedded_word_count", "embedded_body_text",
                "embedded_inline_body_empty",
            )
        }
        normalized_body = _normalize(str(declared["embedded_body_text"] or ""))
        empty_record_matches = bool(
            isinstance(declared["publisher_url"], str)
            and declared["publisher_url"].startswith("https://www.nst.com.my/")
            and isinstance(declared["embedded_nid"], int)
            and declared["embedded_status"] == 1
            and declared["embedded_word_count"] == 0
            and normalized_body == "© New Straits Times Press (M) Bhd"
            and declared["embedded_inline_body_empty"] is True
            and any(
                isinstance(item, dict)
                and all(item.get(key) == value for key, value in declared.items())
                for item in evidence.get("publisher_empty_articles", [])
            )
            and any(
                _normalize(str(item.get("text") or "")) == normalized_body
                for item in evidence.get("bodies", [])
                if isinstance(item, dict)
            )
        )
        valid = bool(
            only_expected_article_failure
            and only_expected_evidence_failure
            and evidence.get("titles")
            and evidence.get("dates")
            and evidence.get("http_status") == 200
            and empty_record_matches
        )
        return valid, {"mode": policy_type, "empty_record_verified": empty_record_matches} if valid else None

    if policy_type == "publisher_program_announcement" and reason == "excluded_program_announcement":
        only_expected_article_failure = (
            set(failed_by_field) == {"maintext"}
            and failed_by_field["maintext"] == {"maintext_too_short"}
            and {"title", "date_publish"} <= passed_fields
        )
        declared = {
            key: exclusion_evidence.get(key)
            for key in (
                "publisher_url", "publisher_dom_selector", "publisher_body_text",
                "publisher_cta", "publisher_item_type",
            )
        }
        normalized_body = _normalize(str(declared["publisher_body_text"] or ""))
        normalized_cta = _normalize(str(declared["publisher_cta"] or ""))
        announcement_matches = bool(
            isinstance(declared["publisher_url"], str)
            and declared["publisher_url"].startswith("https://n1info.rs/najave-emisija/")
            and declared["publisher_dom_selector"] == "article.article-wrapper"
            and declared["publisher_item_type"] == "live_program_announcement"
            and normalized_cta == "Pridružite se diskusiji ili pročitajte komentare"
            and normalized_body.endswith(normalized_cta)
            and len(normalized_body.split()) <= 12
            and any(
                _normalize(str(item.get("raw") or "")).casefold().startswith("gledajte")
                for item in evidence.get("titles", [])
                if isinstance(item, dict)
            )
            and any(
                _normalize(str(item.get("text") or "")) == normalized_body
                for item in evidence.get("bodies", [])
                if isinstance(item, dict)
            )
            and any(
                isinstance(item, dict)
                and all(item.get(key) == value for key, value in declared.items())
                for item in evidence.get("publisher_program_announcements", [])
            )
        )
        valid = bool(
            only_expected_article_failure
            and not evidence_failed
            and evidence.get("titles")
            and evidence.get("dates")
            and evidence.get("http_status") == 200
            and announcement_matches
        )
        return valid, {"mode": policy_type, "announcement_verified": announcement_matches} if valid else None

    return False, None


def _normalize(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _parse_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str) and value.strip():
        candidate = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_calendar_date(value: object) -> Optional[date]:
    """Parse an ISO-like publisher value without shifting its wall-clock day."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def check_date(value: object, start: date, end: date, now: Optional[datetime] = None) -> FieldCheck:
    now = now or datetime.now(timezone.utc)
    parsed = _parse_datetime(value)
    codes: List[str] = []
    if parsed is None:
        codes.append("date_missing" if value in (None, "") else "date_unparseable")
        return FieldCheck("date_publish", "fail", 0.0, codes, {"raw": value})
    if parsed > now + timedelta(hours=24):
        codes.append("date_future")
    if not start <= parsed.date() <= end:
        codes.append("date_out_of_range")
    status = "fail" if codes else "pass"
    return FieldCheck("date_publish", status, 1.0 if not codes else 0.2, codes, {"raw": str(value), "normalized_utc": parsed.isoformat()})


def check_title(value: object, site_name: Optional[str] = None) -> FieldCheck:
    title = _normalize(value if isinstance(value, str) else None)
    codes: List[str] = []
    lowered = title.lower()
    if not title:
        codes.append("title_missing")
    elif len(title) < 8:
        codes.append("title_too_short")
    if TITLE_CHALLENGE.fullmatch(title):
        codes.append("challenge_title")
    if site_name and lowered == _normalize(site_name).lower():
        codes.append("template_title")
    return FieldCheck("title", "fail" if codes else "pass", 1.0 if not codes else 0.1, codes, {"length": len(title), "preview": title[:240]})


def check_maintext(value: object, minimum_words: int = 40, minimum_paragraphs: int = 2) -> FieldCheck:
    text = _normalize(value if isinstance(value, str) else None)
    words = re.findall(r"\b\w+\b", text, re.UNICODE)
    raw = value if isinstance(value, str) else ""
    paragraphs = [part for part in re.split(r"\n\s*\n|\r\n\s*\r\n", raw) if part.strip()]
    codes: List[str] = []
    if not text:
        codes.append("maintext_missing")
    elif len(words) < minimum_words:
        codes.append("maintext_too_short")
    lowered = text.lower()
    if len(text) < 400 and any(marker in lowered for marker in (
        "her türlü telif hakkı", "all rights reserved", "tous droits réservés",
    )):
        codes.append("boilerplate_maintext")
    # Challenge bodies are short interstitials.  Long news reports can quote
    # phrases such as "just a moment ago" or "access denied" legitimately.
    if len(text) < 500 and any(pattern in lowered[:500] for pattern in CHALLENGE_PATTERNS):
        codes.append("wrong_container")
    return FieldCheck(
        "maintext", "fail" if codes else "pass", 1.0 if not codes else 0.2, codes,
        {
            "characters": len(text), "words": len(words), "paragraphs": len(paragraphs),
            "paragraph_structure_available": "\n" in raw or "\r" in raw,
            "preview": text[:320],
        },
    )


def validate_article(article: Dict[str, object], start: date, end: date, site_name: Optional[str] = None) -> ArticleCheck:
    checks = [
        check_date(article.get("date_publish"), start, end),
        check_title(article.get("title"), site_name),
        check_maintext(article.get("maintext")),
    ]
    return ArticleCheck(all(check.status == "pass" for check in checks), checks)
