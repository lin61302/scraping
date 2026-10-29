"""Focused conservative repairs for the sealed PAN, SSD, and UGA residuals."""
from __future__ import annotations

import hashlib
import html
import json

import pytest

from adapters import candidate_common as common
from adapters.PAN import newsroompanama
from adapters.SSD import eyeradio
from adapters.UGA import eagle


def _paragraph(label: str) -> str:
    return (label + " publisher evidence remains source local and independently reviewable. ") * 4


def _page(
    module,
    url: str,
    *,
    visible_title: str,
    structured_title: str,
    item_type: str = "NewsArticle",
    visible_body: str | None = None,
    structured_body: str | None = None,
    meta_date: str = "2026-09-30T08:00:00+02:00",
    visible_date: str = "2026-09-30T08:00:00+02:00",
    structured_date: str = "2026-09-30T08:00:00+02:00",
    extra_items: list[dict] | None = None,
) -> bytes:
    visible_body = visible_body or _paragraph("Visible article body")
    structured_body = structured_body or visible_body
    item = {
        "@context": "https://schema.org",
        "@type": item_type,
        "url": url,
        "mainEntityOfPage": {"@id": url},
        "headline": structured_title,
        "datePublished": structured_date,
        "inLanguage": "en",
        "articleSection": ["News"],
        "articleBody": structured_body,
    }
    items = [*(extra_items or []), item]
    if module is newsroompanama:
        body = (
            "<article><p>Generic article chrome should not be selected as body text because a "
            "specific content container is present for this publisher.</p>"
            f"<div class='entry-content'><p>{html.escape(visible_body)}</p></div></article>"
        )
    elif module is eyeradio:
        body = f"<article class='single-article'><div class='single-content'><p>{html.escape(visible_body)}</p></div></article>"
    else:
        body = f"<article><div class='td-post-content'><p>{html.escape(visible_body)}</p></div></article>"
    return f"""<!doctype html>
    <html lang='en'><head>
      <link rel='canonical' href='{url}/'>
      <meta property='og:title' content='{html.escape(visible_title)}'>
      <meta property='article:published_time' content='{meta_date}'>
      <script type='application/ld+json'>{json.dumps({'@context': 'https://schema.org', '@graph': items})}</script>
    </head><body><main><h1>{html.escape(visible_title)}</h1>
      <time itemprop='datePublished' datetime='{visible_date}'></time>{body}
    </main></body></html>""".encode()


@pytest.mark.parametrize(
    ("module", "url"),
    (
        (newsroompanama, "https://newsroompanama.com/2026/09/30/synthetic-title"),
        (eagle, "https://eagle.co.ug/2026/09/30/synthetic-title"),
    ),
)
def test_article_title_reconciles_only_presentation_differences(module, url):
    payload = _page(
        module,
        url,
        visible_title="“Climate &amp; Resilience” — Update",
        structured_title='climate & resilience - update',
    )
    article = module._parse_article_html(url, payload, response_url=url + "/")
    assert article["parser_error"] is None
    assert article["field_evidence"]["title_reconciliation"] == {
        "method": "unicode_nfkc_entity_whitespace_case_and_punctuation_only",
        "matched": True,
        "semantic_edits_allowed": False,
    }


@pytest.mark.parametrize(
    ("module", "url"),
    (
        (newsroompanama, "https://newsroompanama.com/2026/09/30/semantic-title"),
        (eagle, "https://eagle.co.ug/2026/09/30/semantic-title"),
    ),
)
def test_article_title_semantic_disagreement_remains_error(module, url):
    payload = _page(
        module,
        url,
        visible_title="Government approves climate plan",
        structured_title="Government rejects climate plan",
    )
    article = module._parse_article_html(url, payload, response_url=url + "/")
    assert "visible_vs_jsonld_title_disagreement" in article["parser_error"]
    assert article["field_evidence"]["title_reconciliation"]["matched"] is False


