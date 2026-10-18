"""Verify a completed orchestrator audit index and its evidence files.

The verifier is read-only unless ``--output`` is supplied. It checks that the
index is structurally complete for each reached stage and that every recorded
artifact still has the exact bytes (or absence) recorded at cycle completion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ARTIFACT_KEYS = (
    "observation_manifest", "discovered_urls", "parsed_articles", "field_checks",
    "observation_issues", "observation_review", "candidate_daily_plot",
    "promotion_dry_run", "promotion_commit", "exact_postcheck",
)
READY_KEYS = ("observation_manifest", "observation_review", "candidate_daily_plot")
TERMINAL_RUN_STATUSES = {"completed", "completed_with_issues", "deferred", "blocked_storage", "idle"}
OBSERVED_TASK_STATUSES = {"needs_review", "ready_for_commit", "completed"}


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _artifact_path(record: dict[str, Any], output_root: Path) -> Path:
    location = record.get("path")
    if not isinstance(location, str) or not location:
        raise ValueError("artifact_path_missing")
    if record.get("root") == "output_root":
        if Path(location).is_absolute():
            raise ValueError("artifact_relative_path_is_absolute")
        resolved = (output_root / location).resolve()
        try:
            resolved.relative_to(output_root)
        except ValueError as exc:
            raise ValueError("artifact_outside_output_root") from exc
        return resolved
    if record.get("root") == "absolute" and Path(location).is_absolute():
        return Path(location).resolve()
    raise ValueError("artifact_root_invalid")


def verify(audit_path: Path, output_root_override: Path | None = None) -> dict[str, Any]:
    audit_path = audit_path.resolve()
    raw = audit_path.read_bytes()
    index = json.loads(raw)
    if not isinstance(index, dict):
        raise ValueError("audit_index_not_object")
    roots = index.get("roots")
    if not isinstance(roots, dict) or not isinstance(roots.get("output_root"), str):
        raise ValueError("audit_output_root_missing")
    output_root = (output_root_override or Path(roots["output_root"])).resolve()
    findings: list[dict[str, str]] = []
    if index.get("kind") != "scraping_cycle_audit_index":
        findings.append({"issue": "audit_kind_invalid"})
    if not isinstance(index.get("orchestration_run_id"), str) or not index["orchestration_run_id"]:
        findings.append({"issue": "audit_run_id_missing"})
    if index.get("runner_status") not in TERMINAL_RUN_STATUSES:
        findings.append({"issue": "audit_runner_status_not_terminal"})
    if not isinstance(index.get("commit_enabled"), bool):
        findings.append({"issue": "audit_commit_mode_missing"})
    if not isinstance(index.get("finished_at"), str):
        findings.append({"issue": "audit_finish_time_missing"})
    amendment = index.get("amendment")
    if amendment is not None:
        if not isinstance(amendment, dict) or amendment.get("kind") != "append_only_plot_recovery":
            findings.append({"issue": "audit_amendment_invalid"})
        else:
            original_location = amendment.get("original_audit_index")
            original_path = Path(original_location) if isinstance(original_location, str) else Path()
            if not original_path.is_absolute():
                original_path = audit_path.parent / original_path
            try:
                original_raw = original_path.read_bytes()
                original = json.loads(original_raw)
                if hashlib.sha256(original_raw).hexdigest() != amendment.get("original_audit_index_sha256"):
                    findings.append({"issue": "audit_amendment_original_hash_mismatch"})
                elif not isinstance(original, dict) or not isinstance(original.get("sources"), list):
                    findings.append({"issue": "audit_amendment_original_invalid"})
                else:
                    base = json.loads(raw)
                    base.pop("amendment", None)
                    source_id = amendment.get("source_id")
                    matching = [i for i, source in enumerate(base.get("sources", []))
                                if isinstance(source, dict) and source.get("source_id") == source_id]
                    if len(matching) != 1 or len(original["sources"]) != len(base.get("sources", [])):
                        findings.append({"issue": "audit_amendment_source_invalid"})
                    else:
                        number = matching[0]
                        original_source = original["sources"][number]
                        original_plot = original_source.get("artifacts", {}).get("candidate_daily_plot")
                        recovered_plot = base["sources"][number].get("artifacts", {}).get("candidate_daily_plot")
                        review_hash = original_source.get("artifacts", {}).get("observation_review", {}).get("sha256")
                        if not isinstance(original_plot, dict) or original_plot.get("exists") is not False:
                            findings.append({"issue": "audit_amendment_original_plot_not_absent"})
                        elif not isinstance(recovered_plot, dict) or recovered_plot.get("exists") is not True:
                            findings.append({"issue": "audit_amendment_recovered_plot_missing"})
                        elif (recovered_plot.get("sha256") != amendment.get("plot_sha256")
                              or review_hash != amendment.get("original_review_sha256")
                              or review_hash != amendment.get("derived_review_sha256")):
                            findings.append({"issue": "audit_amendment_evidence_mismatch"})
                        else:
                            base["sources"][number]["artifacts"]["candidate_daily_plot"] = original_plot
                            if base != original:
                                findings.append({"issue": "audit_amendment_changed_other_fields"})
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                findings.append({"issue": "audit_amendment_original_unreadable"})
    sources = index.get("sources")
    if not isinstance(sources, list):
        raise ValueError("audit_sources_not_list")
    if index.get("source_count") != len(sources):
        findings.append({"issue": "source_count_mismatch"})
    seen: set[str] = set()
    checked = 0
    for number, source in enumerate(sources):
        if not isinstance(source, dict):
            findings.append({"issue": "source_not_object", "source_index": str(number)})
            continue
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or len(source_id.split(":")) != 3:
            findings.append({"issue": "source_id_invalid", "source_index": str(number)})
            source_id = f"source_index_{number}"
        if source_id in seen:
            findings.append({"issue": "duplicate_source_id", "source_id": source_id})
        seen.add(source_id)
        parts = source_id.split(":", 2)
        if len(parts) == 3 and any(
            source.get(key) != value for key, value in
            zip(("project", "country", "domain"), parts)
        ):
            findings.append({"issue": "source_identity_mismatch", "source_id": source_id})
        for key, expected_type in (
            ("status", str), ("stage", str), ("error", dict),
            ("issue_categories", list), ("manifest_counts", dict),
            ("review", dict), ("plot_report_follow_up", str),
        ):
            if not isinstance(source.get(key), expected_type):
                findings.append({"issue": "source_field_missing", "source_id": source_id, "field": key})
        window = source.get("window")
        if not isinstance(window, dict) or not all(
            isinstance(window.get(key), str) for key in ("start", "end")
        ):
            findings.append({"issue": "window_missing", "source_id": source_id})
        artifacts = source.get("artifacts")
        if not isinstance(artifacts, dict):
            findings.append({"issue": "artifacts_not_object", "source_id": source_id})
            continue
        if artifacts:
            for key in ARTIFACT_KEYS:
                if key not in artifacts:
                    findings.append({"issue": "artifact_record_missing", "source_id": source_id, "artifact": key})
        audit_ready = source.get("audit_ready") is True
        if source.get("status") in OBSERVED_TASK_STATUSES and not audit_ready:
            findings.append({"issue": "observed_task_without_ready_audit", "source_id": source_id})
        required = set(READY_KEYS) if audit_ready else set()
        expected_rows: dict[str, int] = {}
        expected_issues_min = 0
        if audit_ready:
            counts = source.get("manifest_counts")
            fields = ("discovered", "selected", "passed", "quarantined", "skipped", "exceptions")
            if not isinstance(counts, dict) or any(
                not isinstance(counts.get(field), int) or isinstance(counts.get(field), bool)
                or counts[field] < 0 for field in fields
            ):
                findings.append({"issue": "manifest_counts_invalid", "source_id": source_id})
            else:
                selected = counts["selected"]
                parsed = selected - counts["exceptions"]
                if selected > counts["discovered"] or parsed < 0 or parsed != (
                    counts["passed"] + counts["quarantined"] + counts["skipped"]
                ):
                    findings.append({"issue": "manifest_count_accounting_mismatch", "source_id": source_id})
                else:
                    expected_rows = {
                        "discovered_urls": selected,
                        "parsed_articles": parsed,
                        "field_checks": parsed,
                    }
                    expected_issues_min = counts["exceptions"]
                    required.update(key for key, count in expected_rows.items() if count > 0)
                    if expected_issues_min:
                        required.add("observation_issues")
                    # An all-access-exception review has no article volume to
                    # plot. Its URL and issue logs, review, and source status
                    # still have to prove the block; a normal observation
                    # continues to require the candidate chart.
                    review = source.get("review")
                    if (
                        selected > 0 and counts["exceptions"] == selected
                        and source.get("status") == "needs_review"
                        and isinstance(review, dict)
                        and isinstance(review.get("discovery_failure_count"), int)
                        and review["discovery_failure_count"] > 0
                    ):
                        required.discard("candidate_daily_plot")
        if source.get("status") == "ready_for_commit":
            required.add("promotion_dry_run")
        if source.get("status") == "completed":
            required.update(("promotion_commit", "exact_postcheck"))
            if not isinstance(source.get("postcheck"), dict) or source["postcheck"].get("passed") is not True:
                findings.append({"issue": "completed_without_passing_postcheck", "source_id": source_id})
        for key in required:
            if not isinstance(artifacts.get(key), dict) or artifacts[key].get("exists") is not True:
                findings.append({"issue": "required_artifact_absent", "source_id": source_id, "artifact": key})
        url_sets: dict[str, set[str]] = {}
        exception_issue_urls: set[str] = set()
        for key, record in artifacts.items():
            if not isinstance(record, dict) or not isinstance(record.get("exists"), bool):
                findings.append({"issue": "artifact_record_invalid", "source_id": source_id, "artifact": str(key)})
                continue
            try:
                path = _artifact_path(record, output_root)
            except ValueError as exc:
                findings.append({"issue": str(exc), "source_id": source_id, "artifact": str(key)})
                continue
            exists = path.is_file()
            if exists != record["exists"]:
                findings.append({"issue": "artifact_presence_changed", "source_id": source_id, "artifact": str(key)})
                continue
            if not exists:
                if record.get("sha256") is not None:
                    findings.append({"issue": "absent_artifact_has_hash", "source_id": source_id, "artifact": str(key)})
                continue
            checked += 1
            digest, size = _sha256(path)
            if digest != record.get("sha256") or size != record.get("bytes"):
                findings.append({"issue": "artifact_bytes_changed", "source_id": source_id, "artifact": str(key)})
            if key in expected_rows or key == "observation_issues":
                row_count = 0
                invalid_json_rows = 0
                duplicate_url_rows = 0
                found_urls: set[str] = set()
                with path.open("rb") as stream:
                    for line in stream:
                        row_count += 1
                        if key == "parsed_articles":
                            continue
                        try:
                            payload = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            invalid_json_rows += 1
                            continue
                        if not isinstance(payload, dict) or not isinstance(payload.get("url"), str) or not payload["url"]:
                            invalid_json_rows += 1
                            continue
                        url = payload["url"]
                        if key in {"discovered_urls", "field_checks"} and url in found_urls:
                            duplicate_url_rows += 1
                        found_urls.add(url)
                        if key == "observation_issues" and payload.get("code") in {
                            "source_exception", "source_access_circuit_open",
                        }:
                            exception_issue_urls.add(url)
                if key in {"discovered_urls", "field_checks"}:
                    url_sets[key] = found_urls
                if invalid_json_rows:
                    findings.append({"issue": "artifact_jsonl_invalid", "source_id": source_id, "artifact": str(key)})
                if duplicate_url_rows:
                    findings.append({"issue": "artifact_duplicate_url", "source_id": source_id, "artifact": str(key)})
                if key in expected_rows and row_count != expected_rows[key]:
                    findings.append({"issue": "artifact_row_count_mismatch", "source_id": source_id, "artifact": str(key)})
                if key == "observation_issues" and row_count < expected_issues_min:
                    findings.append({"issue": "exception_issue_count_mismatch", "source_id": source_id, "artifact": str(key)})
        if audit_ready and expected_rows:
            inventory = url_sets.get("discovered_urls", set())
            field_urls = url_sets.get("field_checks", set())
            if field_urls & exception_issue_urls or inventory != field_urls | exception_issue_urls:
                findings.append({"issue": "per_url_accounting_mismatch", "source_id": source_id})
            if len(exception_issue_urls) != expected_issues_min:
                findings.append({"issue": "exception_url_count_mismatch", "source_id": source_id})
    return {
        "schema_version": 1,
        "kind": "scraping_cycle_audit_verification",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "audit_index": str(audit_path),
        "audit_index_sha256": hashlib.sha256(raw).hexdigest(),
        "orchestration_run_id": index.get("orchestration_run_id"),
        "output_root": str(output_root),
        "source_count": len(sources),
        "verified_artifact_count": checked,
        "findings": findings,
        "passed": not findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_index", type=Path)
    parser.add_argument("--output-root", type=Path, help="Replacement output root for copied evidence")
    parser.add_argument("--output", type=Path, help="Write a JSON verification receipt")
    args = parser.parse_args(argv)
    try:
        result = verify(args.audit_index, args.output_root)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"audit_verification_error: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, args.output)
    print(rendered, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
