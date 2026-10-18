"""Recover transient parse exceptions without repeating URL discovery.

The recovery path accepts only a completed, full, pure-adapter observation.
It reads the parent's immutable discovery artifacts, retries only URLs whose
parse result is missing or recorded as an exception, and creates a separate
observation.  It never imports the legacy pipeline or opens a database.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from .evidence import compare_required_evidence
from .observe import adapter_bundle_sha256
from .validation import resolve_policy_exclusion, resolve_with_page_evidence, validate_article


PARENT_ARTIFACTS = ("manifest.json", "urls.jsonl", "articles.jsonl", "field_checks.jsonl", "issues.jsonl")


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _read_jsonl(path: Path, *, required: bool = True) -> list[dict]:
    if not path.exists():
        if required:
            raise ValueError(f"Parent artifact is missing: {path.name}")
        return []
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid JSON in {path.name} line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Expected an object in {path.name} line {line_number}")
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_hashes(parent: Path) -> dict[str, str]:
    return {name: _sha256(parent / name) for name in PARENT_ARTIFACTS if (parent / name).exists()}


def _unique_rows_by_url(rows: Iterable[dict], artifact: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in rows:
        url = row.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"{artifact} contains a row without a URL")
        if url in result:
            raise ValueError(f"{artifact} contains duplicate URL: {url}")
        result[url] = row
    return result


def _load_module(path: Path):
    module_name = f"recovered_source_{path.stem}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load source module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_recovered(raw: Mapping[str, object], url: str, manifest: Mapping[str, object]) -> dict:
    article = dict(raw)
    article.setdefault("url", url)
    if article.get("url") != url:
        raise ValueError(f"Adapter returned a different URL for {url}: {article.get('url')}")
    article.setdefault("source_domain", manifest["source"])
    article.setdefault("country_code", manifest["country"])
    article.setdefault("parser", "pure_adapter_recovery")
    article.setdefault("collection", None)
    article.setdefault("parser_error", None)
    return article


def _validate_one(
    article: dict,
    *,
    url: str,
    source: str,
    start: date,
    end: date,
) -> tuple[dict, list[dict], str]:
    """Return a fresh check, its issues, and its accounting bucket."""
    base = validate_article(article, start, end, source)
    raw_evidence = article.get("field_evidence")
    evidence = dict(raw_evidence) if isinstance(raw_evidence, dict) else {}
    evidence_checks = compare_required_evidence(article, evidence)
    all_checks = [*base.fields, *evidence_checks]
    semantic_eligible, resolutions = resolve_with_page_evidence(base, evidence_checks, evidence)
    is_excluded = article.get("collection") == "SKIP"
    reason = article.get("exclusion_reason")
    exclusion_evidence = article.get("exclusion_evidence")
    exclusion_valid, exclusion_resolution = resolve_policy_exclusion(
        base, evidence_checks, evidence, reason, exclusion_evidence,
        article_url=article.get("url"),
    )
    excluded = bool(is_excluded and exclusion_valid)
    eligible = bool(semantic_eligible and not is_excluded and not article.get("parser_error"))
    check = {
        "url": url,
        "eligible": eligible,
        "fields": [asdict(item) for item in all_checks],
        "page_evidence": evidence,
        "automatic_resolutions": resolutions,
        "excluded": excluded,
        "exclusion_reason": reason,
        "exclusion_evidence": exclusion_evidence,
        "exclusion_resolution": exclusion_resolution,
    }
    issues: list[dict] = []
    if excluded:
        bucket = "skipped"
        issues.append({
            "url": url, "source": source, "stage": "source_policy",
            "category": "collection", "code": reason, "severity": "info",
            "status": "excluded", "observed": exclusion_evidence, "attempt": 1,
        })
    elif eligible:
        bucket = "passed"
        for resolution in resolutions:
            issues.append({
                "url": url, "source": source, "stage": "validation",
                "category": resolution["field"], "code": resolution["code"],
                "severity": "info", "status": "resolved", "observed": resolution,
                "attempt": 1,
            })
    else:
        bucket = "quarantined"
        if is_excluded and not (reason and exclusion_evidence):
            issues.append({
                "url": url, "source": source, "stage": "source_policy",
                "category": "collection", "code": "exclusion_policy_evidence_missing",
                "severity": "error", "status": "open",
                "observed": {"reason": reason, "evidence": exclusion_evidence}, "attempt": 1,
            })
        if article.get("parser_error"):
            issues.append({
                "url": url, "source": source, "stage": "parse",
                "category": "content", "code": "adapter_parser_error",
                "severity": "error", "status": "open",
                "observed": {"parser_error": article.get("parser_error")}, "attempt": 1,
            })
        for field in all_checks:
            for code in field.codes:
                issues.append({
                    "url": url, "source": source, "stage": "derived_validation",
                    "category": field.field, "code": code, "severity": "error",
                    "status": "open", "observed": field.evidence, "attempt": 1,
                })
    return check, issues, bucket


def _preflight(parent: Path, source_file: Optional[Path]) -> tuple[dict, Path, list[dict], dict[str, dict], set[str], dict[str, str]]:
    manifest_path = parent / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("Parent manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Parent manifest must be an object")
    if manifest.get("status") != "observed":
        raise ValueError("Parent observation is not complete")
    if manifest.get("adapter_contract") != "pure_network_only":
        raise ValueError("Recovery accepts only pure-network adapter observations")
    counts = manifest.get("counts") or {}
    discovered, selected = counts.get("discovered"), counts.get("selected")
    if not isinstance(discovered, int) or discovered < 0 or selected != discovered:
        raise ValueError("Parent must contain the full discovered URL set without sampling")
    if not isinstance(counts.get("exceptions"), int) or counts.get("exceptions", 0) <= 0:
        raise ValueError("Parent has no parse exceptions to recover")
    urls = _read_jsonl(parent / "urls.jsonl")
    if len(urls) != discovered:
        raise ValueError("Parent URL artifact does not match discovered count")
    ordered_urls = []
    seen = set()
    for row in urls:
        url = row.get("url")
        if not isinstance(url, str) or not url or url in seen:
            raise ValueError("Parent URL artifact contains a missing or duplicate URL")
        seen.add(url)
        ordered_urls.append(row)
    articles = _unique_rows_by_url(_read_jsonl(parent / "articles.jsonl", required=False), "articles.jsonl")
    checks = _unique_rows_by_url(_read_jsonl(parent / "field_checks.jsonl", required=False), "field_checks.jsonl")
    issues = _read_jsonl(parent / "issues.jsonl", required=False)
    unknown = (set(articles) | set(checks) | {str(row.get("url")) for row in issues}) - seen
    if unknown:
        raise ValueError(f"Parent artifacts contain URLs outside discovery: {sorted(unknown)}")
    parse_exception_urls = {
        row["url"] for row in issues
        if row.get("stage") == "parse" and row.get("code") == "source_exception" and row.get("status") == "open"
    }
    targets = parse_exception_urls | (seen - set(articles)) | (seen - set(checks))
    if len(targets) != counts["exceptions"]:
        raise ValueError(
            "Parent exception accounting is inconsistent: "
            f"manifest={counts['exceptions']} artifact_urls={len(targets)}"
        )
    for url in seen - targets:
        if url not in articles or url not in checks:
            raise ValueError(f"Parent successful URL lacks article/check pair: {url}")
    source = (source_file or Path(str(manifest.get("source_file") or ""))).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Adapter source file does not exist: {source}")
    source_sha = _sha256(source)
    if source_sha != manifest.get("source_sha256"):
        raise ValueError("Adapter changed after parent observation; run a new full observation")
    bundle_sha = adapter_bundle_sha256(source)
    if not manifest.get("source_bundle_sha256"):
        raise ValueError("Parent lacks an adapter dependency bundle hash")
    if bundle_sha != manifest["source_bundle_sha256"]:
        raise ValueError("Adapter or sibling common.py changed after parent observation; run a new full observation")
    accounted = sum(int(counts.get(key, 0) or 0) for key in ("passed", "quarantined", "skipped", "exceptions"))
    if accounted != selected:
        raise ValueError("Parent result counts do not account for every selected URL")
    return manifest, source, ordered_urls, articles, targets, _artifact_hashes(parent)


def recover_observation(
    parent_run: Path,
    output_root: Path,
    *,
    source_file: Optional[Path] = None,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 1.0,
    backoff_multiplier: float = 2.0,
    max_backoff_seconds: float = 10.0,
    pacing_seconds: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> Path:
    """Create a derived observation by retrying only parent exception URLs."""
    if not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be between 1 and 10")
    if initial_backoff_seconds < 0 or max_backoff_seconds < 0 or backoff_multiplier < 1:
        raise ValueError("Backoff values must be nonnegative and multiplier must be at least 1")
    if not math.isfinite(pacing_seconds) or pacing_seconds < 0:
        raise ValueError("pacing_seconds must be a finite nonnegative number")
    parent = parent_run.expanduser().resolve()
    manifest, source, url_rows, original_articles, targets, parent_hashes = _preflight(parent, source_file)
    source_sha = _sha256(source)
    bundle_sha = adapter_bundle_sha256(source)
    sys.path.insert(0, str(source.parent.parent.parent if source.parent.name != "adapters" else source.parent.parent))
    module = _load_module(source)
    if not callable(getattr(module, "discover_urls", None)) or not callable(getattr(module, "scrape_url", None)):
        raise ValueError("Current adapter no longer satisfies the pure-network contract")
    config = module.Config()
    if getattr(config, "source_name", None) != manifest.get("source") or getattr(config, "country_code", None) != manifest.get("country"):
        raise ValueError("Current adapter identity differs from the parent manifest")

    started = datetime.now(timezone.utc)
    attempts: dict[str, list[dict]] = {url: [] for url in targets}
    recovered: dict[str, dict] = {}
    exhausted: dict[str, dict] = {}
    ordered_target_urls = [row["url"] for row in url_rows if row["url"] in targets]
    for target_index, url in enumerate(ordered_target_urls):
        for attempt in range(1, max_attempts + 1):
            attempt_started = datetime.now(timezone.utc)
            try:
                article = _normalize_recovered(dict(module.scrape_url(url)), url, manifest)
                attempts[url].append({
                    "attempt": attempt, "status": "recovered",
                    "started_at": attempt_started.isoformat(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                recovered[url] = article
                break
            except Exception as exc:
                failure = {
                    "attempt": attempt, "status": "failed", "type": type(exc).__name__,
                    "message": str(exc), "started_at": attempt_started.isoformat(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
                attempts[url].append(failure)
                exhausted[url] = failure
                if attempt < max_attempts:
                    delay = min(max_backoff_seconds, initial_backoff_seconds * (backoff_multiplier ** (attempt - 1)))
                    if delay:
                        sleeper(delay)
        if url in recovered:
            exhausted.pop(url, None)
        if pacing_seconds and target_index < len(ordered_target_urls) - 1:
            sleeper(pacing_seconds)

    # Fail closed if code or parent artifacts moved while network retries ran.
    if _sha256(source) != source_sha or adapter_bundle_sha256(source) != bundle_sha:
        raise ValueError("Adapter bundle changed during recovery; no derived run was created")
    if _artifact_hashes(parent) != parent_hashes:
        raise ValueError("Parent observation changed during recovery; no derived run was created")

    start = date.fromisoformat(str(manifest["requested_start"]))
    end = date.fromisoformat(str(manifest["requested_end"]))
    # A target may have an article row when the original exception happened
    # during evidence validation after that row was appended.  Do not treat
    # such a partial result as successful when its retry was exhausted.
    final_articles = {url: row for url, row in original_articles.items() if url not in targets}
    final_articles.update(recovered)
    article_rows: list[dict] = []
    check_rows: list[dict] = []
    issue_rows: list[dict] = []
    counts = {
        "discovered": len(url_rows), "selected": len(url_rows), "passed": 0,
        "quarantined": 0, "skipped": 0, "exceptions": 0,
    }
    for url_row in url_rows:
        url = url_row["url"]
        article = final_articles.get(url)
        if article is None:
            counts["exceptions"] += 1
            failure = exhausted[url]
            issue_rows.append({
                "url": url, "source": manifest["source"], "stage": "parse",
                "category": "infrastructure", "code": "source_exception",
                "severity": "error", "status": "open",
                "observed": {
                    "type": failure["type"], "message": failure["message"],
                    "recovery_attempts": attempts[url],
                    "parent_run_id": manifest["run_id"],
                },
                "attempt": len(attempts[url]),
            })
            continue
        article_rows.append(article)
        check, validation_issues, bucket = _validate_one(
            article, url=url, source=str(manifest["source"]), start=start, end=end,
        )
        check_rows.append(check)
        counts[bucket] += 1
        if url in recovered:
            issue_rows.append({
                "url": url, "source": manifest["source"], "stage": "parse_recovery",
                "category": "infrastructure", "code": "source_exception_recovered",
                "severity": "info", "status": "resolved",
                "observed": {"attempts": attempts[url], "parent_run_id": manifest["run_id"]},
                "attempt": len(attempts[url]),
            })
        issue_rows.extend(validation_issues)

    finished = datetime.now(timezone.utc)
    run_id = f"observe-recovered-{finished.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output = output_root.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".recovering-", dir=output))
    final_dir = output / run_id
    try:
        _write_jsonl(temporary / "urls.jsonl", url_rows)
        _write_jsonl(temporary / "articles.jsonl", article_rows)
        _write_jsonl(temporary / "field_checks.jsonl", check_rows)
        _write_jsonl(temporary / "issues.jsonl", issue_rows)
        derived_manifest = dict(manifest)
        derived_manifest.update({
            "run_id": run_id,
            "mode": "observation_recovery_no_production_writes",
            "status": "observed",
            "source_file": str(source),
            "source_sha256": source_sha,
            "source_bundle_sha256": bundle_sha,
            "started_at": started,
            "finished_at": finished,
            "runtime_seconds": (finished - started).total_seconds(),
            "counts": counts,
            "discovery_evidence": json.loads(json.dumps(manifest.get("discovery_evidence") or {})),
            "derived_from_run_id": manifest["run_id"],
            "derivation": "retry_parse_exceptions_and_revalidate_all_records",
            "parent_observation": {
                "run_id": manifest["run_id"],
                "manifest_sha256": parent_hashes["manifest.json"],
                "artifact_sha256": parent_hashes,
                "source_sha256": manifest["source_sha256"],
                "source_bundle_sha256": manifest["source_bundle_sha256"],
            },
            "recovery": {
                "target_urls": ordered_target_urls,
                "recovered_urls": [url for url in ordered_target_urls if url in recovered],
                "exhausted_urls": [url for url in ordered_target_urls if url in exhausted],
                "attempts_by_url": attempts,
                "policy": {
                    "max_attempts": max_attempts,
                    "initial_backoff_seconds": initial_backoff_seconds,
                    "backoff_multiplier": backoff_multiplier,
                    "max_backoff_seconds": max_backoff_seconds,
                    "pacing_seconds": pacing_seconds,
                },
            },
        })
        (temporary / "manifest.json").write_text(
            json.dumps(derived_manifest, indent=2, ensure_ascii=False, default=_json_default) + "\n",
            encoding="utf-8",
        )
        (temporary / "summary.md").write_text(
            f"# Recovered observation for {manifest['source']}\n\n"
            f"- Parent: `{manifest['run_id']}`\n- Run: `{run_id}`\n"
            f"- Retry targets: {len(targets)}\n- Recovered: {len(recovered)}\n"
            f"- Exhausted: {len(exhausted)}\n- Passed: {counts['passed']}\n"
            f"- Quarantined: {counts['quarantined']}\n- Skipped: {counts['skipped']}\n\n"
            "Every available article was revalidated with current field and page-evidence rules. "
            "No URL discovery or production database operation was performed.\n",
            encoding="utf-8",
        )
        # Recheck all lineage inputs immediately before publishing the immutable directory.
        if _sha256(source) != source_sha or adapter_bundle_sha256(source) != bundle_sha:
            raise ValueError("Adapter bundle changed while writing recovery artifacts")
        if _artifact_hashes(parent) != parent_hashes:
            raise ValueError("Parent observation changed while writing recovery artifacts")
        os.replace(temporary, final_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return final_dir
