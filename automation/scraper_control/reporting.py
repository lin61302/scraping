from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Dict, List


def write_inventory_report(state_path: Path, output_dir: Path, as_of: date) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(state_path))
    connection.row_factory = sqlite3.Row
    rows = connection.execute("SELECT * FROM sources WHERE enabled=1 ORDER BY project, country_code, domain").fetchall()

    source_csv = output_dir / "source_inventory.csv"
    with source_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "source_id", "project", "country_code", "domain", "last_update", "stale_days",
            "status", "discovery_hints", "access_hints", "safe_through", "confidence", "note_path",
        ])
        watermarks = {
            row["source_id"]: row
            for row in connection.execute("SELECT * FROM watermarks")
        }
        for row in rows:
            last = date.fromisoformat(row["last_update"]) if row["last_update"] else None
            stale = (as_of - last).days if last else None
            watermark = watermarks.get(row["source_id"])
            writer.writerow([
                row["source_id"], row["project"], row["country_code"], row["domain"], row["last_update"] or "",
                "" if stale is None else stale, row["normalized_status"],
                ";".join(json.loads(row["discovery_hints_json"])),
                ";".join(json.loads(row["access_hints_json"])),
                watermark["safe_through"] if watermark else "",
                watermark["confidence"] if watermark else "unknown", row["note_path"],
            ])

    projects = defaultdict(list)
    countries = defaultdict(list)
    discovery = Counter()
    access = Counter()
    missing_last = stale_30 = stale_60 = 0
    for row in rows:
        projects[row["project"]].append(row)
        countries[(row["project"], row["country_code"])].append(row)
        hints = json.loads(row["discovery_hints_json"])
        if not hints:
            discovery["unknown"] += 1
        discovery.update(hints)
        access.update(json.loads(row["access_hints_json"]))
        if not row["last_update"]:
            missing_last += 1
        else:
            days = (as_of - date.fromisoformat(row["last_update"])).days
            stale_30 += days > 30
            stale_60 += days > 60

    country_rows = []
    for (project, code), members in sorted(countries.items()):
        dates = [date.fromisoformat(row["last_update"]) for row in members if row["last_update"]]
        last = max(dates).isoformat() if dates else "unknown"
        known_methods = sum(bool(json.loads(row["discovery_hints_json"])) for row in members)
        risk_sources = sum(bool(json.loads(row["access_hints_json"])) for row in members)
        country_rows.append((project, code, len(members), last, known_methods, risk_sources))

    report = output_dir / "INITIAL_INVENTORY.md"
    lines: List[str] = [
        "# Initial scraping automation inventory",
        "",
        f"Generated from the local Notion exports on {as_of.isoformat()}. Production script and database reconciliation is still pending, so safe through dates remain unknown.",
        "",
        "## Summary",
        "",
        f"- Registry sources: {len(rows)}",
        f"- MLP sources: {len(projects['mlp'])}",
        f"- MLEED sources: {len(projects['mleed'])}",
        f"- Sources with no parsed last update: {missing_last}",
        f"- Sources more than 30 days past their recorded country update: {stale_30}",
        f"- Sources more than 60 days past their recorded country update: {stale_60}",
        f"- Sources with historical access risk hints: {sum(access.values())}",
        "",
        "## Discovery hints from historical notes",
        "",
        "| Method | Sources mentioning method |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {count} |" for name, count in discovery.most_common())
    lines.extend(["", "## Historical access hints", "", "| Hint | Sources mentioning hint |", "|---|---:|"])
    lines.extend(f"| {name} | {count} |" for name, count in access.most_common())
    lines.extend([
        "", "## Country inventory", "",
        "`Last recorded update` is imported from the country page. It does not yet prove source level coverage.", "",
        "| Project | Country | Sources | Last recorded update | Sources with method hints | Sources with access hints |",
        "|---|---|---:|---|---:|---:|",
    ])
    lines.extend(f"| {p.upper()} | {c} | {n} | {last} | {known} | {risk} |" for p, c, n, last, known, risk in country_rows)
    lines.extend([
        "", "## Immediate operational risks", "",
        "- Production URL and monthly article collections need duplicate analysis and index planning before parallel unattended writes.",
        "- Source notes provide method hints but do not prove current URL coverage or field correctness.",
        "- The worker host needs disk retention controls before raw pages, screenshots, and append only logs are enabled.",
        "- The existing production environment and dependencies are not reproducible yet.",
        "- All source and country safe through dates remain unknown until observation runs pass coverage, field, anomaly, and postprocessing gates.",
        "",
    ])
    report.write_text("\n".join(lines), encoding="utf-8")
    return {"report": str(report), "csv": str(source_csv), "sources": len(rows)}

