#!/usr/bin/env python3
"""Merge complete date shards and revalidate them over their combined window."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper_control.observe import adapter_bundle_sha256
from scraper_control.recover import _validate_one


ARTIFACTS = ("manifest.json", "urls.jsonl", "articles.jsonl", "field_checks.jsonl", "issues.jsonl")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path} line {line_number} is not an object")
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _find_runs(root: Path) -> list[Path]:
    runs = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        found = sorted(path for path in child.glob("observe-*") if (path / "manifest.json").is_file())
        if len(found) != 1:
            raise ValueError(f"Expected exactly one observation under {child}; found {len(found)}")
        runs.append(found[0])
    if not runs:
        raise ValueError(f"No shard observations found under {root}")
    return runs


def merge_shards(shard_root: Path, output_root: Path) -> Path:
    runs = _find_runs(shard_root.resolve())
    loaded = []
    identity = None
    intervals = []
    for run in runs:
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("status") != "observed" or manifest.get("adapter_contract") != "pure_network_only":
            raise ValueError(f"Shard is not a complete pure observation: {run}")
        counts = manifest.get("counts") or {}
        if counts.get("selected") != counts.get("discovered") or counts.get("exceptions"):
            raise ValueError(f"Shard is sampled or has exceptions: {run}")
        current_identity = tuple(manifest.get(key) for key in (
            "source", "country", "is_env", "source_file", "source_sha256", "source_bundle_sha256",
        ))
        if identity is None:
            identity = current_identity
        elif identity != current_identity:
            raise ValueError(f"Shard source identity or adapter bundle differs: {run}")
        start = date.fromisoformat(manifest["requested_start"])
        end = date.fromisoformat(manifest["requested_end"])
        if end < start:
            raise ValueError(f"Invalid shard interval: {run}")
        intervals.append((start, end, run, manifest))
        loaded.append((run, manifest))

    intervals.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] != previous[1] + timedelta(days=1):
            raise ValueError(f"Shard intervals are not contiguous: {previous[0]}..{previous[1]} then {current[0]}..{current[1]}")
    combined_start, combined_end = intervals[0][0], intervals[-1][1]

    source_file = Path(str(intervals[0][3]["source_file"])).resolve()
    if not source_file.is_file():
        raise ValueError(f"Current adapter does not exist: {source_file}")
    if _sha256(source_file) != intervals[0][3]["source_sha256"]:
        raise ValueError("Adapter changed after the shards ran")
    if adapter_bundle_sha256(source_file) != intervals[0][3]["source_bundle_sha256"]:
        raise ValueError("Adapter dependency bundle changed after the shards ran")

    ordered_urls = []
    article_by_url = {}
    parent_rows = []
    duplicate_urls = set()
    for start, end, run, manifest in intervals:
        urls = _read_jsonl(run / "urls.jsonl")
        articles = _read_jsonl(run / "articles.jsonl")
        checks = _read_jsonl(run / "field_checks.jsonl")
        selected = int(manifest["counts"]["selected"])
        if len(urls) != selected or len(articles) != selected or len(checks) != selected:
            raise ValueError(f"Shard artifacts do not account for every URL: {run}")
        local_urls = [row.get("url") for row in urls]
        local_articles = {row.get("url"): row for row in articles}
        local_checks = {row.get("url"): row for row in checks}
        if len(set(local_urls)) != selected or set(local_urls) != set(local_articles) or set(local_urls) != set(local_checks):
            raise ValueError(f"Shard URL/article/check sets differ: {run}")
        for url in local_urls:
            if url in article_by_url:
                duplicate_urls.add(url)
                earlier = article_by_url[url]
                current = local_articles[url]
                fields = ("title", "date_publish", "maintext", "collection", "exclusion_reason", "exclusion_evidence")
                if any(earlier.get(key) != current.get(key) for key in fields):
                    raise ValueError(f"Duplicate URL changed across shards: {url}")
                continue
            ordered_urls.append(url)
            article_by_url[url] = local_articles[url]
        parent_rows.append({
            "run_id": manifest["run_id"],
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "manifest_sha256": _sha256(run / "manifest.json"),
            "artifact_sha256": {name: _sha256(run / name) for name in ARTIFACTS if (run / name).exists()},
            "discovered": manifest["counts"]["discovered"],
        })

    url_rows = [{"url": url, "discovery_method": "complete_date_shard_merge"} for url in ordered_urls]
    article_rows = []
    check_rows = []
    issue_rows = []
    counts = {
        "discovered": len(ordered_urls), "selected": len(ordered_urls), "passed": 0,
        "quarantined": 0, "skipped": 0, "exceptions": 0,
    }
    source = str(identity[0])
    for url in ordered_urls:
        article = article_by_url[url]
        article_rows.append(article)
        check, issues, bucket = _validate_one(
            article, url=url, source=source, start=combined_start, end=combined_end,
        )
        check_rows.append(check)
        issue_rows.extend(issues)
        counts[bucket] += 1

    now = datetime.now(timezone.utc)
    run_id = f"observe-merged-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".merging-", dir=output_root))
    final = output_root / run_id
    try:
        _write_jsonl(temporary / "urls.jsonl", url_rows)
        _write_jsonl(temporary / "articles.jsonl", article_rows)
        _write_jsonl(temporary / "field_checks.jsonl", check_rows)
        _write_jsonl(temporary / "issues.jsonl", issue_rows)
        first = intervals[0][3]
        manifest = dict(first)
        manifest.update({
            "run_id": run_id,
            "mode": "observation_shard_merge_no_production_writes",
            "status": "observed",
            "requested_start": combined_start.isoformat(),
            "requested_end": combined_end.isoformat(),
            "started_at": now.isoformat(),
            "finished_at": now.isoformat(),
            "runtime_seconds": 0.0,
            "counts": counts,
            "discovery_method": "complete_date_shard_merge",
            "discovery_evidence": {
                "coverage_complete": True,
                "strategy": "contiguous_complete_date_shards",
                "requested_days": [
                    (combined_start + timedelta(days=offset)).isoformat()
                    for offset in range((combined_end - combined_start).days + 1)
                ],
                "missing_days_in_index": [],
                "sitemap_failures": [],
                "shard_count": len(intervals),
                "parent_discovered_total": sum(int(row[3]["counts"]["discovered"]) for row in intervals),
                "merged_unique_urls": len(ordered_urls),
                "duplicate_urls_collapsed": len(duplicate_urls),
            },
            "derived_from_shards": parent_rows,
            "derivation": "union_complete_contiguous_date_shards_and_revalidate_combined_window",
        })
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (temporary / "summary.md").write_text(
            f"# Merged observation for {source}\n\n"
            f"- Run: `{run_id}`\n- Window: {combined_start} through {combined_end}\n"
            f"- Complete date shards: {len(intervals)}\n- Unique URLs: {len(ordered_urls)}\n"
            f"- Duplicate URLs collapsed: {len(duplicate_urls)}\n- Passed: {counts['passed']}\n"
            f"- Quarantined: {counts['quarantined']}\n- Skipped: {counts['skipped']}\n"
            "- Exceptions: 0\n\nAll records were revalidated against the combined interval. No production database operation was performed.\n",
            encoding="utf-8",
        )
        temporary.rename(final)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    print(merge_shards(args.shard_root, args.output_root))


if __name__ == "__main__":
    main()
