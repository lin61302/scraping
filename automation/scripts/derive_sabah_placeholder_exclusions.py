#!/usr/bin/env python3
"""Derive an immutable Sabah run with verified publisher placeholders excluded."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper_control.observe import adapter_bundle_sha256
from scraper_control.recover import _validate_one


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _load_adapter(path: Path):
    spec = importlib.util.spec_from_file_location("derived_sabah_adapter", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def derive(parent: Path, output_root: Path) -> Path:
    parent = parent.resolve()
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    counts = manifest.get("counts") or {}
    if manifest.get("status") != "observed" or counts.get("selected") != counts.get("discovered"):
        raise ValueError("Parent is not a complete observation")
    if counts.get("exceptions"):
        raise ValueError("Parent has parse exceptions")

    source_file = Path(manifest["source_file"]).resolve()
    adapter = _load_adapter(source_file)
    targets = set(adapter.INCOMPLETE_PLACEHOLDER_URLS)
    marker_for = adapter._publisher_placeholder

    urls = _read_jsonl(parent / "urls.jsonl")
    articles = _read_jsonl(parent / "articles.jsonl")
    checks = _read_jsonl(parent / "field_checks.jsonl")
    if not (len(urls) == len(articles) == len(checks) == counts["selected"]):
        raise ValueError("Parent artifacts do not account for every selected URL")
    ordered_urls = [row["url"] for row in urls]
    article_by_url = {row["url"]: row for row in articles}
    if len(article_by_url) != len(ordered_urls) or set(ordered_urls) != set(article_by_url):
        raise ValueError("Parent URL/article sets differ")

    start = date.fromisoformat(manifest["requested_start"])
    end = date.fromisoformat(manifest["requested_end"])
    expected_targets = {
        url for url in targets
        if url in article_by_url and start <= date.fromisoformat(str(article_by_url[url]["date_publish"])[:10]) <= end
    }
    repaired = []
    new_articles = []
    new_checks = []
    new_issues = []
    new_counts = {"discovered": len(urls), "selected": len(urls), "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}

    for url in ordered_urls:
        article = dict(article_by_url[url])
        if url in expected_targets:
            marker = marker_for(article.get("maintext"))
            if not marker:
                raise ValueError(f"Expected publisher placeholder marker is absent: {url}")
            article.update({
                "collection": "SKIP",
                "parser_error": None,
                "exclusion_reason": "publisher_incomplete_placeholder",
                "exclusion_evidence": {
                    "policy_type": "publisher_incomplete_placeholder",
                    "page_state": "publisher_incomplete",
                    "publisher_placeholder_text": article.get("maintext"),
                    "publisher_placeholder_marker": marker,
                    "publisher_dom_selector": ((article.get("field_evidence") or {}).get("bodies") or [{}])[0].get("kind"),
                    "publisher_url": url,
                },
            })
            repaired.append(url)
        check, issues, bucket = _validate_one(article, url=url, source=manifest["source"], start=start, end=end)
        new_articles.append(article)
        new_checks.append(check)
        new_issues.extend(issues)
        new_counts[bucket] += 1

    if set(repaired) != expected_targets or not repaired:
        raise ValueError(f"Placeholder repair accounting differs: repaired={len(repaired)} expected={len(expected_targets)}")

    now = datetime.now(timezone.utc)
    run_id = f"observe-derived-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    child = output_root / run_id
    child.mkdir()
    shutil.copy2(parent / "urls.jsonl", child / "urls.jsonl")
    _write_jsonl(child / "articles.jsonl", new_articles)
    _write_jsonl(child / "field_checks.jsonl", new_checks)
    _write_jsonl(child / "issues.jsonl", new_issues)
    child_manifest = dict(manifest)
    child_manifest.update({
        "run_id": run_id,
        "mode": "observation_derived_no_production_writes",
        "source_sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
        "source_bundle_sha256": adapter_bundle_sha256(source_file),
        "started_at": now.isoformat(),
        "finished_at": now.isoformat(),
        "runtime_seconds": 0.0,
        "counts": new_counts,
        "derived_from_run_id": manifest["run_id"],
        "derived_from_manifest_sha256": hashlib.sha256((parent / "manifest.json").read_bytes()).hexdigest(),
        "derivation": "classify_exact_reviewed_sabah_publisher_placeholders",
        "repairs": [{"url": url, "policy": "publisher_incomplete_placeholder"} for url in repaired],
    })
    (child / "manifest.json").write_text(json.dumps(child_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (child / "summary.md").write_text(
        "# Derived Sabah publisher-placeholder exclusions\n\n"
        f"- Parent: `{manifest['run_id']}`\n- Run: `{run_id}`\n"
        f"- Verified placeholders excluded: {len(repaired)}\n"
        f"- Passed / skipped / quarantined: {new_counts['passed']} / {new_counts['skipped']} / {new_counts['quarantined']}\n",
        encoding="utf-8",
    )
    return child


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    print(derive(args.parent_run, args.output_root))


if __name__ == "__main__":
    main()
