from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import run_pure_discovery_diagnostic as diagnostic


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_adapter(
    root: Path,
    *,
    calls_scrape: bool = False,
    plain_text_robots: bool = False,
) -> tuple[Path, Path]:
    automation = root / "automation"
    adapters = automation / "adapters"
    adapters.mkdir(parents=True)
    (adapters / "__init__.py").write_text("", encoding="utf-8")
    adapter = adapters / "source.py"
    if calls_scrape:
        discover_body = " scrape_url('https://example.test/forbidden')\n"
    elif plain_text_robots:
        discover_body = (
            " robots=common.bounded_request(ROBOTS_URL,max_bytes=10)\n"
            " if b'Disallow: /' in robots.content: raise RuntimeError('robots denied')\n"
            " evidence['robots_enforced']=True\n"
            " common.bounded_request('https://example.test/inventory',max_bytes=20)\n"
            " evidence['endpoint']='https://example.test/inventory'\n"
            " return ['https://example.test/a']\n"
        )
    else:
        discover_body = (
            " common.bounded_request(ROBOTS_URL,max_bytes=10)\n"
            " common.bounded_request('https://example.test/inventory',max_bytes=20)\n"
            " evidence['endpoint']='https://example.test/inventory'\n"
            " return ['https://example.test/a']\n"
        )
    adapter.write_text(
        "class Response:\n"
        " status_code=200\n"
        " def __init__(self,url):\n"
        "  self.url=url\n"
        "  self.content=(b'User-agent: *\\nAllow: /\\n' if "
        + repr(plain_text_robots)
        + " and url.endswith('/robots.txt') else "
        "b'<urlset><url><loc>https://example.test/a</loc></url></urlset>')\n"
        "class Common:\n"
        " @staticmethod\n"
        " def bounded_request(url, **kwargs): return Response(url)\n"
        "common=Common()\n"
        "class Config:\n"
        " source_name='example.test'; country_code='TST'; production_writes=False\n"
        "PRODUCTION_WRITES=False\n"
        "ROBOTS_URL='https://example.test/robots.txt'\n"
        "def scrape_url(url): raise AssertionError('article fetch forbidden')\n"
        "def discover_urls(start,end,evidence):\n" + discover_body,
        encoding="utf-8",
    )
    return automation, adapter


def test_discovery_only_runner_counts_requests_and_never_records_raw_urls():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        automation, adapter = fixture_adapter(root)
        output = root / "diagnostic.json"
        result = diagnostic.run_diagnostic(
            automation_root=automation,
            source_file=adapter,
            source_sha256=sha(adapter),
            diagnostic_day=date(2026, 6, 29),
            output_path=output,
            expected_requests=2,
            expected_robots=1,
            expected_inventory=1,
        )
        assert result["status"] == "observed"
        assert result["selected"] == 1
        assert result["scrape_url_calls"] == 0
        assert result["request_accounting"]["observed"] == {
            "total": 2, "robots": 1, "inventory": 1, "article": 0,
        }
        serialized = output.read_text(encoding="utf-8")
        assert "https://example.test" not in serialized
        assert output.stat().st_mode & 0o777 == 0o600


def test_plain_text_robots_keeps_exact_discovery_contract():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        automation, adapter = fixture_adapter(root, plain_text_robots=True)
        output = root / "diagnostic.json"
        result = diagnostic.run_diagnostic(
            automation_root=automation,
            source_file=adapter,
            source_sha256=sha(adapter),
            diagnostic_day=date(2026, 8, 10),
            output_path=output,
            expected_requests=2,
            expected_robots=1,
            expected_inventory=1,
        )

        assert result["status"] == "observed"
        assert result["production_writes"] is False
        assert result["discovery_evidence"]["robots_enforced"] is True
        assert result["scrape_url_calls"] == 0
        assert result["request_accounting"]["observed"] == {
            "total": 2, "robots": 1, "inventory": 1, "article": 0,
        }
        requests = result["request_accounting"]["requests"]
        assert [row["kind"] for row in requests] == ["robots", "inventory"]
        assert requests[0]["structural_class"] == "opaque"
        assert requests[1]["structural_class"] == "xml:urlset"


