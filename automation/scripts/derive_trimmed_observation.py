#!/usr/bin/env python3
"""Derive a shorter immutable observation from a completed daily-sitemap run.

This is allowed only when every newly excluded tail day failed before exposing
any URLs and every retained day has a successful sitemap record. Article fields
and page evidence are revalidated against the shorter date window.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scraper_control.evidence import compare_evidence
from scraper_control.validation import resolve_with_page_evidence, validate_article


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def days(start: date, end: date):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def derive(parent: Path, new_end: date, output_root: Path) -> Path:
    parent_manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    if parent_manifest.get("status") != "observed":
        raise ValueError("Parent observation is not complete")
    start = date.fromisoformat(parent_manifest["requested_start"])
    old_end = date.fromisoformat(parent_manifest["requested_end"])
    if not start <= new_end < old_end:
        raise ValueError("New end must be inside the parent window and before its end")
    if parent_manifest.get("counts", {}).get("selected") != parent_manifest.get("counts", {}).get("discovered"):
        raise ValueError("Sampled parent observations cannot be derived")
    if parent_manifest.get("counts", {}).get("exceptions"):
        raise ValueError("Parent contains parse exceptions")

    discovery = json.loads(json.dumps(parent_manifest.get("discovery_evidence") or {}))
    checked = discovery.get("sitemaps_checked") or []
    failures = discovery.get("sitemap_failures") or []
    if not checked or any("day" not in row for row in checked) or any("day" not in row for row in failures):
        raise ValueError("Derivation requires dated sitemap evidence for every check and failure")
    retained_days = {item.isoformat() for item in days(start, new_end)}
    checked_days = {row["day"] for row in checked}
    if not retained_days <= checked_days:
        raise ValueError(f"Retained sitemap coverage is incomplete: {sorted(retained_days - checked_days)}")
    if any(row["day"] in retained_days for row in failures):
        raise ValueError("A retained day has a sitemap failure")
    if any(row["day"] > new_end.isoformat() for row in checked):
        raise ValueError("Cannot separate URLs from successful sitemap days after the new end")
    removed_tail_days = {item.isoformat() for item in days(new_end + timedelta(days=1), old_end)}
    if not failures or any(row["day"] not in removed_tail_days for row in failures):
        raise ValueError("Only failed tail days may be trimmed")
    discovery["sitemap_failures"] = []
    discovery["trimmed_failed_tail"] = failures

    urls = read_jsonl(parent / "urls.jsonl")
    articles = read_jsonl(parent / "articles.jsonl")
    old_checks = read_jsonl(parent / "field_checks.jsonl")
    if len(urls) != len(articles) or len(urls) != len(old_checks):
        raise ValueError("Parent JSONL accounting is incomplete")
    article_by_url = {row["url"]: row for row in articles}
    check_by_url = {row["url"]: row for row in old_checks}

    counts = {"discovered": len(urls), "selected": len(urls), "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}
    new_checks = []
    issues = []
    for row in urls:
        url = row["url"]
        article = article_by_url[url]
        old_check = check_by_url[url]
        evidence = old_check.get("page_evidence") or {}
        base = validate_article(article, start, new_end, parent_manifest["source"])
        evidence_checks = compare_evidence(article, evidence) if evidence else []
        semantic_eligible, resolutions = resolve_with_page_evidence(base, evidence_checks, evidence)
        excluded = bool(
            article.get("collection") == "SKIP" and semantic_eligible
            and article.get("exclusion_reason") and article.get("exclusion_evidence")
        )
        eligible = bool(semantic_eligible and not excluded and not article.get("parser_error"))
        payload = {
            "url": url, "eligible": eligible, "excluded": excluded,
            "fields": [asdict(item) for item in [*base.fields, *evidence_checks]],
            "page_evidence": evidence, "automatic_resolutions": resolutions,
            "exclusion_reason": article.get("exclusion_reason"),
            "exclusion_evidence": article.get("exclusion_evidence"),
        }
        new_checks.append(payload)
        if excluded:
            counts["skipped"] += 1
        elif eligible:
            counts["passed"] += 1
        else:
            counts["quarantined"] += 1
            for field in [*base.fields, *evidence_checks]:
                for code in field.codes:
                    issues.append({
                        "url": url, "source": parent_manifest["source"], "stage": "derived_validation",
                        "category": field.field, "code": code, "severity": "error",
                        "status": "open", "observed": field.evidence, "attempt": 1,
                    })

    run_id = f"observe-derived-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(parent / "articles.jsonl", run_dir / "articles.jsonl")
    shutil.copy2(parent / "urls.jsonl", run_dir / "urls.jsonl")
    write_jsonl(run_dir / "field_checks.jsonl", new_checks)
    if issues:
        write_jsonl(run_dir / "issues.jsonl", issues)
    manifest = dict(parent_manifest)
    manifest.update({
        "run_id": run_id,
        "requested_end": new_end.isoformat(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": 0.0,
        "status": "observed",
        "counts": counts,
        "discovery_evidence": discovery,
        "derived_from_run_id": parent_manifest["run_id"],
        "derivation": "trim_failed_daily_sitemap_tail_and_revalidate_all_evidence",
    })
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(
        f"# Derived observation for {manifest['source']}\n\n"
        f"- Parent: `{parent_manifest['run_id']}`\n- Run: `{run_id}`\n"
        f"- Window: {start} through {new_end}\n- Selected: {len(urls)}\n"
        f"- Passed: {counts['passed']}\n- Quarantined: {counts['quarantined']}\n",
        encoding="utf-8",
    )
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-run", required=True, type=Path)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    print(derive(args.parent_run, args.end, args.output_root))


if __name__ == "__main__":
    main()
