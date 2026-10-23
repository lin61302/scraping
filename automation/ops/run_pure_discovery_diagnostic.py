#!/usr/bin/env python3
"""Run one hash-pinned, discovery-only pure-adapter diagnostic.

The diagnostic imports one reviewed production-disabled adapter and calls only
``discover_urls(day, day, evidence)``.  ``scrape_url`` is replaced by a
fail-closed sentinel for the duration of the call.  The adapter's shared
``bounded_request`` helper is wrapped so the terminal artifact proves the
number and class of requests without recording raw URLs or response bodies.

This command has no production-write mode and no database surface.  Its
artifact is private review evidence; it cannot advance a tracker boundary.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from xml.etree import ElementTree


HEX64 = re.compile(r"^[0-9a-f]{64}$")
URL_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
URL_KEY = re.compile(r"url|uri|href|link|endpoint", re.IGNORECASE)
FORBIDDEN_IMPORTS = frozenset({"pymongo", "mongoengine", "motor"})
FORBIDDEN_CALLS = frozenset({
    "insert_one", "insert_many", "update_one", "update_many",
    "delete_one", "delete_many", "bulk_write",
})


class DiagnosticError(RuntimeError):
    """The requested diagnostic is unsafe or failed its exact contract."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def url_set_sha256(urls: Iterable[str]) -> str:
    payload = "".join("%s\n" % value for value in sorted(set(urls)))
    return sha256_bytes(payload.encode("utf-8"))


