"""Exact post-write verification for promoted observation records."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def normalized_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value.replace(tzinfo=None)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except (TypeError, ValueError):
            return None
    # BSON stores milliseconds, so compare at the same precision.
    return parsed.replace(microsecond=(parsed.microsecond // 1000) * 1000)


def verify_exact_observation(run_dir: Path, framework_root: Path) -> dict:
    automation_root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(automation_root), str(framework_root.resolve())]
    from core.base_pipeline import MONGO_DB, MONGO_URI, MongoClient
    from scraper_control.promote import _load_candidates

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    eligible, accounting = _load_candidates(run_dir, manifest)
    if accounting["rejected"]:
        raise ValueError("Observation has rejected candidates")

    expected_by_collection: dict[str, dict[str, dict]] = defaultdict(dict)
    for article in eligible:
        parsed = article["_parsed_date"]
        prefix = "opinion-articles" if str(article.get("collection") or "").startswith("opinion-articles") else "articles"
        expected_by_collection[f"{prefix}-{parsed.year}-{parsed.month}"][article["url"]] = article

    db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000)[MONGO_DB]
    missing: list[str] = []
    duplicates: list[dict] = []
    mismatches: list[dict] = []
    checked = 0
    latest: datetime | None = None
    for collection, expected in expected_by_collection.items():
        found: dict[str, list[dict]] = defaultdict(list)
        for row in db[collection].find(
            {"source_domain": manifest["source"], "url": {"$in": list(expected)}},
            {"url": 1, "title": 1, "maintext": 1, "date_publish": 1},
        ):
            found[row["url"]].append(row)
        for url, article in expected.items():
            rows = found.get(url, [])
            if not rows:
                missing.append(url)
                continue
            if len(rows) != 1:
                duplicates.append({"url": url, "collection": collection, "count": len(rows)})
                continue
            row = rows[0]
            different = [field for field in ("title", "maintext") if row.get(field) != article.get(field)]
            stored_date = normalized_datetime(row.get("date_publish"))
            if stored_date != normalized_datetime(article["_parsed_date"]):
                different.append("date_publish")
            if different:
                mismatches.append({"url": url, "collection": collection, "fields": different})
            else:
                checked += 1
                if stored_date and (latest is None or stored_date > latest):
                    latest = stored_date
    return {
        "source": manifest["source"],
        "observation_run_id": manifest["run_id"],
        "expected_eligible": len(eligible),
        "exact_match": checked,
        "missing_urls": missing,
        "duplicate_urls": duplicates,
        "field_mismatches": mismatches,
        "latest": latest.isoformat() if latest else None,
        "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def postcheck_passed(result: dict) -> bool:
    return bool(
        result.get("observation_run_id")
        and int(result.get("exact_match", -1)) == int(result.get("expected_eligible", -2))
        and not result.get("missing_urls")
        and not result.get("duplicate_urls")
        and not result.get("field_mismatches")
    )
