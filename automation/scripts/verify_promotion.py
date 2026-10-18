from __future__ import annotations

"""Verify every promotable observation URL against the production database."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _required_field_errors(row: dict) -> list[str]:
    errors: list[str] = []
    for field in ("title", "maintext"):
        if not str(row.get(field) or "").strip():
            errors.append(field)
    value = row.get("date_publish")
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed = None
    if parsed is None:
        errors.append("date_publish")
    return errors


def verify(run_dir: Path, framework_root: Path) -> dict:
    automation_root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(automation_root), str(framework_root.resolve())]
    from core.base_pipeline import MONGO_DB, MONGO_URI, MongoClient
    from scraper_control.promote import _load_candidates

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    eligible, _ = _load_candidates(run_dir, manifest)
    source = manifest["source"]
    grouped: dict[str, list[str]] = {}
    for article in eligible:
        parsed = article["_parsed_date"]
        prefix = "opinion-articles" if str(article.get("collection") or "").startswith("opinion-articles") else "articles"
        grouped.setdefault(f"{prefix}-{parsed.year}-{parsed.month}", []).append(article["url"])

    db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000)[MONGO_DB]
    found: dict[str, dict] = {}
    for collection, urls in grouped.items():
        projection = {"url": 1, "title": 1, "maintext": 1, "date_publish": 1}
        for row in db[collection].find({"source_domain": source, "url": {"$in": urls}}, projection):
            found[row["url"]] = row

    expected = {article["url"] for article in eligible}
    invalid = [
        {"url": url, "fields": errors}
        for url, row in sorted(found.items())
        if (errors := _required_field_errors(row))
    ]
    dates = [row.get("date_publish") for row in found.values() if row.get("date_publish")]
    latest = max(dates) if dates else None
    if isinstance(latest, datetime):
        latest = latest.isoformat()
    elif latest is not None:
        latest = str(latest)
    return {
        "source": source,
        "expected_eligible": len(expected),
        "found": len(expected & set(found)),
        "missing_urls": sorted(expected - set(found)),
        "invalid_required_fields": invalid,
        "latest": latest,
        "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--framework-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = verify(Path(args.run_dir), Path(args.framework_root))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result["missing_urls"] or result["invalid_required_fields"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
