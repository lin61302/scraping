"""Transport byte caps for production-disabled candidate adapters."""

from __future__ import annotations

import gzip
from pathlib import Path
import sys

import pytest


AUTOMATION = Path(__file__).resolve().parents[1]
if str(AUTOMATION) not in sys.path:
    sys.path.insert(0, str(AUTOMATION))

from adapters import candidate_common as common  # noqa: E402


class Response:
    def __init__(self, chunks: list[bytes], *, content_length: str | None = None):
        self.status_code = 200
        self.url = "https://example.test/item"
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self._chunks = chunks
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size: int):
        assert chunk_size == 64 * 1024
        yield from self._chunks

    def close(self):
        self.closed = True

    @property
    def content(self):
        return self._content


def test_bounded_request_streams_and_preserves_small_payload(monkeypatch):
    response = Response([b"<title>News</title>", b"body"])
    monkeypatch.setattr(common, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(common.requests, "get", lambda *args, **kwargs: response)
    result = common.bounded_request(
        "https://example.test/item", allowed_hosts=("example.test",), max_bytes=100,
    )
    assert result.content == b"<title>News</title>body"
    assert result.scrape_transport == "requests_stream_bounded"
    assert response.closed is True


@pytest.mark.parametrize(
    "response",
    [Response([b"small"], content_length="101"), Response([b"a" * 60, b"b" * 41])],
)
def test_bounded_request_closes_before_or_during_oversize_body(monkeypatch, response):
    monkeypatch.setattr(common, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(common.requests, "get", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError, match="byte cap"):
        common.bounded_request(
            "https://example.test/item", allowed_hosts=("example.test",), max_bytes=100,
        )
    assert response.closed is True


def test_gzip_sitemap_decompression_is_bounded(monkeypatch):
    monkeypatch.setattr(common, "MAX_SITEMAP_BYTES", 128)
    payload = gzip.compress(b"<urlset>" + b"x" * 256 + b"</urlset>")
    with pytest.raises(RuntimeError, match="decompressed byte cap"):
        common.parse_sitemap(payload)
