from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple


def _score(source, script) -> int:
    domains = set(script.get("declared_source_domains", [])) | set(script.get("source_domains", []))
    if source["domain"] not in domains:
        return -1
    score = 10
    script_country = (script.get("country") or "").upper()
    source_country = source["country_code"].upper()
    if script_country == source_country:
        score += 5
    elif source_country in ("INT", "REG") and script_country in ("INT", "INT_REG"):
        score += 3
    project = script.get("project")
    if (source["project"] == "mlp" and project == "MLP") or (source["project"] == "mleed" and project == "MLEED"):
        score += 5
    elif project == "NEW_FRAMEWORK":
        env = bool(script.get("environment_flag", {}).get("present"))
        if env == (source["project"] == "mleed"):
            score += 3
        else:
            score += 1
    if script.get("ast_parse", {}).get("status") == "ok":
        score += 1
    return score


def reconcile(state_path: Path, manifest_path: Path, output_dir: Path) -> Dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scripts = manifest["scripts"]
    connection = sqlite3.connect(str(state_path))
    connection.row_factory = sqlite3.Row
    sources = connection.execute("SELECT * FROM sources WHERE enabled=1 ORDER BY source_id").fetchall()
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    counts = Counter()
    for source in sources:
        candidates: List[Tuple[int, dict]] = []
        for script in scripts:
            score = _score(source, script)
            if score >= 0:
                candidates.append((score, script))
        candidates.sort(key=lambda item: (-item[0], item[1]["relative_path"]))
        best_score = candidates[0][0] if candidates else None
        best = [script for score, script in candidates if score == best_score]
        if not candidates:
            disposition = "unmatched"
        elif len(best) == 1 and best_score >= 18:
            disposition = "high_confidence"
        elif len(best) == 1:
            disposition = "candidate"
        else:
            disposition = "ambiguous"
        counts[disposition] += 1
        results.append({
            "source_id": source["source_id"], "project": source["project"],
            "country_code": source["country_code"], "domain": source["domain"],
            "disposition": disposition, "best_score": best_score,
            "best_paths": [item["relative_path"] for item in best],
            "candidate_count": len(candidates),
        })
        if disposition == "high_confidence":
            connection.execute("UPDATE sources SET script_path=? WHERE source_id=?", (best[0]["relative_path"], source["source_id"]))
    connection.commit()
    connection.close()

    csv_path = output_dir / "script_reconciliation.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=(
            "source_id", "project", "country_code", "domain", "disposition",
            "best_score", "candidate_count", "best_paths",
        ))
        writer.writeheader()
        for result in results:
            writer.writerow({**result, "best_paths": ";".join(result["best_paths"])})

    report_path = output_dir / "SCRIPT_RECONCILIATION.md"
    report_path.write_text(
        "\n".join([
            "# Notion and production script reconciliation", "",
            f"Compared {len(sources)} registry records with {len(scripts)} Python files from the sanitized static manifest.", "",
            "## Results", "",
            f"- High confidence single script: {counts['high_confidence']}",
            f"- Lower confidence single candidate: {counts['candidate']}",
            f"- Ambiguous best match: {counts['ambiguous']}",
            f"- No domain match: {counts['unmatched']}", "",
            "Only high confidence matches were written to the local control database. Ambiguous and unmatched records require review against the live MongoDB source registry. Static matching does not prove that a script is active or correct.", "",
        ]),
        encoding="utf-8",
    )
    return {"sources": len(sources), "scripts": len(scripts), "counts": dict(counts), "csv": str(csv_path), "report": str(report_path)}

