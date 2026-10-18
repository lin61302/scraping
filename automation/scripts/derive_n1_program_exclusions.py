#!/usr/bin/env python3
"""Derive explicit N1 non-news exclusions from immutable page evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scraper_control.observe import adapter_bundle_sha256
from scraper_control.promote import _load_candidates


CTA = "Pridružite se diskusiji ili pročitajte komentare"


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def derive(parent: Path, output_root: Path) -> Path:
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    counts = manifest.get("counts") or {}
    if manifest.get("source") != "rs.n1info.com" or manifest.get("status") != "observed":
        raise ValueError("parent must be a completed N1 observation")
    if counts.get("selected") != counts.get("discovered") or counts.get("exceptions"):
        raise ValueError("parent observation must be complete and exception free")
    source_file = Path(manifest["source_file"])
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != manifest.get("source_sha256"):
        raise ValueError("adapter changed after parent observation")
    if adapter_bundle_sha256(source_file) != manifest.get("source_bundle_sha256"):
        raise ValueError("adapter bundle changed after parent observation")

    urls = _read(parent / "urls.jsonl")
    articles = _read(parent / "articles.jsonl")
    checks = _read(parent / "field_checks.jsonl")
    if not (len(urls) == len(articles) == len(checks) == counts["selected"]):
        raise ValueError("parent artifacts do not account for every URL")

    repaired = []
    for article, check in zip(articles, checks):
        body = " ".join(str(article.get("maintext") or "").split())
        fields = check.get("fields") or []
        failed_codes = {
            code for item in fields[:3] if item.get("status") != "pass"
            for code in item.get("codes", [])
        }
        if not (
            article["url"].startswith("https://n1info.rs/najave-emisija/")
            and not check.get("eligible")
            and failed_codes == {"maintext_too_short"}
            and str(article.get("title") or "").casefold().startswith("gledajte")
            and body.endswith(CTA)
            and len(body.split()) <= 12
            and check.get("page_evidence", {}).get("http_status") == 200
        ):
            continue
        declared = {
            "publisher_url": article["url"],
            "publisher_dom_selector": "article.article-wrapper",
            "publisher_body_text": body,
            "publisher_cta": CTA,
            "publisher_item_type": "live_program_announcement",
        }
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_program_announcement"
        article["exclusion_evidence"] = {"policy_type": "publisher_program_announcement", **declared}
        article.setdefault("field_evidence", {})["publisher_program_announcements"] = [declared]
        check.setdefault("page_evidence", {})["publisher_program_announcements"] = [declared]
        check.update({
            "eligible": False,
            "excluded": True,
            "exclusion_reason": article["exclusion_reason"],
            "exclusion_evidence": article["exclusion_evidence"],
            "exclusion_resolution": {"mode": "publisher_program_announcement", "announcement_verified": True},
        })
        repaired.append(article["url"])

    if len(repaired) != 3:
        raise ValueError(f"expected exactly three evidenced N1 announcements, found {len(repaired)}")

    child = dict(manifest)
    child_counts = dict(counts)
    child_counts["quarantined"] -= len(repaired)
    child_counts["skipped"] += len(repaired)
    run_id = f"observe-derived-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    child.update({
        "run_id": run_id,
        "started_at": now,
        "finished_at": now,
        "runtime_seconds": 0.0,
        "counts": child_counts,
        "derived_from_run_id": manifest["run_id"],
        "derivation": "n1_evidence_verified_program_announcement_exclusions",
        "repairs": repaired,
    })
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(parent / "urls.jsonl", run_dir / "urls.jsonl")
    _write(run_dir / "articles.jsonl", articles)
    _write(run_dir / "field_checks.jsonl", checks)
    old_issues = _read(parent / "issues.jsonl") if (parent / "issues.jsonl").exists() else []
    kept = [row for row in old_issues if row.get("url") not in repaired]
    kept.extend({
        "url": url, "source": manifest["source"], "stage": "source_policy",
        "category": "collection", "code": "excluded_program_announcement",
        "severity": "info", "status": "excluded", "attempt": 1,
    } for url in repaired)
    _write(run_dir / "issues.jsonl", kept)
    (run_dir / "manifest.json").write_text(json.dumps(child, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    eligible, accounting = _load_candidates(run_dir, child)
    if accounting["rejected"] or len(accounting["excluded"]) != 3 or len(eligible) != counts["passed"]:
        raise ValueError("derived observation failed independent promotion revalidation")
    (run_dir / "summary.md").write_text(
        f"# N1 evidence-derived program exclusions\n\n- Parent: `{manifest['run_id']}`\n"
        f"- Run: `{run_id}`\n- Excluded: {len(repaired)}\n- Eligible: {len(eligible)}\n",
        encoding="utf-8",
    )
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    print(derive(args.parent_run, args.output_root))


if __name__ == "__main__":
    main()