def test_pan_prefers_specific_content_container():
    url = "https://newsroompanama.com/2026/09/30/specific-container"
    desired = _paragraph("Specific publisher body")
    article = newsroompanama._parse_article_html(
        url,
        _page(
            newsroompanama,
            url,
            visible_title="Specific container",
            structured_title="Specific container",
            visible_body=desired,
            structured_body=desired,
        ),
        response_url=url + "/",
    )
    assert article["parser_error"] is None
    assert article["field_sources"]["maintext"] == "main article .entry-content"
    assert "Generic article chrome" not in article["maintext"]


def test_pan_uses_long_jsonld_body_only_with_strict_visible_containment():
    url = "https://newsroompanama.com/2026/09/30/structured-fallback"
    visible = "This publisher lead is long enough to provide strict normalized containment evidence."
    structured = visible + " " + _paragraph("Additional publisher article text")
    article = newsroompanama._parse_article_html(
        url,
        _page(
            newsroompanama,
            url,
            visible_title="Structured fallback",
            structured_title="Structured fallback",
            visible_body=visible,
            structured_body=structured,
        ),
        response_url=url + "/",
    )
    assert article["parser_error"] is None
    assert article["maintext"] == structured.strip()
    assert article["field_sources"]["maintext"] == "jsonld.articleBody_strict_visible_containment_fallback"
    evidence = article["field_evidence"]["structured_body_fallback"]
    assert evidence["applied"] is True
    assert evidence["normalized_visible_contained"] is True
    assert evidence["visible_word_containment"] == 1.0


@pytest.mark.parametrize(
    "structured",
    (
        "Too short to establish a supported article body fallback.",
        _paragraph("Semantically unrelated replacement copy"),
    ),
    ids=("short-structured-body", "no-visible-containment"),
)
def test_pan_unsupported_jsonld_body_keeps_visible_maintext_error(structured):
    url = "https://newsroompanama.com/2026/09/30/rejected-fallback"
    visible = "This short publisher lead cannot by itself satisfy the visible article body contract."
    article = newsroompanama._parse_article_html(
        url,
        _page(
            newsroompanama,
            url,
            visible_title="Rejected fallback",
            structured_title="Rejected fallback",
            visible_body=visible,
            structured_body=structured,
        ),
        response_url=url + "/",
    )
    assert "insufficient_visible_maintext" in article["parser_error"]
    assert article["field_evidence"]["structured_body_fallback"]["applied"] is False


def _ssd_page(url: str, **kwargs) -> bytes:
    return _page(
        eyeradio,
        url,
        item_type="BlogPosting",
        visible_title=kwargs.pop("visible_title", "“Peace &amp; Dialogue” — Update"),
        structured_title=kwargs.pop("structured_title", "peace & dialogue - update"),
        **kwargs,
    )


def test_ssd_selects_unique_canonical_blogposting_and_same_day_date_evidence():
    url = "https://eyeradio.org/synthetic-canonical-story"
    unrelated = {
        "@type": "BlogPosting",
        "url": "https://eyeradio.org/unrelated-story",
        "headline": "Unrelated story",
        "datePublished": "2026-09-28T10:00:00+02:00",
        "inLanguage": "en",
    }
    article = eyeradio._parse_article_html(
        url,
        _ssd_page(
            url,
            extra_items=[unrelated],
            meta_date="2026-09-30T08:00:00+02:00",
            visible_date="2026-09-30T09:30:00+02:00",
            structured_date="2026-09-30T18:45:00+02:00",
        ),
        response_url=url + "/",
    )
    assert article["parser_error"] is None
    assert article["field_evidence"]["blogposting_selection"] == {
        "candidate_count": 2,
        "canonical_match_count": 1,
        "selection": "unique_canonical_match",
    }
    date_evidence = article["field_evidence"]["publication_date_reconciliation"]
    assert date_evidence["matched_same_publisher_calendar_day"] is True
    assert date_evidence["publisher_calendar_day"] == "2026-09-30"
    assert len(date_evidence["sources"]) == 2


