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
    args = parser.parse_args()
    explicit_urls = args.urls_file.read_text(encoding="utf-8").splitlines() if args.urls_file else None
    print(observe_parallel(
        args.automation_root, args.source_file, args.start, args.end, args.output_root, args.workers,
        explicit_urls, args.parent_run_dir, args.derivation_reason,
    ))


if __name__ == "__main__":
    main()
