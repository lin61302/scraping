#!/usr/bin/env python3
"""Read-only 2026 production volume and candidate-inventory check for two BRA sources.

Run after guarded promotions and exact postchecks. This script never updates MongoDB
or the scraper control database. The candidate inventories are independently audited
publisher discovery sets, so a partial overlap is reported as a coverage gap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path


SOURCE_COUNTS = {
    "mleed:BRA:agenciaeconordeste.com.br": 46,
    "mleed:BRA:amazoniareal.com.br": 61,
    "mleed:BRA:infoamazonia.org": 41,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parsed_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def read_candidate_inventory(path: Path, source_id: str, as_of: date) -> dict:
    inventory = json.loads(path.read_text(encoding="utf-8"))
    if inventory.get("source_id") != source_id:
        raise ValueError("Candidate inventory source_id does not match requested source")
    urls = inventory.get("candidate_urls")
    expected = SOURCE_COUNTS[source_id]
    if not isinstance(urls, list) or len(urls) != expected or len(set(urls)) != expected:
        raise ValueError("Candidate inventory count or URL uniqueness does not match audited count")
    if not all(isinstance(url, str) and url.startswith("https://") for url in urls):
        raise ValueError("Candidate inventory has an invalid URL")
    window = inventory.get("window") or {}
    start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    if start.year != as_of.year or not start <= end <= as_of:
        raise ValueError("Candidate inventory window is inconsistent with cutoff")
    return {"urls": set(urls), "start": start, "end": end}


def fetch_source_rows(framework_root: Path, domain: str, year: int) -> tuple[list[dict], list[str]]:
    """Query only source-specific rows from the year's article partitions.

    Nodate rows downloaded in the given year are included in the field audit but
    cannot be assigned a publication month.
    """
    sys.path.insert(0, str(framework_root.resolve()))
    from core.base_pipeline import MONGO_DB, MONGO_URI, MongoClient

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000)
    rows: list[dict] = []
    try:
        db = client[MONGO_DB]
        existing = set(db.list_collection_names())
        names = [
            f"{prefix}-{year}-{month}"
            for prefix in ("articles", "opinion-articles")
            for month in range(1, 13)
            if f"{prefix}-{year}-{month}" in existing
        ]
        if "articles-nodate" in existing:
            names.append("articles-nodate")
        projection = {
            "url": 1, "title": 1, "maintext": 1, "date_publish": 1,
            "date_download": 1, "environmental": 1, "primary_location": 1,
        }
        for name in names:
            cursor = db[name].find({"source_domain": domain}, projection, no_cursor_timeout=True)
            try:
                for record in cursor:
                    if name == "articles-nodate":
                        downloaded = parsed_date(record.get("date_download"))
                        if not downloaded or downloaded.year != year:
                            continue
                    rows.append({
                        "_collection": name,
                        "url": record.get("url"),
                        "title": record.get("title"),
                        "maintext": record.get("maintext"),
                        "date_publish": record.get("date_publish"),
                        "environmental": record.get("environmental"),
                        "primary_location": record.get("primary_location"),
                    })
            finally:
                cursor.close()
        return rows, names
    finally:
        client.close()


def analyze_rows(rows: list[dict], source_id: str, candidates: dict, as_of: date) -> dict:
    start, end = candidates["start"], candidates["end"]
    candidate_urls = candidates["urls"]
    by_url: dict[str, list[dict]] = defaultdict(list)
    monthly: dict[str, dict] = {
        f"{as_of.year}-{month:02d}": {"month": f"{as_of.year}-{month:02d}", "rows": 0, "unique_urls": 0}
        for month in range(1, as_of.month + 1)
    }
    month_urls: dict[str, set[str]] = defaultdict(set)
    missing_fields = []
    invalid_dates = []
    partition_mismatches = []
    future_dates = []
    candidate_metadata_mismatches = []
    for row in rows:
        url = row.get("url")
        collection = str(row.get("_collection") or "")
        if url:
            by_url[str(url)].append(row)
        missing = [field for field in ("url", "title", "maintext") if not str(row.get(field) or "").strip()]
        day = parsed_date(row.get("date_publish"))
        if day is None:
            missing.append("date_publish")
            invalid_dates.append({"url": url, "collection": collection})
        if missing:
            missing_fields.append({"url": url, "collection": collection, "fields": missing})
        if day is None:
            continue
        expected_suffix = f"-{day.year}-{day.month}"
        if collection == "articles-nodate" or not collection.endswith(expected_suffix):
            partition_mismatches.append({"url": url, "collection": collection, "date_publish": day.isoformat()})
        if day > as_of:
            future_dates.append({"url": url, "collection": collection, "date_publish": day.isoformat()})
        if day.year == as_of.year and day <= as_of:
            key = day.strftime("%Y-%m")
            monthly[key]["rows"] += 1
            if url:
                month_urls[key].add(str(url))
    for key, item in monthly.items():
        item["unique_urls"] = len(month_urls[key])
    duplicates = [
        {"url": url, "collections": [row.get("_collection") for row in matches], "count": len(matches)}
        for url, matches in sorted(by_url.items()) if len(matches) > 1
    ]
    verified = set()
    for url in candidate_urls:
        matches = by_url.get(url, [])
        for row in matches:
            if row.get("environmental") is not True or row.get("primary_location") != "ENV_BRA":
                candidate_metadata_mismatches.append({
                    "url": url,
                    "collection": row.get("_collection"),
                    "environmental": row.get("environmental"),
                    "primary_location": row.get("primary_location"),
                })
        if len(matches) != 1:
            continue
        row = matches[0]
        day = parsed_date(row.get("date_publish"))
        if not (day and start <= day <= end):
            continue
        if not all(str(row.get(field) or "").strip() for field in ("title", "maintext")):
            continue
        if not str(row.get("_collection") or "").endswith(f"-{day.year}-{day.month}"):
            continue
        if row.get("environmental") is not True or row.get("primary_location") != "ENV_BRA":
            continue
        verified.add(url)
    all_present = candidate_urls & set(by_url)
    window_urls = {
        url for url, matches in by_url.items()
        if any((day := parsed_date(row.get("date_publish"))) and start <= day <= end for row in matches)
    }
    return {
        "source_id": source_id,
        "source_domain": source_id.rsplit(":", 1)[-1],
        "as_of": as_of.isoformat(),
        "candidate_window": {"start": start.isoformat(), "end": end.isoformat()},
        "candidate_expected": len(candidate_urls),
        "candidate_present": len(all_present),
        "candidate_verified_in_window": len(verified),
        "candidate_missing_urls": sorted(candidate_urls - set(by_url)),
        "candidate_present_but_invalid_urls": sorted(all_present - verified),
        "unexpected_window_urls": sorted(window_urls - candidate_urls),
        "source_2026_rows": len(rows),
        "source_2026_unique_urls": len(by_url),
        "duplicate_urls": duplicates,
        "missing_required_fields": missing_fields,
        "invalid_date_rows": invalid_dates,
        "partition_mismatches": partition_mismatches,
        "candidate_metadata_mismatches": sorted(candidate_metadata_mismatches, key=lambda row: row["url"]),
        "after_cutoff_rows": future_dates,
        "undated_rows_excluded_from_monthly": len(invalid_dates),
        "rows_excluded_from_monthly": len(rows) - sum(item["rows"] for item in monthly.values()),
        "monthly": list(monthly.values()),
        "candidate_coverage_complete": len(verified) == len(candidate_urls),
        "note": "Candidate coverage is a bounded inventory check; exact field equality requires separate postcheck evidence.",
    }


def render_monthly_plot(metrics: dict, output: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1200, 660
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("DejaVuSans.ttf", 26)
        font = ImageFont.truetype("DejaVuSans.ttf", 17)
        small = ImageFont.truetype("DejaVuSans.ttf", 14)
    except OSError:
        title_font = font = small = ImageFont.load_default()
    domain = metrics["source_domain"]
    as_of = date.fromisoformat(metrics["as_of"])
    draw.text((70, 32), f"{domain} · 2026 production monthly volume", font=title_font, fill="#172b4d")
    draw.text((70, 76), f"Unique publication URLs and stored rows through {as_of.isoformat()}", font=font, fill="#475569")
    chart = (85, 150, 1130, 530)
    left, top, right, bottom = chart
    data = metrics["monthly"]
    max_value = max(1, *(item["rows"] for item in data), *(item["unique_urls"] for item in data))
    scale = max(1, (max_value + 3) // 4)
    ceiling = scale * 4
    for tick in range(5):
        value = tick * scale
        y = bottom - (bottom - top) * value / ceiling
        draw.line((left, y, right, y), fill="#e1e7ef", width=1)
        draw.text((30, y - 8), str(value), font=small, fill="#64748b")
    count = len(data)
    xs = [left + (right - left) * i / max(1, count - 1) for i in range(count)]
    for x, item in zip(xs, data):
        label = date.fromisoformat(item["month"] + "-01").strftime("%b")
        if item["month"] == as_of.strftime("%Y-%m") and as_of.day < 28:
            label += f" 1–{as_of.day}"
        draw.text((x - 28, bottom + 12), label, font=small, fill="#334155")
    for key, color in (("rows", "#c2412d"), ("unique_urls", "#1769aa")):
        points = [(xs[i], bottom - (bottom - top) * item[key] / ceiling) for i, item in enumerate(data)]
        if len(points) > 1:
            draw.line(points, fill=color, width=4)
        for x, y in points:
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color)
    draw.line((730, 115, 770, 115), fill="#1769aa", width=4)
    draw.text((782, 105), "Unique URLs", font=small, fill="#334155")
    draw.line((930, 115, 970, 115), fill="#c2412d", width=4)
    draw.text((982, 105), "Stored rows", font=small, fill="#334155")
    footer = (f"Audited candidate inventory: {metrics['candidate_verified_in_window']}/{metrics['candidate_expected']} "
              f"valid URLs in production · September is partial through {as_of.day}")
    draw.text((70, 590), footer, font=font, fill="#334155")
    excluded = metrics.get("undated_rows_excluded_from_monthly", 0)
    if excluded:
        draw.text((70, 619), f"{excluded} undated production row(s) excluded from monthly volume; details in metrics JSON", font=small, fill="#a43c26")
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(output), format="PNG")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework-root", type=Path, required=True)
    parser.add_argument("--source-id", choices=sorted(SOURCE_COUNTS), required=True)
    parser.add_argument("--candidate-discovery", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    candidates = read_candidate_inventory(args.candidate_discovery, args.source_id, args.as_of)
    rows, collections = fetch_source_rows(args.framework_root, args.source_id.rsplit(":", 1)[-1], args.as_of.year)
    metrics = analyze_rows(rows, args.source_id, candidates, args.as_of)
    metrics["captured_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metrics["collections_read"] = collections
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    metrics_path = output / "production_metrics.json"
    plot_path = output / "monthly_volume_2026.png"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    render_monthly_plot(metrics, plot_path)
    manifest = {
        "source_id": args.source_id,
        "as_of": args.as_of.isoformat(),
        "read_only": True,
        "artifacts": [
            {"path": path.name, "sha256": sha256(path), "bytes": path.stat().st_size}
            for path in (metrics_path, plot_path)
        ],
        "candidate_discovery": {
            "path": str(args.candidate_discovery.resolve()),
            "sha256": sha256(args.candidate_discovery),
        },
    }
    (output / "artifact_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output_dir": str(output),
        "source_id": args.source_id,
        "candidate_verified_in_window": metrics["candidate_verified_in_window"],
        "candidate_expected": metrics["candidate_expected"],
        "missing_required_fields": len(metrics["missing_required_fields"]),
        "duplicate_urls": len(metrics["duplicate_urls"]),
        "candidate_metadata_mismatches": len(metrics["candidate_metadata_mismatches"]),
    }))


if __name__ == "__main__":
    main()