def test_discovery_calling_scrape_url_fails_closed_with_terminal_artifact():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        automation, adapter = fixture_adapter(root, calls_scrape=True)
        output = root / "diagnostic.json"
        result = diagnostic.run_diagnostic(
            automation_root=automation,
            source_file=adapter,
            source_sha256=sha(adapter),
            diagnostic_day=date(2026, 8, 10),
            output_path=output,
            expected_requests=0,
            expected_robots=0,
            expected_inventory=0,
        )
        assert result["status"] == "failed"
        assert result["scrape_url_calls"] == 1
        assert result["fatal_error"]["type"] == "DiagnosticError"
        assert output.is_file()


def test_adapter_hash_and_request_class_bounds_are_exact():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        automation, adapter = fixture_adapter(root)
        with pytest.raises(diagnostic.DiagnosticError, match="SHA-256"):
            diagnostic.run_diagnostic(
                automation_root=automation,
                source_file=adapter,
                source_sha256="0" * 64,
                diagnostic_day=date(2026, 6, 29),
                output_path=root / "diagnostic.json",
                expected_requests=2,
                expected_robots=1,
                expected_inventory=1,
            )
        with pytest.raises(diagnostic.DiagnosticError, match="do not sum"):
            diagnostic.run_diagnostic(
                automation_root=automation,
                source_file=adapter,
                source_sha256=sha(adapter),
                diagnostic_day=date(2026, 6, 29),
                output_path=root / "diagnostic.json",
                expected_requests=3,
                expected_robots=1,
                expected_inventory=1,
            )


def test_bound_bounded_request_alias_is_intercepted_and_counted():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        automation, adapter = fixture_adapter(root)
        source = adapter.read_text(encoding="utf-8")
        source = source.replace(" common.bounded_request(", " bound_request(")
        source = source.replace("common=Common()\n", "common=Common()\nbound_request=common.bounded_request\n")
        adapter.write_text(source, encoding="utf-8")
        result = diagnostic.run_diagnostic(
            automation_root=automation,
            source_file=adapter,
            source_sha256=sha(adapter),
            diagnostic_day=date(2026, 6, 29),
            output_path=root / "diagnostic.json",
            expected_requests=2,
            expected_robots=1,
            expected_inventory=1,
        )
        assert result["status"] == "observed"
        assert result["bounded_request_interception"]["bound_alias_count"] == 1


def test_unpatchable_default_request_alias_fails_before_discovery():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        automation, adapter = fixture_adapter(root)
        source = adapter.read_text(encoding="utf-8")
        source = source.replace(
            "def discover_urls(start,end,evidence):\n",
            "def discover_urls(start,end,evidence,request=common.bounded_request):\n",
        ).replace(" common.bounded_request(", " request(")
        adapter.write_text(source, encoding="utf-8")
        with pytest.raises(diagnostic.DiagnosticError, match="unpatchable"):
            diagnostic.run_diagnostic(
                automation_root=automation,
                source_file=adapter,
                source_sha256=sha(adapter),
                diagnostic_day=date(2026, 6, 29),
                output_path=root / "diagnostic.json",
                expected_requests=2,
                expected_robots=1,
                expected_inventory=1,
            )


def test_sanitizer_redacts_url_keys_embedded_urls_and_url_mapping_keys():
    value = {
        "canonicalUrl": "https://example.test/a",
        "message": "prefix https://example.test/b?x=1 suffix",
        "https://example.test/key": {"href": "/relative/private/path"},
        "nested": ["safe", "see http://example.test/c"],
    }
    sanitized = diagnostic.sanitize(value)
    encoded = json.dumps(sanitized, sort_keys=True)
    assert "https://" not in encoded
    assert "http://" not in encoded
    assert "example.test" not in encoded
    assert "redacted_sha256" in encoded
    assert "url_key_sha256_" in encoded
