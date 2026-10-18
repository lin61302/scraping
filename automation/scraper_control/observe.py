from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import traceback
import uuid
import inspect
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

from .validation import resolve_policy_exclusion, resolve_with_page_evidence, validate_article
from .evidence import compare_required_evidence, extract_html_evidence


def adapter_bundle_sha256(source_file: Path) -> str:
    """Hash the adapter and its sibling shared helper when present."""
    files = [source_file.resolve()]
    common = source_file.resolve().parent / "common.py"
    if common.exists() and common != files[0]:
        files.append(common)
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _append_jsonl(path: Path, payload: Dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"observed_source_{path.stem}", str(path))
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load source module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observe_source(
    framework_root: Path,
    source_file: Path,
    start: date,
    end: date,
    output_root: Path,
    max_urls: Optional[int] = None,
    config_overrides: Optional[Dict[str, object]] = None,
    reverse_urls: bool = False,
) -> Path:
    """Observe a source without calling any production insert path.

    Pure adapters export discover_urls(start, end, evidence) and
    scrape_url(url). They are preferred because the observer never imports or
    constructs SourcePipeline, whose initializer performs database reads.
    """
    framework_root = framework_root.resolve()
    source_file = source_file.resolve()
    sys.path.insert(0, str(framework_root))
    module = _load_module(source_file)
    pure_adapter = callable(getattr(module, "discover_urls", None)) and callable(getattr(module, "scrape_url", None))
    base_pipeline = None
    SourcePipeline = None
    if not pure_adapter:
        import core.base_pipeline as base_pipeline
        SourcePipeline = base_pipeline.SourcePipeline

    config = module.Config()
    if not pure_adapter:
        config.force_date_range = True
        config.start_date = start.isoformat()
        config.end_date = end.isoformat()
        config.persist_learned_selectors = False
    for name, value in (config_overrides or {}).items():
        if name.startswith("_"):
            raise ValueError(f"Private configuration override is not allowed: {name}")
        setattr(config, name, value)
    run_id = f"observe-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc)

    manifest: Dict[str, object] = {
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
        "config_overrides": config_overrides or {},
        "reverse_urls": reverse_urls,
        "started_at": started,
        "status": "running",
        "adapter_contract": "pure_network_only" if pure_adapter else "source_pipeline_validation_mode",
        "limitations": [
            "Discovery evidence records the requested sitemap/listing coverage but does not prove the publisher exposed every article.",
            "Safe-through requires anomaly and postprocessing gates after this observation.",
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=_json_default) + "\n", encoding="utf-8")

    response_cache = {}
    original_safe_get = None
    pipeline = None
    if not pure_adapter:
        pipeline_class = SourcePipeline
        subclasses = [
            value for value in vars(module).values()
            if inspect.isclass(value) and value is not SourcePipeline and issubclass(value, SourcePipeline)
        ]
        if subclasses:
            pipeline_class = subclasses[-1]
        manifest["pipeline_class"] = pipeline_class.__name__
        original_safe_get = base_pipeline._safe_get

        def cached_safe_get(url, *args, **kwargs):
            if url in response_cache:
                return response_cache[url]
            response = original_safe_get(url, *args, **kwargs)
            if response is not None:
                response_cache[url] = response
            return response

        base_pipeline._safe_get = cached_safe_get
        pipeline = pipeline_class(config, custom_parser=getattr(module, "custom_parser", None))
        # This flag blocks _insert, DB duplicate filtering, and learned-selector writes.
        pipeline._validating = True
    else:
        manifest["pipeline_class"] = None
    counts = {"discovered": 0, "selected": 0, "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}
    access_circuit_threshold = int(getattr(config, "access_block_circuit_threshold", 0) or 0) if pure_adapter else 0
    if access_circuit_threshold < 0:
        raise ValueError("access_block_circuit_threshold must be nonnegative")
    consecutive_access_blocks: list[str] = []
    access_circuit: Optional[Dict[str, object]] = None
    try:
        discovery_evidence = {}
        if pure_adapter:
            urls = module.discover_urls(start, end, discovery_evidence)
            method = "pure_adapter"
            manifest["discovery_evidence"] = discovery_evidence
        else:
            urls, method = pipeline.discover_urls()
        urls = list(dict.fromkeys(urls))
        counts["discovered"] = len(urls)
        if reverse_urls:
            urls.reverse()
        if max_urls is not None:
            urls = urls[:max_urls]
        counts["selected"] = len(urls)
        manifest["discovery_method"] = method
        for url in urls:
            _append_jsonl(run_dir / "urls.jsonl", {"url": url, "discovery_method": method})

        for index, url in enumerate(urls, start=1):
            if access_circuit is not None:
                counts["exceptions"] += 1
                access_circuit["deferred_url_count"] = int(access_circuit["deferred_url_count"]) + 1
                _append_jsonl(
                    run_dir / "issues.jsonl",
                    {
                        "url": url, "source": config.source_name, "stage": "parse",
                        "category": "infrastructure", "code": "source_access_circuit_open",
                        "severity": "error", "status": "open",
                        "observed": {
                            "reason": "Publisher access blocked on consecutive sampled article URLs; this URL was not fetched",
                            "trigger_urls": access_circuit["trigger_urls"],
                            "threshold": access_circuit_threshold,
                        },
                        "attempt": 0,
                    },
                )
                continue
            try:
                if pure_adapter:
                    normalized = dict(module.scrape_url(url))
                    normalized.setdefault("url", url)
                    normalized.setdefault("source_domain", config.source_name)
                    normalized.setdefault("country_code", config.country_code)
                    normalized.setdefault("parser", "pure_adapter")
                    normalized.setdefault("collection", None)
                    normalized.setdefault("parser_error", None)
                else:
                    article = pipeline.scrape_url(url)
                    normalized = {
                        "url": article.url,
                        "source_domain": config.source_name,
                        "country_code": config.country_code,
                        "title": article.title,
                        "date_publish": article.date_publish,
                        "maintext": article.maintext,
                        "parser": article.parser,
                        "collection": article.collection,
                        "parser_error": article.error,
                    }
                _append_jsonl(run_dir / "articles.jsonl", normalized)
                is_excluded = normalized.get("collection") == "SKIP"
                check = validate_article(normalized, start, end, config.source_name)
                evidence = None
                evidence_checks = []
                response = response_cache.get(url)
                if response is not None:
                    evidence = extract_html_evidence(response.text)
                    evidence_checks = compare_required_evidence(normalized, evidence)
                elif normalized.get("field_evidence"):
                    evidence = normalized["field_evidence"]
                    evidence_checks = compare_required_evidence(normalized, evidence)
                all_checks = list(check.fields) + evidence_checks
                semantic_eligible, resolutions = resolve_with_page_evidence(check, evidence_checks, evidence)
                exclusion_reason = normalized.get("exclusion_reason")
                exclusion_evidence = normalized.get("exclusion_evidence")
                exclusion_valid, exclusion_resolution = resolve_policy_exclusion(
                    check, evidence_checks, evidence, exclusion_reason, exclusion_evidence,
                    article_url=normalized.get("url"),
                )
                excluded = bool(is_excluded and exclusion_valid and not normalized.get("parser_error"))
                eligible = bool(semantic_eligible and not is_excluded and not normalized.get("parser_error"))
                check_payload = {
                    "url": url, "eligible": eligible,
                    "fields": [asdict(item) for item in all_checks], "page_evidence": evidence,
                    "automatic_resolutions": resolutions,
                    "excluded": excluded,
                    "exclusion_reason": exclusion_reason,
                    "exclusion_evidence": exclusion_evidence,
                    "exclusion_resolution": exclusion_resolution,
                }
                _append_jsonl(run_dir / "field_checks.jsonl", check_payload)
                if excluded:
                    counts["skipped"] += 1
                    _append_jsonl(
                        run_dir / "issues.jsonl",
                        {
                            "url": url, "source": config.source_name, "stage": "source_policy",
                            "category": "collection", "code": exclusion_reason,
                            "severity": "info", "status": "excluded", "observed": exclusion_evidence,
                            "attempt": 1,
                        },
                    )
                elif eligible:
                    counts["passed"] += 1
                    for resolution in resolutions:
                        _append_jsonl(
                            run_dir / "issues.jsonl",
                            {
                                "url": url, "source": config.source_name, "stage": "validation",
                                "category": resolution["field"], "code": resolution["code"],
                                "severity": "info", "status": "resolved", "observed": resolution,
                                "attempt": 1,
                            },
                        )
                else:
                    counts["quarantined"] += 1
                    if is_excluded and not (exclusion_reason and exclusion_evidence):
                        _append_jsonl(
                            run_dir / "issues.jsonl",
                            {
                                "url": url, "source": config.source_name, "stage": "source_policy",
                                "category": "collection", "code": "exclusion_policy_evidence_missing",
                                "severity": "error", "status": "open",
                                "observed": {"reason": exclusion_reason, "evidence": exclusion_evidence},
                                "attempt": 1,
                            },
                        )
                    if normalized.get("parser_error"):
                        _append_jsonl(
                            run_dir / "issues.jsonl",
                            {
                                "url": url, "source": config.source_name, "stage": "parse",
                                "category": "content", "code": "adapter_parser_error",
                                "severity": "error", "status": "open",
                                "observed": {
                                    "parser_error": normalized["parser_error"],
                                    "field_sources": normalized.get("field_sources"),
                                    "page_evidence": evidence,
                                },
                                "attempt": 1,
                            },
                        )
                    for field in all_checks:
                        for code in field.codes:
                            _append_jsonl(
                                run_dir / "issues.jsonl",
                                {
                                    "url": url, "source": config.source_name, "stage": "validation",
                                    "category": field.field, "code": code, "severity": "error",
                                    "status": "open", "observed": field.evidence, "attempt": 1,
                                },
                            )
                consecutive_access_blocks.clear()
            except Exception as exc:
                counts["exceptions"] += 1
                if access_circuit_threshold and type(exc).__name__ == "AccessBlockedError":
                    consecutive_access_blocks.append(url)
                    if len(consecutive_access_blocks) >= access_circuit_threshold:
                        access_circuit = {
                            "reason": "consecutive_article_access_blocked",
                            "threshold": access_circuit_threshold,
                            "trigger_urls": list(consecutive_access_blocks),
                            "opened_after_url_index": index,
                            "deferred_url_count": 0,
                        }
                        manifest["access_circuit"] = access_circuit
                else:
                    consecutive_access_blocks.clear()
                _append_jsonl(
                    run_dir / "issues.jsonl",
                    {
                        "url": url, "source": config.source_name, "stage": "parse",
                        "category": "infrastructure", "code": "source_exception", "severity": "error",
                        "status": "open", "observed": {"type": type(exc).__name__, "message": str(exc)},
                        "attempt": 1,
                    },
                )

        manifest["status"] = "observed"
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["fatal_error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        raise
    finally:
        try:
            if base_pipeline is not None and original_safe_get is not None:
                base_pipeline._safe_get = original_safe_get
        except Exception:
            pass
        finished = datetime.now(timezone.utc)
        manifest["finished_at"] = finished
        manifest["runtime_seconds"] = (finished - started).total_seconds()
        manifest["counts"] = counts
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=_json_default) + "\n", encoding="utf-8")
        summary = [
            f"# Observation run for {config.source_name}", "",
            f"- Run: `{run_id}`", f"- Status: {manifest['status']}",
            f"- Window: {start.isoformat()} through {end.isoformat()}",
            f"- Discovery method: {manifest.get('discovery_method', 'unknown')}",
            f"- Discovered: {counts['discovered']}", f"- Selected for observation: {counts['selected']}",
            f"- Passed field and page-evidence checks: {counts['passed']}",
            f"- Quarantined: {counts['quarantined']}", f"- Skipped by source rules: {counts['skipped']}",
            f"- Exceptions: {counts['exceptions']}", "",
            "This run made no production inserts. Coverage and visible page evidence gates are still required before promotion.", "",
        ]
        (run_dir / "summary.md").write_text("\n".join(summary), encoding="utf-8")
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Observe one getnewurls_new source without production writes")
    parser.add_argument("--framework-root", required=True)
    parser.add_argument("--source-file", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--max-urls", type=int)
    parser.add_argument("--reverse-urls", action="store_true", help="Observe from the end of the discovered URL list")
    parser.add_argument(
        "--config-json", default="{}",
        help="JSON object of explicit source Config overrides, such as sitemap page ranges represented as lists",
    )
    args = parser.parse_args()
    run_dir = observe_source(
        Path(args.framework_root), Path(args.source_file), date.fromisoformat(args.start), date.fromisoformat(args.end),
        Path(args.output_root), args.max_urls, json.loads(args.config_json), args.reverse_urls,
    )
    print(run_dir)


if __name__ == "__main__":
    main()
