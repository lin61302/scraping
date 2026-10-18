#!/usr/bin/env python3
"""Clear a stale missing-maintext marker from fully verified media exclusions.

The parent discovery and page evidence stay immutable. A new observation is
created only when the current adapter bundle still matches the parent and the
policy resolver independently verifies every repaired exclusion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scraper_control.evidence import compare_required_evidence
from scraper_control.observe import adapter_bundle_sha256
from scraper_control.validation import resolve_policy_exclusion, resolve_with_page_evidence, validate_article


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def derive(parent: Path, output_root: Path) -> Path:
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    counts = manifest.get("counts") or {}
    if manifest.get("status") != "observed" or counts.get("selected") != counts.get("discovered"):
        raise ValueError("Parent must be a complete full observation")
    if counts.get("exceptions"):
        raise ValueError("Parent has parse exceptions")
    source_file = Path(manifest["source_file"])
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != manifest.get("source_sha256"):
        raise ValueError("Adapter changed after the parent observation")
    if adapter_bundle_sha256(source_file) != manifest.get("source_bundle_sha256"):
        raise ValueError("Adapter bundle changed after the parent observation")

    urls = read_jsonl(parent / "urls.jsonl")
    articles = read_jsonl(parent / "articles.jsonl")
    old_checks = read_jsonl(parent / "field_checks.jsonl")
    if len(urls) != len(articles) or len(urls) != len(old_checks) or len(urls) != counts["selected"]:
        raise ValueError("Parent artifacts do not account for every selected URL")
    article_by_url = {row["url"]: row for row in articles}
    check_by_url = {row["url"]: row for row in old_checks}
    start = date.fromisoformat(manifest["requested_start"])
    end = date.fromisoformat(manifest["requested_end"])

    repairs: list[dict] = []
    new_articles: list[dict] = []
    new_checks: list[dict] = []
    issues: list[dict] = []
    new_counts = {"discovered": len(urls), "selected": len(urls), "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}
    for row in urls:
        url = row["url"]
        article = dict(article_by_url[url])
        recorded = check_by_url[url]
        evidence = recorded.get("page_evidence") or {}
        base = validate_article(article, start, end, manifest["source"])
        evidence_checks = compare_required_evidence(article, evidence) if evidence else []
        exclusion_valid, exclusion_resolution = resolve_policy_exclusion(
            base, evidence_checks, evidence, article.get("exclusion_reason"), article.get("exclusion_evidence"),
            article_url=article.get("url"),
        )
        stale_marker = article.get("parser_error") == "missing:maintext"
        if stale_marker:
            if not (
                article.get("collection") == "SKIP" and recorded.get("excluded")
                and exclusion_valid and exclusion_resolution == recorded.get("exclusion_resolution")
            ):
                raise ValueError(f"Parser marker is not backed by an identical verified exclusion: {url}")
            article["parser_error"] = None
            repairs.append({"url": url, "cleared": "missing:maintext", "policy": article.get("exclusion_reason")})
            base = validate_article(article, start, end, manifest["source"])
            evidence_checks = compare_required_evidence(article, evidence) if evidence else []
            exclusion_valid, exclusion_resolution = resolve_policy_exclusion(
                base, evidence_checks, evidence, article.get("exclusion_reason"), article.get("exclusion_evidence"),
                article_url=article.get("url"),
            )

        semantic_eligible, resolutions = resolve_with_page_evidence(base, evidence_checks, evidence)
        excluded = bool(article.get("collection") == "SKIP" and exclusion_valid and not article.get("parser_error"))
        eligible = bool(semantic_eligible and article.get("collection") != "SKIP" and not article.get("parser_error"))
        payload = {
            "url": url, "eligible": eligible, "fields": [asdict(item) for item in [*base.fields, *evidence_checks]],
            "page_evidence": evidence, "automatic_resolutions": resolutions, "excluded": excluded,
            "exclusion_reason": article.get("exclusion_reason"),
            "exclusion_evidence": article.get("exclusion_evidence"),
            "exclusion_resolution": exclusion_resolution,
        }
        if excluded:
            new_counts["skipped"] += 1
            issues.append({"url": url, "source": manifest["source"], "stage": "source_policy", "category": "collection", "code": article.get("exclusion_reason"), "severity": "info", "status": "excluded", "observed": article.get("exclusion_evidence"), "attempt": 1})
        elif eligible:
            new_counts["passed"] += 1
        else:
            new_counts["quarantined"] += 1
            for item in [*base.fields, *evidence_checks]:
                for code in item.codes:
                    issues.append({"url": url, "source": manifest["source"], "stage": "derived_validation", "category": item.field, "code": code, "severity": "error", "status": "open", "observed": item.evidence, "attempt": 1})
        new_articles.append(article)
        new_checks.append(payload)

    if not repairs:
        raise ValueError("Parent has no verified exclusion with a stale missing-maintext marker")
    run_id = f"observe-derived-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(parent / "urls.jsonl", run_dir / "urls.jsonl")
    write_jsonl(run_dir / "articles.jsonl", new_articles)
    write_jsonl(run_dir / "field_checks.jsonl", new_checks)
    write_jsonl(run_dir / "issues.jsonl", issues)
    now = datetime.now(timezone.utc).isoformat()
    child = dict(manifest)
    child.update({"run_id": run_id, "started_at": now, "finished_at": now, "runtime_seconds": 0.0, "counts": new_counts, "derived_from_run_id": manifest["run_id"], "derivation": "clear_stale_parser_marker_from_verified_policy_exclusion", "repairs": repairs})
    (run_dir / "manifest.json").write_text(json.dumps(child, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(f"# Derived verified-exclusion repair\n\n- Parent: `{manifest['run_id']}`\n- Run: `{run_id}`\n- Repairs: {len(repairs)}\n- Passed / skipped / quarantined: {new_counts['passed']} / {new_counts['skipped']} / {new_counts['quarantined']}\n", encoding="utf-8")
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    print(derive(args.parent_run, args.output_root))


if __name__ == "__main__":
    main()
