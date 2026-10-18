"""Import externally verified source watermarks into the scheduler state.

This is the bridge for production cycles completed before the orchestrator was
deployed.  The manifest is deliberately explicit and the default is a dry run.
Only high-confidence entries are accepted, and an existing watermark can never
be moved backwards.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_manifest(path: Path) -> tuple[str, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError("watermark manifest version must be 1")
    batch_id = str(payload.get("batch_id") or "").strip()
    if not batch_id:
        raise ValueError("watermark manifest requires batch_id")
    entries = payload.get("watermarks")
    if not isinstance(entries, list) or not entries:
        raise ValueError("watermark manifest requires a non-empty watermarks list")
    return batch_id, entries


def _validate_evidence(evidence: dict[str, Any], manifest_path: Path) -> tuple[str | None, dict[str, str]]:
    """Require portable, content-addressed evidence whose bytes match its digest."""

    report_value = evidence.get("status_report")
    expected = str(evidence.get("status_report_sha256") or "").lower()
    if not isinstance(report_value, str) or not report_value.strip():
        return "evidence_status_report_required", {}
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return "evidence_status_report_sha256_invalid", {}
    configured = Path(report_value)
    if configured.is_absolute():
        return "evidence_status_report_must_be_project_relative", {}
    automation_root = manifest_path.resolve().parent.parent
    report = (automation_root / configured).resolve()
    try:
        report.relative_to(automation_root)
    except ValueError:
        return "evidence_status_report_outside_automation_root", {}
    if not re.fullmatch(
        rf".+\.{re.escape(expected)}{re.escape(report.suffix)}", report.name
    ):
        return "evidence_status_report_not_content_addressed", {}
    try:
        actual = hashlib.sha256(report.read_bytes()).hexdigest()
    except OSError as exc:
        return "evidence_status_report_unreadable", {"detail": str(exc)}
    if actual != expected:
        return "evidence_status_report_sha256_mismatch", {"expected": expected, "actual": actual}
    return None, {}


def seed(state_path: Path, manifest_path: Path, *, commit: bool = False) -> dict[str, Any]:
    batch_id, entries = _load_manifest(manifest_path)
    result: dict[str, Any] = {
        "mode": "commit" if commit else "dry_run",
        "batch_id": batch_id,
        "requested": len(entries),
        "ready": [],
        "evidence_refresh": [],
        "already_current": [],
        "errors": [],
        "committed": 0,
    }
    seen: set[str] = set()
    with sqlite3.connect(state_path) as connection:
        connection.row_factory = sqlite3.Row
        source_ids = {row["source_id"] for row in connection.execute("SELECT source_id FROM sources")}
        current = {
            row["source_id"]: {
                "safe_through": row["safe_through"],
                "validated_through": row["validated_through"],
                "evidence_json": row["evidence_json"],
                "run_id": row["run_id"],
            }
            for row in connection.execute(
                "SELECT source_id, safe_through, validated_through, evidence_json, run_id FROM watermarks"
            )
        }
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(entries):
            source_id = str(raw.get("source_id") or "").strip()
            if not source_id or source_id in seen:
                result["errors"].append({"index": index, "source_id": source_id, "error": "missing_or_duplicate_source_id"})
                continue
            seen.add(source_id)
            if source_id not in source_ids:
                result["errors"].append({"index": index, "source_id": source_id, "error": "unknown_source_id"})
                continue
            if raw.get("confidence") != "high":
                result["errors"].append({"index": index, "source_id": source_id, "error": "confidence_must_be_high"})
                continue
            try:
                safe = date.fromisoformat(str(raw["safe_through"]))
            except (KeyError, TypeError, ValueError):
                result["errors"].append({"index": index, "source_id": source_id, "error": "invalid_safe_through"})
                continue
            try:
                resume = date.fromisoformat(str(raw.get("resume_through") or raw["safe_through"]))
            except (KeyError, TypeError, ValueError):
                result["errors"].append({"index": index, "source_id": source_id, "error": "invalid_resume_through"})
                continue
            if resume < safe:
                result["errors"].append({"index": index, "source_id": source_id, "error": "resume_before_safe_through"})
                continue
            old_row = current.get(source_id) or {}
            old = date.fromisoformat(old_row["safe_through"]) if old_row.get("safe_through") else None
            old_resume = date.fromisoformat(old_row["validated_through"]) if old_row.get("validated_through") else old
            if old and safe < old:
                result["errors"].append({
                    "index": index,
                    "source_id": source_id,
                    "error": "watermark_regression",
                    "existing": old.isoformat(),
                    "requested": safe.isoformat(),
                })
                continue
            evidence = raw.get("evidence")
            if not isinstance(evidence, dict) or not evidence:
                result["errors"].append({"index": index, "source_id": source_id, "error": "evidence_required"})
                continue
            evidence_error, evidence_detail = _validate_evidence(evidence, manifest_path)
            if evidence_error:
                result["errors"].append({
                    "index": index,
                    "source_id": source_id,
                    "error": evidence_error,
                    **evidence_detail,
                })
                continue
            item = {
                "source_id": source_id,
                "safe_through": safe.isoformat(),
                "resume_through": resume.isoformat(),
                "evidence": {**evidence, "seed_batch_id": batch_id, "seed_manifest": str(manifest_path)},
            }
            if old == safe and old_resume == resume:
                try:
                    old_evidence = json.loads(old_row.get("evidence_json") or "{}")
                except (TypeError, ValueError):
                    old_evidence = {}
                if old_evidence == item["evidence"] and old_row.get("run_id") == batch_id:
                    result["already_current"].append(item)
                else:
                    # Evidence is part of the scheduler's audit trail. Refresh it
                    # even when the verified dates are unchanged so the row never
                    # points at a stale or replaced report revision.
                    result["evidence_refresh"].append(item)
                    normalized.append(item)
            else:
                result["ready"].append(item)
                normalized.append(item)

        if result["errors"]:
            return result
        if commit:
            assessed_at = _now()
            for item in normalized:
                safe = item["safe_through"]
                resume = item["resume_through"]
                connection.execute(
                    """
                    INSERT INTO watermarks (
                        source_id, discovery_through, parsed_through, validated_through,
                        safe_through, confidence, evidence_json, run_id, assessed_at
                    ) VALUES (?, ?, ?, ?, ?, 'high', ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        discovery_through=excluded.discovery_through,
                        parsed_through=excluded.parsed_through,
                        validated_through=excluded.validated_through,
                        safe_through=excluded.safe_through,
                        confidence=excluded.confidence,
                        evidence_json=excluded.evidence_json,
                        run_id=excluded.run_id,
                        assessed_at=excluded.assessed_at
                    """,
                    (
                        item["source_id"], resume, resume, resume, safe,
                        json.dumps(item["evidence"], ensure_ascii=False), batch_id, assessed_at,
                    ),
                )
            connection.commit()
    result["committed"] = len(normalized) if commit and not result["errors"] else 0
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = seed(args.state, args.manifest, commit=args.commit)
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