def test_ssd_different_publisher_calendar_days_remain_error():
    url = "https://eyeradio.org/synthetic-date-disagreement"
    article = eyeradio._parse_article_html(
        url,
        _ssd_page(url, structured_date="2026-10-01T00:05:00+02:00"),
        response_url=url + "/",
    )
    assert "meta_vs_blogposting_publication_date_disagreement" in article["parser_error"]
    assert article["field_evidence"]["publication_date_reconciliation"]["matched_same_publisher_calendar_day"] is False


def test_ssd_ambiguous_blogposting_and_semantic_title_remain_errors():
    url = "https://eyeradio.org/synthetic-ambiguous-story"
    ambiguous = {
        "@type": "BlogPosting",
        "url": url,
        "headline": "Different semantic story",
        "datePublished": "2026-09-30T08:00:00+02:00",
        "inLanguage": "en",
    }
    payload = _ssd_page(
        url,
        structured_title="Rejected semantic headline",
        extra_items=[ambiguous],
    )
    article = eyeradio._parse_article_html(url, payload, response_url=url + "/")
    assert "visible_vs_blogposting_headline_disagreement" in article["parser_error"]
    assert article["field_evidence"]["blogposting_selection"]["selection"] == "none_or_ambiguous"


class _Response:
    def __init__(
        self,
        url: str,
        *,
        status_code: int = 200,
        content: bytes = b"",
        location: str | None = None,
    ):
        self.url = url
        self.status_code = status_code
        self.content = content
        self.headers = {"Location": location} if location is not None else {}
        self.scrape_transport = "offline_fixture"


def _enable_ssd_redirect(monkeypatch, original: str) -> None:
    monkeypatch.setenv("SCRAPER_BOUNDED_OBSERVATION", "1")
    monkeypatch.setattr(
        eyeradio,
        "REVIEWED_REDIRECT_SOURCE_HASHES",
        frozenset({hashlib.sha256(original.encode()).hexdigest()}),
    )


def test_ssd_reviewed_single_same_host_redirect_preserves_original_identity(monkeypatch):
    original = "https://eyeradio.org/synthetic-reviewed-alias"
    target = "https://eyeradio.org/synthetic-canonical-target"
    _enable_ssd_redirect(monkeypatch, original)
    calls = []
    authorizations = []
    require_allowed = common.require_allowed

    def request(url: str, *, max_bytes: int, timeout: int = 45):
        calls.append(url)
        if url == eyeradio.ROBOTS_URL:
            return _Response(url, content=b"User-agent: *\nAllow: /\n")
        if url == target + "/":
            return _Response(url, content=b"synthetic article")
        raise AssertionError("unexpected request")

    def authorize(robots, url: str):
        authorizations.append(url)
        require_allowed(robots, url)

    monkeypatch.setattr(eyeradio, "_request", request)
    monkeypatch.setattr(
        eyeradio,
        "_request_redirect_probe",
        lambda url, **_: _Response(url, status_code=301, location=target + "/"),
    )
    monkeypatch.setattr(
        eyeradio,
        "_parse_article_html",
        lambda url, payload, response_url=None: {
            "url": url,
            "parser_error": None,
            "field_evidence": {"canonical": url, "response_url": response_url},
        },
    )
    monkeypatch.setattr(common, "require_allowed", authorize)

    article = eyeradio.scrape_url(original)

    assert calls == [eyeradio.ROBOTS_URL, target + "/"]
    assert authorizations == [original + "/", target + "/"]
    assert article["url"] == original
    redirect_evidence = article["field_evidence"]["redirect_repair"]
    assert redirect_evidence["hop_count"] == 1
    assert redirect_evidence["original_identity_preserved"] is True
    assert redirect_evidence["raw_urls_recorded"] is False
    assert "https://" not in json.dumps(redirect_evidence)


