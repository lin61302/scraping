#!/usr/bin/env python3
"""Guarded five-row ZimLive publisher-local timestamp/spacing repair.

The April 17–19 overlap rows have the same publication *day*, title, and body
as the first-party observation. Their stored clock uses WordPress date_gmt;
new records use the publisher's local date. This repairs only those exact rows,
and keeps a full BSON-typed backup before any update.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bson import json_util


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--framework-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    sys.path[:0] = [str(args.framework_root.resolve()), str(Path(__file__).resolve().parents[1])]
    from core.base_pipeline import MONGO_DB, MONGO_URI, MongoClient

    run = args.run_dir.resolve()
    manifest = json.loads((run / "manifest.json").read_text())
    if manifest.get("source") != "zimlive.com" or manifest.get("status") != "observed":
        raise ValueError("Wrong or incomplete observation")
    review = args.output_dir / "zimlive_exact_postcheck.json"
    mismatches = json.loads(review.read_text()).get("field_mismatches", [])
    if len(mismatches) != 5 or any(set(x.get("fields", [])) != {"maintext", "date_publish"} for x in mismatches):
        raise ValueError("Expected exactly five known overlap mismatches")
    wanted = {item["url"] for item in mismatches}
    if len(wanted) != 5:
        raise ValueError("Duplicate overlap URL")
    publisher_evidence_path = args.output_dir / "zimlive_wp_overlap_evidence.json"
    publisher_evidence = json.loads(publisher_evidence_path.read_text())
    publisher_by_url = {item["link"]: item for item in publisher_evidence["rows"]}
    if set(publisher_by_url) != wanted:
        raise ValueError("Publisher evidence does not cover exactly the five overlaps")
    articles = {
        row["url"]: row
        for row in (json.loads(line) for line in (run / "articles.jsonl").open())
        if row["url"] in wanted
    }
    if set(articles) != wanted:
        raise ValueError("Observation does not contain all five overlaps")

    db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000)[MONGO_DB]
    collection = db["articles-2026-4"]
    plans = []
    for url in sorted(wanted):
        new = articles[url]
        rows = list(collection.find({"source_domain": "zimlive.com", "url": url}))
        if len(rows) != 1:
            raise ValueError(f"Expected one April row: {url}, found {len(rows)}")
        old = rows[0]
        new_date = datetime.fromisoformat(new["date_publish"])
        old_date = old.get("date_publish")
        wp = publisher_by_url[url]
        if not isinstance(old_date, datetime):
            raise ValueError(f"Stored publication timestamp is missing: {url}")
        if wp["date"] != new["date_publish"] or wp["date_gmt"] != old_date.isoformat():
            raise ValueError(f"First-party publication timestamps disagree: {url}")
        if new_date - old_date != timedelta(hours=2):
            raise ValueError(f"Publisher-local/GMT relationship changed: {url}")
        if new_date.date() != old_date.date() or not (new_date.year == 2026 and new_date.month == 4 and 17 <= new_date.day <= 19):
            raise ValueError(f"Publication day changed: {url}")
        if old.get("title") != new.get("title"):
            raise ValueError(f"Headline differs: {url}")
        before_body = old.get("maintext") or ""
        after_body = new.get("maintext") or ""
        if not before_body or not after_body or normalized(before_body) != normalized(after_body):
            raise ValueError(f"Body differs beyond whitespace: {url}")
        plans.append({"url": url, "old": old, "new_date": new_date, "new_body": after_body, "wp": wp})

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    mode = "commit" if args.commit else "dry_run"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = output / f"zimlive_overlap_{mode}_{stamp}.jsonl"
    descriptor = output / f"zimlive_overlap_{mode}_{stamp}.json"
    fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for item in plans:
            stream.write(json_util.dumps(item["old"], ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    report = {
        "mode": mode,
        "source": "zimlive.com",
        "observation_run_id": manifest["run_id"],
        "observation_manifest_sha256": sha((run / "manifest.json").read_bytes()),
        "backup_path": str(backup),
        "backup_sha256": sha(backup.read_bytes()),
        "publisher_evidence_path": str(publisher_evidence_path.resolve()),
        "publisher_evidence_sha256": sha(publisher_evidence_path.read_bytes()),
        "rows": [],
    }
    for item in plans:
        old = item["old"]
        row = {
            "url": item["url"],
            "before_date_publish": old["date_publish"].isoformat(),
            "after_date_publish": item["new_date"].isoformat(),
            "publisher_date": item["wp"]["date"],
            "publisher_date_gmt": item["wp"]["date_gmt"],
            "publisher_modified": item["wp"]["modified"],
            "title_sha256": sha(old["title"].encode()),
            "before_maintext_sha256": sha(old["maintext"].encode()),
            "after_maintext_sha256": sha(item["new_body"].encode()),
            "body_difference": "whitespace_only",
            "matched_count": 0,
            "verified": False,
        }
        if args.commit:
            result = collection.update_one(
                {"_id": old["_id"], "source_domain": "zimlive.com", "url": item["url"],
                 "date_publish": old["date_publish"], "title": old["title"], "maintext": old["maintext"]},
                {"$set": {"date_publish": item["new_date"], "maintext": item["new_body"]}},
            )
            row["matched_count"] = result.matched_count
            current = collection.find_one({"_id": old["_id"]})
            row["verified"] = bool(
                result.matched_count == 1 and current
                and current.get("date_publish") == item["new_date"]
                and current.get("title") == old["title"]
                and current.get("maintext") == item["new_body"]
            )
        report["rows"].append(row)
        descriptor.write_text(json.dumps(report, indent=2) + "\n")
        if args.commit and not row["verified"]:
            raise RuntimeError(f"Targeted repair failed, backup and partial status retained: {item['url']}")
    print(json.dumps({"mode": mode, "rows": len(plans), "verified": sum(x["verified"] for x in report["rows"]), "report": str(descriptor), "backup_sha256": report["backup_sha256"]}))


if __name__ == "__main__":
    main()
