#!/usr/bin/env python3
"""Observe a stateless pure-network adapter with bounded parallel fetches.

The standard observer intentionally processes URLs serially. Large sitemap
sources can take hours, even though pure adapters have no shared database or
pipeline state. This runner keeps the same immutable artifact contract while
fetching independent article pages in a bounded thread pool. Results are
written in discovery order so the artifact remains deterministic and easy to
audit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import traceback
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _target_url_sha256(urls: list[str]) -> str:
    canonical = "".join(f"{url}\n" for url in dict.fromkeys(url.strip() for url in urls if url.strip()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_json_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path.name}")
    return value


def _verify_contract_pin(value: object, field: str) -> tuple[Path, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} pin is missing")
    path = Path(str(value.get("path", ""))).resolve()
    digest = str(value.get("sha256", ""))
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{field} SHA-256 is invalid")
    if not path.is_file() or _sha256(path) != digest:
        raise ValueError(f"{field} SHA-256 mismatch")
    return path, digest


def _count_hash(value: object, field: str) -> tuple[int, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must contain count and SHA-256")
    count = value.get("count")
    digest = str(value.get("sha256", ""))
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"{field} count is invalid")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{field} SHA-256 is invalid")
    return count, digest


def _identity_jsonl_facts(path: Path, digest: str, source_id: str, field: str) -> dict:
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, Mapping) or row.get("source_id") != source_id:
            raise ValueError(f"{field} contains an invalid source identity")
        url = str(row.get("url", "")).strip()
        if not url:
            raise ValueError(f"{field} contains a missing URL")
        urls.append(url)
    if len(urls) != len(set(urls)):
        raise ValueError(f"{field} contains duplicate identities")
    return {
        "count": len(urls),
        "sha256": digest,
        "url_set_sha256": _target_url_sha256(urls),
        "_urls": frozenset(urls),
    }


def _assert_path_free_handoff(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in {
                "path", "run_dir", "runtime_root", "output_root", "receipt_root",
                "closed_runtime", "parent_run_dir",
            } or normalized.endswith("_path"):
                raise ValueError("sealed classification handoff contains a local/runtime path")
            _assert_path_free_handoff(child)
    elif isinstance(value, list):
        for child in value:
            _assert_path_free_handoff(child)
    elif isinstance(value, str) and Path(value).is_absolute():
        raise ValueError("sealed classification handoff contains an absolute path")


def _sealed_classification_provenance(
    value: object, source_name: str, explicit_urls: list[str],
) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("sealed classification parent contract is missing")
    required = {
        "handoff_manifest", "worklist", "classification_seal",
        "independent_audit", "source_summary", "preserved_passes",
        "verified_exclusions", "classified_source_ledger",
    }
    if set(value) != required:
        raise ValueError("sealed classification parent must pin every required artifact")
    pins = {name: _verify_contract_pin(value[name], name) for name in required}
    worklist_path, worklist_sha = pins["worklist"]
    worklist_urls = [
        line.strip() for line in worklist_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(worklist_urls) != len(set(worklist_urls)) or worklist_urls != explicit_urls:
        raise ValueError("sealed classification worklist does not match the exact URL input")
    worklist_url_set_sha = _target_url_sha256(worklist_urls)
    manifest = _read_json_object(pins["handoff_manifest"][0])
    _assert_path_free_handoff(manifest)
    source_id = str(manifest.get("source_id", ""))
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "sealed_classification_exact_handoff"
        or manifest.get("status") != "sealed"
        or manifest.get("source_name") != source_name
        or source_id.split(":", 2)[-1] != source_name
    ):
        raise ValueError("sealed classification handoff identity is invalid")
    worklist = manifest.get("worklist")
    if not isinstance(worklist, Mapping) or (
        worklist.get("count") != len(worklist_urls)
        or worklist.get("sha256") != worklist_sha
        or worklist.get("url_set_sha256") != worklist_url_set_sha
    ):
        raise ValueError("sealed classification worklist count, hash, or URL set is invalid")
    if (
        manifest.get("classification_seal_sha256") != pins["classification_seal"][1]
        or manifest.get("independent_audit_sha256") != pins["independent_audit"][1]
        or manifest.get("source_summary_sha256") != pins["source_summary"][1]
    ):
        raise ValueError("sealed classification artifact hash is invalid")
    preserved_count, preserved_sha = _count_hash(
        manifest.get("preserved_passes"), "preserved_passes",
    )
    exclusion_count, exclusion_sha = _count_hash(
        manifest.get("verified_exclusions"), "verified_exclusions",
    )
    preserved_facts = _identity_jsonl_facts(
        pins["preserved_passes"][0], pins["preserved_passes"][1], source_id,
        "preserved_passes",
    )
    exclusion_facts = _identity_jsonl_facts(
        pins["verified_exclusions"][0], pins["verified_exclusions"][1], source_id,
        "verified_exclusions",
    )
    classified_facts = _identity_jsonl_facts(
        pins["classified_source_ledger"][0], pins["classified_source_ledger"][1],
        source_id, "classified_source_ledger",
    )
    classified_urls = {
        str(json.loads(line).get("url", "")).strip()
        for line in pins["classified_source_ledger"][0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    preserved_urls = set(preserved_facts["_urls"])
    exclusion_urls = set(exclusion_facts["_urls"])
    worklist_url_set = set(worklist_urls)
    if (
        worklist_url_set & preserved_urls
        or worklist_url_set & exclusion_urls
        or preserved_urls & exclusion_urls
    ):
        raise ValueError("sealed classification identity partitions overlap")
    if (
        (preserved_count, preserved_sha)
        != (preserved_facts["count"], preserved_facts["sha256"])
        or manifest.get("preserved_passes", {}).get("url_set_sha256")
        != preserved_facts["url_set_sha256"]
        or (exclusion_count, exclusion_sha)
        != (exclusion_facts["count"], exclusion_facts["sha256"])
        or manifest.get("verified_exclusions", {}).get("url_set_sha256")
        != exclusion_facts["url_set_sha256"]
        or manifest.get("classified_source_ledger") != {
            key: classified_facts[key] for key in ("count", "sha256", "url_set_sha256")
        }
        or not set(worklist_urls).issubset(classified_urls)
    ):
        raise ValueError("sealed preserved-pass or exclusion ledger facts are invalid")
    summary = _read_json_object(pins["source_summary"][0])
    _assert_path_free_handoff(summary)
    if (
        summary.get("source_id") != source_id
        or summary.get("classified_count") != classified_facts["count"]
        or _count_hash(summary.get("preserved_passes"), "source_summary.preserved_passes")
        != (preserved_count, preserved_sha)
        or _count_hash(summary.get("verified_exclusions"), "source_summary.verified_exclusions")
        != (exclusion_count, exclusion_sha)
    ):
        raise ValueError("sealed classification source summary does not match the handoff")
    audit = _read_json_object(pins["independent_audit"][0])
    _assert_path_free_handoff(audit)
    seal = _read_json_object(pins["classification_seal"][0])
    _assert_path_free_handoff(seal)
    source_code = source_id.split(":", 2)[1] if source_id.count(":") >= 2 else ""
    audit_summary = audit.get("check_summary")
    audit_sources = audit.get("sources")
    audit_source = audit_sources.get(source_code) if isinstance(audit_sources, Mapping) else None
    audited = audit.get("audited_artifacts")
    output_hashes = {
        item.get("sha256") for item in seal.get("output_artifacts", [])
        if isinstance(item, Mapping)
    }
    if (
        seal.get("kind") != "private_offline_classification_seal"
        or seal.get("status") != "sealed_pending_independent_audit"
        or pins["source_summary"][1] not in output_hashes
        or pins["classified_source_ledger"][1] not in output_hashes
        or audit.get("kind") != "private_independent_offline_source_local_classification_audit"
        or audit.get("result") != "approved"
        or not isinstance(audit_summary, Mapping)
        or audit_summary.get("failed") != 0
        or not isinstance(audit_summary.get("total"), int)
        or audit_summary.get("total", 0) <= 0
        or audit_summary.get("passed") != audit_summary.get("total")
        or not isinstance(audit.get("failed_checks"), list)
        or audit.get("failed_checks")
        or not isinstance(audited, Mapping)
        or audited.get("classification_seal_sha256") != pins["classification_seal"][1]
        or not isinstance(audit_source, Mapping)
        or audit_source.get("classified_count") != classified_facts["count"]
        or audit_source.get("preserved_pass_count") != preserved_count
        or audit_source.get("verified_exclusion_count") != exclusion_count
    ):
        raise ValueError("sealed classification independent audit is not approved for the source")
    return {
        "kind": "sealed_classification_parent",
        "source_id": source_id,
        "target_url_count": len(worklist_urls),
        "target_url_set_sha256": worklist_url_set_sha,
        "worklist_sha256": worklist_sha,
        "classification_seal_sha256": pins["classification_seal"][1],
        "independent_audit_sha256": pins["independent_audit"][1],
        "source_summary_sha256": pins["source_summary"][1],
        "handoff_manifest_sha256": pins["handoff_manifest"][1],
        "preserved_pass_count": preserved_count,
        "preserved_pass_sha256": preserved_sha,
        "verified_exclusion_count": exclusion_count,
        "verified_exclusion_sha256": exclusion_sha,
        "preserved_pass_url_set_sha256": preserved_facts["url_set_sha256"],
        "verified_exclusion_url_set_sha256": exclusion_facts["url_set_sha256"],
        "classified_source_count": classified_facts["count"],
        "classified_source_ledger_sha256": classified_facts["sha256"],
        "classified_source_url_set_sha256": classified_facts["url_set_sha256"],
    }


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"parallel_observed_{path.stem}", str(path))
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load source module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observe_parallel(
    automation_root: Path,
    source_file: Path,
    start: date,
    end: date,
    output_root: Path,
    workers: int = 12,
    explicit_urls: list[str] | None = None,
    parent_run_dir: Path | None = None,
    derivation_reason: str | None = None,
    sealed_classification_parent: Mapping[str, object] | None = None,
) -> Path:
    automation_root = automation_root.resolve()
    source_file = source_file.resolve()
    sys.path.insert(0, str(automation_root))
    from scraper_control.evidence import compare_required_evidence
    from scraper_control.observe import adapter_bundle_sha256
    from scraper_control.validation import (
        resolve_policy_exclusion,
        resolve_with_page_evidence,
        validate_article,
    )

    module = _load_module(source_file)
    if not callable(getattr(module, "discover_urls", None)) or not callable(getattr(module, "scrape_url", None)):
        raise ValueError("parallel observer requires a pure-network adapter")
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    config = module.Config()
    derived_from = None
    if parent_run_dir is not None and sealed_classification_parent is not None:
        raise ValueError("legacy and sealed classification parents are mutually exclusive")
    if parent_run_dir is not None:
        if explicit_urls is None:
            raise ValueError("parent provenance is allowed only with an exact URL set")
        parent_run_dir = parent_run_dir.resolve()
        parent_manifest_path = parent_run_dir / "manifest.json"
        parent_manifest = json.loads(parent_manifest_path.read_text(encoding="utf-8"))
        if parent_manifest.get("status") != "observed":
            raise ValueError("parent observation must have observed status")
        if parent_manifest.get("source") != config.source_name:
            raise ValueError("parent observation source does not match the repair adapter")
        artifact_names = ("manifest.json", "urls.jsonl", "field_checks.jsonl", "issues.jsonl")
        derived_from = {
            "parent_run_dir": str(parent_run_dir),
            "parent_run_id": parent_manifest.get("run_id"),
            "parent_source_sha256": parent_manifest.get("source_sha256"),
            "parent_source_bundle_sha256": parent_manifest.get("source_bundle_sha256"),
            "parent_artifact_sha256": {
                name: _sha256(parent_run_dir / name)
                for name in artifact_names if (parent_run_dir / name).is_file()
            },
            "target_url_count": len(dict.fromkeys(url.strip() for url in explicit_urls if url.strip())),
            "target_url_set_sha256": _target_url_sha256(explicit_urls),
            "reason": derivation_reason or "targeted_reprobe",
        }
    elif sealed_classification_parent is not None:
        if explicit_urls is None:
            raise ValueError("sealed classification provenance requires an exact URL set")
        derived_from = _sealed_classification_provenance(
            sealed_classification_parent, config.source_name,
            list(dict.fromkeys(url.strip() for url in explicit_urls if url.strip())),
        )
        derived_from["reason"] = derivation_reason or "sealed_classification_exact_repair"
    run_id = f"observe-parallel-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc)
    manifest = {
        "run_id": run_id,
        "mode": "observation_no_production_writes",
        "source": config.source_name,
        "country": config.country_code,
        "is_env": bool(getattr(config, "is_env", False)),
        "source_file": str(source_file),
        "source_sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
        "source_bundle_sha256": adapter_bundle_sha256(source_file),
        "requested_start": start,
        "requested_end": end,
        "config_overrides": {},
        "reverse_urls": False,
        "started_at": started,
        "status": "running",
        "adapter_contract": "pure_network_only",
        "observer_execution": {"kind": "bounded_thread_pool", "workers": workers},
        "limitations": [
            "Discovery evidence records the requested sitemap/listing coverage but does not prove the publisher exposed every article.",
            "Safe-through requires anomaly and postprocessing gates after this observation.",
        ],
    }
    if derived_from is not None:
        manifest["derived_from"] = derived_from
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=_json_default) + "\n", encoding="utf-8"
    )
    counts = {"discovered": 0, "selected": 0, "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}
    try:
        discovery_evidence: dict = {}
        if explicit_urls is None:
            urls = list(dict.fromkeys(module.discover_urls(start, end, discovery_evidence)))
        else:
            urls = list(dict.fromkeys(url.strip() for url in explicit_urls if url.strip()))
            discovery_evidence.update({
                "strategy": "exact_reviewed_url_set",
                "coverage_complete": True,
                "explicit_url_count": len(urls),
                "scope": "targeted legacy-row repair evidence; not source-wide publication discovery",
            })
        manifest["discovery_evidence"] = discovery_evidence
        manifest["discovery_method"] = "pure_adapter_parallel"
        manifest["pipeline_class"] = None
        counts["discovered"] = counts["selected"] = len(urls)
        _write_jsonl(run_dir / "urls.jsonl", [
            {"url": url, "discovery_method": "pure_adapter_parallel"} for url in urls
        ])

        def process(url: str) -> dict:
            try:
                article = dict(module.scrape_url(url))
                article.setdefault("url", url)
                article.setdefault("source_domain", config.source_name)
                article.setdefault("country_code", config.country_code)
                article.setdefault("parser", "pure_adapter")
                article.setdefault("collection", None)
                article.setdefault("parser_error", None)
                base = validate_article(article, start, end, config.source_name)
                evidence = article.get("field_evidence") or {}
                evidence_checks = compare_required_evidence(article, evidence) if evidence else []
                semantic_eligible, resolutions = resolve_with_page_evidence(base, evidence_checks, evidence)
                is_excluded = article.get("collection") == "SKIP"
                exclusion_reason = article.get("exclusion_reason")
                exclusion_evidence = article.get("exclusion_evidence")
                exclusion_valid, exclusion_resolution = resolve_policy_exclusion(
                    base, evidence_checks, evidence, exclusion_reason, exclusion_evidence,
                    article_url=article.get("url"),
                )
                excluded = bool(is_excluded and exclusion_valid and not article.get("parser_error"))
                eligible = bool(semantic_eligible and not is_excluded and not article.get("parser_error"))
                check = {
                    "url": url,
                    "eligible": eligible,
                    "fields": [asdict(item) for item in [*base.fields, *evidence_checks]],
                    "page_evidence": evidence,
                    "automatic_resolutions": resolutions,
                    "excluded": excluded,
                    "exclusion_reason": exclusion_reason,
                    "exclusion_evidence": exclusion_evidence,
                    "exclusion_resolution": exclusion_resolution,
                }
                issues = []
                if excluded:
                    outcome = "skipped"
                    issues.append({
                        "url": url, "source": config.source_name, "stage": "source_policy",
                        "category": "collection", "code": exclusion_reason, "severity": "info",
                        "status": "excluded", "observed": exclusion_evidence, "attempt": 1,
                    })
                elif eligible:
                    outcome = "passed"
                    for resolution in resolutions:
                        issues.append({
                            "url": url, "source": config.source_name, "stage": "validation",
                            "category": resolution["field"], "code": resolution["code"],
                            "severity": "info", "status": "resolved", "observed": resolution,
                            "attempt": 1,
                        })
                else:
                    outcome = "quarantined"
                    if is_excluded and not (exclusion_reason and exclusion_evidence):
                        issues.append({
                            "url": url, "source": config.source_name, "stage": "source_policy",
                            "category": "collection", "code": "exclusion_policy_evidence_missing",
                            "severity": "error", "status": "open",
                            "observed": {"reason": exclusion_reason, "evidence": exclusion_evidence},
                            "attempt": 1,
                        })
                    if article.get("parser_error"):
                        issues.append({
                            "url": url, "source": config.source_name, "stage": "parse",
                            "category": "content", "code": "adapter_parser_error",
                            "severity": "error", "status": "open",
                            "observed": {
                                "parser_error": article["parser_error"],
                                "field_sources": article.get("field_sources"),
                                "page_evidence": evidence,
                            },
                            "attempt": 1,
                        })
                    for field in [*base.fields, *evidence_checks]:
                        for code in field.codes:
                            issues.append({
                                "url": url, "source": config.source_name, "stage": "validation",
                                "category": field.field, "code": code, "severity": "error",
                                "status": "open", "observed": field.evidence, "attempt": 1,
                            })
                return {"url": url, "article": article, "check": check, "issues": issues, "outcome": outcome}
            except Exception as exc:
                return {
                    "url": url,
                    "outcome": "exceptions",
                    "issues": [{
                        "url": url, "source": config.source_name, "stage": "parse",
                        "category": "infrastructure", "code": "source_exception", "severity": "error",
                        "status": "open", "observed": {"type": type(exc).__name__, "message": str(exc)},
                        "attempt": 1,
                    }],
                    "error": {"url": url, "type": type(exc).__name__, "message": str(exc)},
                }

        output_names = ("articles.jsonl", "field_checks.jsonl", "issues.jsonl", "errors.jsonl")
        streams = {name: (run_dir / name).open("w", encoding="utf-8") for name in output_names}
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pure-observer") as pool:
                for index, item in enumerate(pool.map(process, urls), start=1):
                    counts[item["outcome"]] += 1
                    for name, values in (
                        ("articles.jsonl", [item["article"]] if item.get("article") is not None else []),
                        ("field_checks.jsonl", [item["check"]] if item.get("check") is not None else []),
                        ("issues.jsonl", item.get("issues", [])),
                        ("errors.jsonl", [item["error"]] if item.get("error") is not None else []),
                    ):
                        for value in values:
                            streams[name].write(json.dumps(value, ensure_ascii=False, default=_json_default) + "\n")
                    if index % 250 == 0 or index == len(urls):
                        for stream in streams.values():
                            stream.flush()
                        manifest["progress"] = {
                            "processed": index,
                            "selected": len(urls),
                            "updated_at": datetime.now(timezone.utc),
                        }
                        (run_dir / "manifest.json").write_text(
                            json.dumps(manifest, indent=2, default=_json_default) + "\n", encoding="utf-8"
                        )
        finally:
            for stream in streams.values():
                stream.close()
        manifest["status"] = "observed"
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["fatal_error"] = {
            "type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()
        }
        raise
    finally:
        finished = datetime.now(timezone.utc)
        manifest["finished_at"] = finished
        manifest["runtime_seconds"] = (finished - started).total_seconds()
        manifest["counts"] = counts
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=_json_default) + "\n", encoding="utf-8"
        )
        (run_dir / "summary.md").write_text(
            f"# Parallel observation for {config.source_name}\n\n"
            f"- Run: `{run_id}`\n- Status: {manifest['status']}\n"
            f"- Window: {start} through {end}\n- Workers: {workers}\n"
            f"- Discovered / selected: {counts['discovered']} / {counts['selected']}\n"
            f"- Passed / quarantined / skipped / exceptions: {counts['passed']} / {counts['quarantined']} / {counts['skipped']} / {counts['exceptions']}\n",
            encoding="utf-8",
        )
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--automation-root", required=True, type=Path)
    parser.add_argument("--source-file", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument(
        "--urls-file", type=Path,
        help="Optional newline-delimited exact URL set for a targeted repair observation",
    )
    parser.add_argument(
        "--parent-run-dir", type=Path,
        help="Immutable parent observation recorded for an exact targeted reprobe",
    )
    parser.add_argument(
        "--derivation-reason",
        help="Machine-readable or short human-readable reason for the targeted reprobe",
    )
    for name in (
        "handoff-manifest", "worklist", "classification-seal",
        "independent-audit", "source-summary", "preserved-passes",
        "verified-exclusions", "classified-source-ledger",
    ):
        parser.add_argument("--sealed-classification-%s" % name, type=Path)
        parser.add_argument("--sealed-classification-%s-sha256" % name)
    args = parser.parse_args()
    explicit_urls = args.urls_file.read_text(encoding="utf-8").splitlines() if args.urls_file else None
    sealed_names = (
        "handoff_manifest", "worklist", "classification_seal",
        "independent_audit", "source_summary", "preserved_passes",
        "verified_exclusions", "classified_source_ledger",
    )
    sealed_values = [
        getattr(args, "sealed_classification_%s" % name)
        for name in sealed_names
    ] + [
        getattr(args, "sealed_classification_%s_sha256" % name)
        for name in sealed_names
    ]
    sealed_parent = None
    if any(value is not None for value in sealed_values):
        if any(value is None for value in sealed_values):
            parser.error("all sealed classification artifact paths and SHA-256 pins are required")
        sealed_parent = {
            name: {
                "path": getattr(args, "sealed_classification_%s" % name),
                "sha256": getattr(args, "sealed_classification_%s_sha256" % name),
            }
            for name in sealed_names
        }
    print(observe_parallel(
        args.automation_root, args.source_file, args.start, args.end, args.output_root, args.workers,
        explicit_urls, args.parent_run_dir, args.derivation_reason, sealed_parent,
    ))


if __name__ == "__main__":
    main()