@pytest.mark.parametrize(
    "location",
    (
        "https://other.invalid/story/",
        "https://eyeradio.org/",
        "https://eyeradio.org/image.jpg",
        "http://eyeradio.org/story/",
        "https://eyeradio.org/story/?query=1",
        "not-a-url",
    ),
    ids=("off-host", "root", "media", "non-https", "query", "malformed"),
)
def test_ssd_reviewed_redirect_rejects_unsafe_target_shapes(monkeypatch, location):
    original = "https://eyeradio.org/synthetic-reviewed-alias"
    _enable_ssd_redirect(monkeypatch, original)

    def request(url: str, *, max_bytes: int, timeout: int = 45):
        assert url == eyeradio.ROBOTS_URL
        return _Response(url, content=b"User-agent: *\nAllow: /\n")

    monkeypatch.setattr(eyeradio, "_request", request)
    monkeypatch.setattr(
        eyeradio,
        "_request_redirect_probe",
        lambda url, **_: _Response(url, status_code=301, location=location),
    )
    with pytest.raises(common.AccessBlockedError):
        eyeradio.scrape_url(original)


def test_ssd_reviewed_redirect_rejects_second_hop(monkeypatch):
    original = "https://eyeradio.org/synthetic-reviewed-alias"
    target = "https://eyeradio.org/synthetic-canonical-target"
    _enable_ssd_redirect(monkeypatch, original)

    def request(url: str, *, max_bytes: int, timeout: int = 45):
        if url == eyeradio.ROBOTS_URL:
            return _Response(url, content=b"User-agent: *\nAllow: /\n")
        if url == target + "/":
            return _Response("https://eyeradio.org/second-hop/", content=b"not accepted")
        raise AssertionError("unexpected request")

    monkeypatch.setattr(eyeradio, "_request", request)
    monkeypatch.setattr(
        eyeradio,
        "_request_redirect_probe",
        lambda url, **_: _Response(url, status_code=301, location=target + "/"),
    )
    with pytest.raises(common.AccessBlockedError, match="second hop"):
        eyeradio.scrape_url(original)


def test_ssd_unlisted_redirect_identity_never_uses_manual_hop(monkeypatch):
    original = "https://eyeradio.org/synthetic-unlisted-alias"
    monkeypatch.setenv("SCRAPER_BOUNDED_OBSERVATION", "1")
    monkeypatch.setattr(eyeradio, "REVIEWED_REDIRECT_SOURCE_HASHES", frozenset())

    def request(url: str, *, max_bytes: int, timeout: int = 45):
        if url == eyeradio.ROBOTS_URL:
            return _Response(url, content=b"User-agent: *\nAllow: /\n")
        return _Response(url, status_code=301, location="https://eyeradio.org/other/")

    monkeypatch.setattr(eyeradio, "_request", request)
    monkeypatch.setattr(
        eyeradio,
        "_request_redirect_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("manual redirect probe must remain allowlisted")),
    )
    with pytest.raises(common.AccessBlockedError, match="unreviewed redirect"):
        eyeradio.scrape_url(original)


def test_ssd_redirect_probe_is_bounded_and_never_auto_follows(monkeypatch):
    class ProbeResponse(_Response):
        def __init__(self):
            super().__init__(
                "https://eyeradio.org/synthetic-reviewed-alias/",
                status_code=302,
                location="https://eyeradio.org/synthetic-target/",
            )
            self.closed = False

        def close(self):
            self.closed = True

    response = ProbeResponse()
    calls = []

    def get(url: str, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(common, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(common.requests, "get", get)
    observed = eyeradio._request_redirect_probe(
        "https://eyeradio.org/synthetic-reviewed-alias/",
        max_bytes=common.MAX_ARTICLE_BYTES,
    )
    assert observed.status_code == 302
    assert response.closed is True
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["stream"] is True

