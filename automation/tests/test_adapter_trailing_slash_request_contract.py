"""Offline contracts for the six reviewed first-party trailing-slash repairs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from urllib.parse import urlparse

import pytest


AUTOMATION = Path(__file__).resolve().parents[2] / "automation"
if str(AUTOMATION) not in sys.path:
    sys.path.insert(0, str(AUTOMATION))

from adapters import candidate_common as common
from adapters.LBR import liberianinvestigator
from adapters.MWI import malawi247
from adapters.PAN import newsroompanama
from adapters.SLB import sibconline
from adapters.SSD import eyeradio
from adapters.UGA import eagle

ARTICLE_PAYLOAD = b"deterministic mocked article response"


@dataclass(frozen=True)
class Case:
    source_id: str
    module: object
    article_url: str


CASES = (
    Case(
        "mlp:LBR:liberianinvestigator.com",
        liberianinvestigator,
        "https://liberianinvestigator.com/contract-liberian-investigator",
    ),
    Case(
        "mlp:MWI:247malawi.com",
        malawi247,
        "https://www.247malawi.com/contract-247-malawi",
    ),
    Case(
        "mlp:PAN:newsroompanama.com",
        newsroompanama,
        "https://newsroompanama.com/2026/09/30/contract-newsroom-panama",
    ),
    Case(
        "mlp:SLB:sibconline.com.sb",
        sibconline,
        "https://www.sibconline.com.sb/contract-sibc-online",
    ),
    Case(
        "mlp:SSD:eyeradio.org",
        eyeradio,
        "https://eyeradio.org/contract-eye-radio",
    ),
    Case(
        "mlp:UGA:eagle.co.ug",
        eagle,
        "https://eagle.co.ug/2026/09/30/contract-eagle-online",
    ),
)


class Response:
    def __init__(self, url: str, payload: bytes):
        self.url = url
        self.content = payload
        self.status_code = 200
        self.scrape_transport = "offline_fixture"


class RedirectResponse:
    def __init__(self, status_code: int, location: str):
        self.status_code = status_code
        self.headers = {"Location": location}
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.source_id)
def test_scrape_requests_one_slash_and_retains_original_identity(monkeypatch, case):
    monkeypatch.setenv("SCRAPER_BOUNDED_OBSERVATION", "1")
    module = case.module
    article_request_url = case.article_url + "/"
    calls = []
    parse_calls = []

    def request(url: str, *, max_bytes: int, timeout: int = 45):
        calls.append((url, max_bytes, timeout))
        if url == module.ROBOTS_URL:
            payload = b"User-agent: *\nAllow: /\n"
        elif url == article_request_url:
            payload = ARTICLE_PAYLOAD
        else:
            raise AssertionError("unexpected fixture URL %s" % url)
        assert len(payload) <= max_bytes
        return Response(url, payload)

    def parse_article(url: str, payload: bytes, *, response_url: str | None = None):
        parse_calls.append((url, payload, response_url))
        return {
            "url": url,
            "parser_error": None,
            "field_evidence": {
                "canonical": url,
                "response_url": response_url,
            },
        }

    authorized_urls = []
    require_allowed = common.require_allowed

    def record_authorization(robots, url: str) -> None:
        authorized_urls.append(url)
        require_allowed(robots, url)

    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(module, "_parse_article_html", parse_article)
    monkeypatch.setattr(common, "require_allowed", record_authorization)

    article = module.scrape_url(case.article_url)

    assert [url for url, _, _ in calls] == [module.ROBOTS_URL, article_request_url]
    assert authorized_urls == [article_request_url]
    assert parse_calls == [(case.article_url, ARTICLE_PAYLOAD, article_request_url)]
    assert urlparse(article_request_url).path.endswith("/")
    assert not urlparse(article_request_url).path.endswith("//")
    assert article["url"] == case.article_url
    assert article["field_evidence"]["canonical"] == case.article_url
    assert article["field_evidence"]["response_url"] == article_request_url
    assert article["parser_error"] is None


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.source_id)
@pytest.mark.parametrize(
    "response_shape",
    ("off-host", "same-host-non-slash"),
)
def test_scrape_rejects_unexpected_article_response_url(monkeypatch, case, response_shape):
    monkeypatch.setenv("SCRAPER_BOUNDED_OBSERVATION", "1")
    module = case.module
    article_request_url = case.article_url + "/"
    response_url = (
        "https://unreviewed.invalid/off-host/"
        if response_shape == "off-host"
        else case.article_url
    )
    calls = []

    def request(url: str, *, max_bytes: int, timeout: int = 45):
        calls.append((url, max_bytes, timeout))
        if url == module.ROBOTS_URL:
            return Response(url, b"User-agent: *\nAllow: /\n")
        if url == article_request_url:
            return Response(response_url, ARTICLE_PAYLOAD)
        raise AssertionError("unexpected fixture URL %s" % url)

    def parse_forbidden(*args, **kwargs):
        raise AssertionError("unexpected response URL reached the article parser")

    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(module, "_parse_article_html", parse_forbidden)

    with pytest.raises(common.AccessBlockedError, match="unexpected article response URL"):
        module.scrape_url(case.article_url)

    assert [url for url, _, _ in calls] == [module.ROBOTS_URL, article_request_url]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.source_id)
@pytest.mark.parametrize(
    ("status_code", "redirect_location"),
    (
        (301, "https://unreviewed.invalid/off-host/"),
        (308, None),
    ),
    ids=("off-host", "same-host-non-slash"),
)
def test_adapter_request_rejects_unreviewed_redirect_shapes(
    monkeypatch,
    case,
    status_code,
    redirect_location,
):
    request_url = case.article_url + "/"
    redirect_location = redirect_location or case.article_url
    response = RedirectResponse(status_code, redirect_location)
    calls = []

    def get(url: str, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(common, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(common.requests, "get", get)

    with pytest.raises(common.AccessBlockedError, match="unreviewed redirect"):
        case.module._request(request_url, max_bytes=common.MAX_ARTICLE_BYTES)

    assert calls == [
        (
            request_url,
            {
                "timeout": 45,
                "allow_redirects": False,
                "stream": True,
                "headers": {
                    "User-Agent": common.USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.1",
                },
            },
        )
    ]
    assert response.headers["Location"] == redirect_location
    assert response.closed is True
