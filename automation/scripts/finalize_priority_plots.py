from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path


DEFAULT_COUNTRIES = ["TUR", "BRA", "MLI", "ZWE", "PHL", "GEO", "PAK", "MYS", "SRB", "ARM"]
REQUIRED_SUMMARY_FIELDS = {
    "source_id", "source", "safe_through", "validated_through", "resume_through",
    "confidence", "latest_date_through_as_of", "future_date_count",
    "future_date_min", "future_date_max", "future_date_sample", "future_url_sample",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_database(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as live, sqlite3.connect(target) as snapshot:
        live.backup(snapshot)


def inventory(state: Path, countries: list[str]) -> dict[str, list[dict[str, str]]]:
    markers = ",".join("?" for _ in countries)
    with sqlite3.connect(state) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""SELECT source_id, country_code, domain
                FROM sources
                WHERE country_code IN ({markers})
                ORDER BY country_code, project, domain""",
            countries,
        ).fetchall()
    grouped = {country: [] for country in countries}
    for row in rows:
        grouped[row["country_code"]].append(dict(row))
    return grouped


def validate_pack(
    plots_root: Path, expected: dict[str, list[dict[str, str]]], cutoff: date,
) -> list[dict]:
    alerts = []
    for country, sources in expected.items():
        directory = plots_root / country
        required_files = [
            directory / f"{country}_baseline_summary.json",
            directory / f"{country}_baseline_summary.csv",
            directory / f"{country}_baseline_summary.md",
            directory / f"{country}_monthly_volume_2026.png",
            directory / f"{country}_recent_daily_2026.png",
        ]
        missing = [str(path) for path in required_files if not path.is_file() or not path.stat().st_size]
        if missing:
            raise ValueError(f"{country}: missing or empty plot artifacts: {missing}")
        rows = json.loads(required_files[0].read_text())
        expected_domains = {row["source_id"]: row["domain"] for row in sources}
        expected_ids = set(expected_domains)
        actual_ids = {row.get("source_id") for row in rows}
        if len(rows) != len(sources) or actual_ids != expected_ids:
            raise ValueError(
                f"{country}: source mismatch; expected {len(sources)} / {sorted(expected_ids)}, "
                f"got {len(rows)} / {sorted(str(value) for value in actual_ids)}"
            )
        for row in rows:
            absent = REQUIRED_SUMMARY_FIELDS - row.keys()
            if absent:
                raise ValueError(f"{country} {row.get('source')}: missing summary fields {sorted(absent)}")
            source_id = row.get("source_id")
            if row.get("source") != expected_domains[source_id]:
                raise ValueError(
                    f"{country} {source_id}: source/domain mismatch; "
                    f"expected {expected_domains[source_id]!r}, got {row.get('source')!r}"
                )
            safe_text = row.get("safe_through")
            validated_text = row.get("validated_through")
            resume_text = row.get("resume_through")
            if resume_text != validated_text:
                raise ValueError(f"{country} {row['source']}: resume-through differs from validated-through")
            parsed_watermarks = {}
            for field, value in (
                ("safe_through", safe_text),
                ("validated_through", validated_text),
                ("resume_through", resume_text),
            ):
                if value is None:
                    continue
                try:
                    parsed = date.fromisoformat(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{country} {row['source']}: invalid {field}") from exc
                if parsed > cutoff:
                    raise ValueError(f"{country} {row['source']}: {field} exceeds cutoff")
                parsed_watermarks[field] = parsed
            if safe_text is not None and validated_text is None:
                raise ValueError(f"{country} {row['source']}: safe-through lacks validated-through")
            if (
                "safe_through" in parsed_watermarks
                and "validated_through" in parsed_watermarks
                and parsed_watermarks["validated_through"] < parsed_watermarks["safe_through"]
            ):
                raise ValueError(f"{country} {row['source']}: validated-through precedes safe-through")
            latest = row.get("latest_date_through_as_of")
            if latest and date.fromisoformat(latest) > cutoff:
                raise ValueError(f"{country} {row['source']}: latest-through date exceeds cutoff")
            future_count = row.get("future_date_count", 0)
            if future_count and not (row.get("future_date_min") and row.get("future_date_max")):
                raise ValueError(f"{country} {row['source']}: future rows lack date range")
            if future_count:
                alerts.append({
                    "country": country,
                    "source": row["source"],
                    "category": "future_dated_rows",
                    "count": future_count,
                    "date_min": row["future_date_min"],
                    "date_max": row["future_date_max"],
                    "url_sample": row["future_url_sample"],
                })
    return alerts


def publish_tree(staged_root: Path, output_root: Path, countries: list[str]) -> list[Path]:
    published = []
    for country in countries:
        destination = output_root / country
        destination.mkdir(parents=True, exist_ok=True)
        for source in sorted((staged_root / country).iterdir()):
            temporary = destination / f".{source.name}.publishing"
            shutil.copy2(source, temporary)
            os.replace(temporary, destination / source.name)
            published.append(destination / source.name)
    return published


def run(args: argparse.Namespace) -> dict:
    automation_root = Path(args.automation_root).resolve()
    framework_root = Path(args.framework_root).resolve()
    live_state = Path(args.control_db).resolve()
    output_root = Path(args.plots_root).resolve()
    metrics_output = Path(args.metrics_output).resolve()
    manifest_output = Path(args.manifest_output).resolve()
    contact_output = Path(args.contact_output).resolve()
    cutoff = date.fromisoformat(args.cutoff)
    scripts = automation_root / "scripts"

    work_parent = automation_root / "var"
    work_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="priority-plot-refresh-", dir=work_parent) as temporary:
        stage = Path(temporary)
        state = stage / "control.db"
        snapshot_database(live_state, state)
        state_sha256 = sha256(state)
        expected = inventory(state, args.countries)
        empty = [country for country, rows in expected.items() if not rows]
        if empty:
            raise ValueError(f"countries absent from control database: {empty}")
        domains = sorted({row["domain"] for rows in expected.values() for row in rows})
        domains_file = stage / "domains.json"
        domains_file.write_text(json.dumps(domains) + "\n")
        metrics = stage / "priority_metrics.json"
        staged_plots = stage / "plots"
        staged_contact = stage / contact_output.name

        subprocess.run([
            sys.executable, str(scripts / "export_metrics.py"),
            "--framework-root", str(framework_root), "--domains-json", str(domains_file),
            "--year", str(args.year), "--output", str(metrics),
        ], check=True)
        subprocess.run([
            sys.executable, str(scripts / "make_country_plots.py"),
            "--metrics", str(metrics), "--control-db", str(state),
            "--output-root", str(staged_plots), "--as-of", args.cutoff,
            "--countries", *args.countries,
        ], check=True)
        subprocess.run([
            sys.executable, str(scripts / "make_priority_contact_sheet.py"),
            "--plots-root", str(staged_plots), "--output", str(staged_contact),
            "--cutoff", args.cutoff, "--countries", *args.countries,
        ], check=True)
        alerts = validate_pack(staged_plots, expected, cutoff)

        published = publish_tree(staged_plots, output_root, args.countries)
        contact_output.parent.mkdir(parents=True, exist_ok=True)
        contact_temporary = contact_output.with_name(f".{contact_output.name}.publishing")
        shutil.copy2(staged_contact, contact_temporary)
        os.replace(contact_temporary, contact_output)
        published.append(contact_output)

        metrics_output.parent.mkdir(parents=True, exist_ok=True)
        metrics_temporary = metrics_output.with_name(f".{metrics_output.name}.publishing")
        shutil.copy2(metrics, metrics_temporary)
        os.replace(metrics_temporary, metrics_output)

    result = {
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "cutoff": args.cutoff,
        "countries": args.countries,
        "source_counts": {country: len(rows) for country, rows in expected.items()},
        "control_db_snapshot_sha256": state_sha256,
        "metrics": str(metrics_output),
        "metrics_sha256": sha256(metrics_output),
        "contact_sheet": str(contact_output),
        "contact_sheet_sha256": sha256(contact_output),
        "alerts": alerts,
        "artifacts": [
            {"path": str(path), "sha256": sha256(path)} for path in published
        ],
    }
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_temporary = manifest_output.with_name(f".{manifest_output.name}.publishing")
    manifest_temporary.write_text(json.dumps(result, indent=2) + "\n")
    os.replace(manifest_temporary, manifest_output)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--automation-root", required=True)
    parser.add_argument("--framework-root", required=True)
    parser.add_argument("--control-db", required=True)
    parser.add_argument("--plots-root", required=True)
    parser.add_argument("--metrics-output", required=True)
    parser.add_argument("--manifest-output", required=True)
    parser.add_argument("--contact-output", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--countries", nargs="+", default=DEFAULT_COUNTRIES)
    args = parser.parse_args()
    print(json.dumps(run(args), indent=2))


if __name__ == "__main__":
    main()