def write_json_exclusive(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def assert_pure_adapter(path: Path) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise DiagnosticError("adapter cannot be parsed") from exc
    imported = set()
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    if imported & FORBIDDEN_IMPORTS or called & FORBIDDEN_CALLS:
        raise DiagnosticError("adapter failed the pure-network static gate")


def load_adapter(path: Path, automation_root: Path):
    assert_pure_adapter(path)
    sys.path.insert(0, str(automation_root))
    name = "discovery_diagnostic_%s" % sha256_path(path)[:16]
    spec = importlib.util.spec_from_file_location(name, str(path))
    if not spec or not spec.loader:
        raise DiagnosticError("adapter import specification is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "discover_urls", None)):
        raise DiagnosticError("adapter has no discover_urls contract")
    if not callable(getattr(module, "scrape_url", None)):
        raise DiagnosticError("adapter has no scrape_url contract")
    config_type = getattr(module, "Config", None)
    if config_type is None:
        raise DiagnosticError("adapter has no Config contract")
    config = config_type()
    if getattr(config, "production_writes", None) is not False:
        raise DiagnosticError("adapter is not explicitly production-write-disabled")
    if getattr(module, "PRODUCTION_WRITES", False) is not False:
        raise DiagnosticError("adapter module exposes an unsafe production-write flag")
    return module, config


def structural_class(payload: bytes) -> str:
    prefix = payload[:65536].lstrip()
    if not prefix:
        return "empty"
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        root = None
    if root is not None:
        return "xml:%s" % root.tag.rsplit("}", 1)[-1].casefold()
    # Keep the HTML marker scan on raw bytes.  ``bytes`` supports ``lower``
    # (ASCII-safe for these markers), whereas ``casefold`` is text-only.
    lowered = prefix[:2048].lower()
    if b"<!doctype html" in lowered or b"<html" in lowered:
        return "html"
    try:
        json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "opaque"
    return "json"


def _redacted_value(value: object) -> Mapping[str, str]:
    encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return {"redacted_sha256": sha256_bytes(encoded)}


def sanitize(value: object, key_hint: Optional[str] = None) -> object:
    """Recursively remove URL fields and URL substrings from evidence."""

    if key_hint is not None and URL_KEY.search(key_hint):
        return _redacted_value(value)
    if isinstance(value, Mapping):
        result = {}  # type: Dict[str, object]
        for key, item in value.items():
            raw_key = str(key)
            safe_key = URL_TEXT.sub(
                lambda match: "url_key_sha256_%s" % sha256_bytes(
                    match.group(0).encode("utf-8")
                )[:16],
                raw_key,
            )
            result[safe_key] = sanitize(item, raw_key)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return URL_TEXT.sub(
            lambda match: "[url_sha256:%s]" % sha256_bytes(
                match.group(0).encode("utf-8")
            ),
            value,
        )
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _bounded_request_owner(module: object) -> object:
    common = getattr(module, "common", None)
    if common is None or not callable(getattr(common, "bounded_request", None)):
        raise DiagnosticError("adapter does not expose its reviewed bounded_request helper")
    return common


def _bound_request_aliases(module: object, original: Callable[..., object]) -> list[str]:
    discover = getattr(module, "discover_urls")
    defaults = list(getattr(discover, "__defaults__", ()) or ())
    defaults.extend((getattr(discover, "__kwdefaults__", {}) or {}).values())
    closure = getattr(discover, "__closure__", None) or ()
    for cell in closure:
        try:
            defaults.append(cell.cell_contents)
        except ValueError:
            continue
    if any(value is original for value in defaults):
        raise DiagnosticError("discover_urls captured an unpatchable bounded_request alias")
    return [
        name for name, value in vars(module).items()
        if value is original
    ]


def run_diagnostic(
    *,
    automation_root: Path,
    source_file: Path,
    source_sha256: str,
    diagnostic_day: date,
    output_path: Path,
    expected_requests: int,
    expected_robots: int,
    expected_inventory: int,
) -> Dict[str, object]:
    automation_root = automation_root.resolve()
    source_file = source_file.resolve()
    if not HEX64.fullmatch(source_sha256) or sha256_path(source_file) != source_sha256:
        raise DiagnosticError("adapter SHA-256 mismatch")
    if min(expected_requests, expected_robots, expected_inventory) < 0:
        raise DiagnosticError("request bounds must be nonnegative")
    if expected_requests != expected_robots + expected_inventory:
        raise DiagnosticError("request class bounds do not sum to the total")

    started = iso_now()
    module, config = load_adapter(source_file, automation_root)
    common = _bounded_request_owner(module)
    original_request = common.bounded_request
    original_scrape = module.scrape_url
    alias_names = _bound_request_aliases(module, original_request)
    alias_originals = {name: getattr(module, name) for name in alias_names}
    robots_url = str(getattr(module, "ROBOTS_URL", ""))
    request_records = []  # type: list[Dict[str, object]]
    scrape_url_calls = []  # type: list[str]

    def counted_request(url: str, **kwargs: object):
        kind = "robots" if robots_url and url == robots_url else "inventory"
        record = {
            "sequence": len(request_records) + 1,
            "kind": kind,
            "url_sha256": sha256_bytes(url.encode("utf-8")),
            "configured_max_bytes": kwargs.get("max_bytes"),
        }  # type: Dict[str, object]
        request_records.append(record)
        try:
            response = original_request(url, **kwargs)
        except Exception as exc:
            record.update({"outcome": "exception", "exception_type": type(exc).__name__})
            raise
        payload = bytes(getattr(response, "content", b""))
        record.update({
            "outcome": "response",
            "status_code": getattr(response, "status_code", None),
            "response_bytes": len(payload),
            "response_sha256": sha256_bytes(payload),
            "structural_class": structural_class(payload),
        })
        return response

    def forbidden_scrape(url: str):
        scrape_url_calls.append(sha256_bytes(str(url).encode("utf-8")))
        raise DiagnosticError("discovery attempted an article fetch through scrape_url")

    evidence = {}  # type: Dict[str, object]
    urls = []  # type: list[str]
    status = "failed"
    fatal_error = None  # type: Optional[Dict[str, str]]
    try:
        common.bounded_request = counted_request
        for name in alias_names:
            setattr(module, name, counted_request)
        module.scrape_url = forbidden_scrape
        discovered = module.discover_urls(diagnostic_day, diagnostic_day, evidence)
        urls = list(dict.fromkeys(str(value).strip() for value in discovered if str(value).strip()))
        robots_count = sum(1 for row in request_records if row["kind"] == "robots")
        inventory_count = sum(1 for row in request_records if row["kind"] == "inventory")
        if scrape_url_calls:
            raise DiagnosticError("scrape_url was invoked")
        if len(request_records) != expected_requests:
            raise DiagnosticError("total request count differed from its exact bound")
        if robots_count != expected_robots or inventory_count != expected_inventory:
            raise DiagnosticError("request class count differed from its exact bound")
        status = "observed"
    except Exception as exc:
        fatal_error = {
            "type": type(exc).__name__,
            "message_sha256": sha256_bytes(str(exc).encode("utf-8")),
            "traceback_sha256": sha256_bytes(traceback.format_exc().encode("utf-8")),
        }
    finally:
        common.bounded_request = original_request
        for name, value in alias_originals.items():
            setattr(module, name, value)
        module.scrape_url = original_scrape

    robots_count = sum(1 for row in request_records if row["kind"] == "robots")
    inventory_count = sum(1 for row in request_records if row["kind"] == "inventory")
    payload = {
        "schema_version": 1,
        "kind": "pure_discovery_diagnostic",
        "mode": "observation_no_production_writes",
        "production_writes": False,
        "tracker_boundary_advancement": False,
        "started_at_utc": started,
        "finished_at_utc": iso_now(),
        "status": status,
        "source": str(getattr(config, "source_name", "")),
        "country": str(getattr(config, "country_code", "")),
        "source_sha256": source_sha256,
        "requested_start": diagnostic_day.isoformat(),
        "requested_end": diagnostic_day.isoformat(),
        "operation": "discover_urls(day,day)_only",
        "bounded_request_interception": {
            "owner_patched": True,
            "bound_alias_count": len(alias_names),
            "bound_alias_names": sorted(alias_names),
        },
        "selected": len(urls),
        "selected_url_set_sha256": url_set_sha256(urls),
        "scrape_url_calls": len(scrape_url_calls),
        "request_accounting": {
            "expected": {
                "total": expected_requests,
                "robots": expected_robots,
                "inventory": expected_inventory,
                "article": 0,
            },
            "observed": {
                "total": len(request_records),
                "robots": robots_count,
                "inventory": inventory_count,
                "article": len(scrape_url_calls),
            },
            "requests": request_records,
        },
        "discovery_evidence": sanitize(evidence),
        "fatal_error": fatal_error,
    }  # type: Dict[str, object]
    write_json_exclusive(output_path.resolve(), payload)
    return payload


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--automation-root", required=True, type=Path)
    result.add_argument("--source-file", required=True, type=Path)
    result.add_argument("--source-sha256", required=True)
    result.add_argument("--day", required=True, type=date.fromisoformat)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--expected-requests", required=True, type=int)
    result.add_argument("--expected-robots", required=True, type=int)
    result.add_argument("--expected-inventory", required=True, type=int)
    return result


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run_diagnostic(
            automation_root=args.automation_root,
            source_file=args.source_file,
            source_sha256=args.source_sha256,
            diagnostic_day=args.day,
            output_path=args.output,
            expected_requests=args.expected_requests,
            expected_robots=args.expected_robots,
            expected_inventory=args.expected_inventory,
        )
    except Exception as exc:
        print(json.dumps({"status": "refused", "error": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({
        "status": result["status"],
        "selected": result["selected"],
        "output_sha256": sha256_path(args.output),
    }, sort_keys=True))
    return 0 if result["status"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
