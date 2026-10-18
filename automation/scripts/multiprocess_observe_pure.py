#!/usr/bin/env python3
"""Run a large pure-adapter observation across isolated worker processes.

Each process invokes ``parallel_observe_pure.py`` on one contiguous URL shard,
then this supervisor verifies and merges the immutable shard artifacts into the
normal promotion contract. Processes provide real CPU parallelism for HTML
parsing while each shard streams output and closes its network sessions.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"multiprocess_observed_{path.stem}", str(path))
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load source module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, indent=2, default=_json_default) + "\n", encoding="utf-8")


def observe_multiprocess(
    automation_root: Path,
    source_file: Path,
    start: date,
    end: date,
    output_root: Path,
    processes: int = 8,
    threads_per_process: int = 2,
) -> Path:
    if processes < 1 or processes > 24:
        raise ValueError("processes must be between 1 and 24")
    if threads_per_process < 1 or threads_per_process > 8:
        raise ValueError("threads_per_process must be between 1 and 8")
    automation_root = automation_root.resolve()
    source_file = source_file.resolve()
    sys.path.insert(0, str(automation_root))
    from scraper_control.observe import adapter_bundle_sha256

    module = _load_module(source_file)
    if not callable(getattr(module, "discover_urls", None)) or not callable(getattr(module, "scrape_url", None)):
        raise ValueError("multiprocess observer requires a pure-network adapter")
    config = module.Config()
    run_id = f"observe-multiprocess-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
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
        "observer_execution": {
            "kind": "verified_multiprocess_shards",
            "processes": processes,
            "threads_per_process": threads_per_process,
        },
        "limitations": [
            "Discovery evidence records the requested sitemap/listing coverage but does not prove the publisher exposed every article.",
            "Safe-through requires anomaly and postprocessing gates after this observation.",
        ],
    }
    manifest_path = run_dir / "manifest.json"
    _write_manifest(manifest_path, manifest)
    counts = {"discovered": 0, "selected": 0, "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}
    try:
        discovery_evidence: dict = {}
        urls = list(dict.fromkeys(module.discover_urls(start, end, discovery_evidence)))
        counts["discovered"] = counts["selected"] = len(urls)
        manifest["discovery_evidence"] = discovery_evidence
        manifest["discovery_method"] = "pure_adapter_multiprocess"
        manifest["pipeline_class"] = None
        with (run_dir / "urls.jsonl").open("w", encoding="utf-8") as stream:
            for url in urls:
                stream.write(json.dumps({"url": url, "discovery_method": "pure_adapter_multiprocess"}) + "\n")

        if not urls:
            shard_runs: list[Path] = []
        else:
            active_processes = min(processes, len(urls))
            chunk_size = math.ceil(len(urls) / active_processes)
            chunks = [urls[index:index + chunk_size] for index in range(0, len(urls), chunk_size)]
            shards_root = run_dir / "shards"
            shards_root.mkdir()
            runner = automation_root / "scripts" / "parallel_observe_pure.py"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(automation_root) + os.pathsep + environment.get("PYTHONPATH", "")

            def run_shard(item: tuple[int, list[str]]) -> Path:
                index, chunk = item
                urls_file = shards_root / f"urls_{index:03d}.txt"
                urls_file.write_text("\n".join(chunk) + "\n", encoding="utf-8")
                output = shards_root / f"output_{index:03d}"
                log = shards_root / f"shard_{index:03d}.log"
                command = [
                    sys.executable, str(runner), "--automation-root", str(automation_root),
                    "--source-file", str(source_file), "--start", start.isoformat(),
                    "--end", end.isoformat(), "--output-root", str(output),
                    "--workers", str(threads_per_process), "--urls-file", str(urls_file),
                ]
                result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
                log.write_text(result.stdout, encoding="utf-8")
                if result.returncode:
                    raise RuntimeError(f"shard {index} failed with status {result.returncode}; see {log}")
                candidates = sorted(output.glob("observe-*"))
                if len(candidates) != 1:
                    raise RuntimeError(f"shard {index} produced {len(candidates)} run directories")
                return candidates[0]

            with ThreadPoolExecutor(max_workers=len(chunks), thread_name_prefix="observation-shard") as pool:
                shard_runs = list(pool.map(run_shard, enumerate(chunks)))

            for name in ("articles.jsonl", "field_checks.jsonl", "issues.jsonl", "errors.jsonl"):
                with (run_dir / name).open("wb") as destination:
                    for shard in shard_runs:
                        source = shard / name
                        if source.exists():
                            with source.open("rb") as stream:
                                shutil.copyfileobj(stream, destination)

            merged_selected = 0
            for shard in shard_runs:
                shard_manifest = json.loads((shard / "manifest.json").read_text(encoding="utf-8"))
                if shard_manifest.get("status") != "observed":
                    raise RuntimeError(f"incomplete shard: {shard}")
                if shard_manifest.get("source_sha256") != manifest["source_sha256"]:
                    raise RuntimeError(f"adapter hash mismatch in shard: {shard}")
                shard_counts = shard_manifest["counts"]
                if shard_counts["selected"] != shard_counts["discovered"]:
                    raise RuntimeError(f"sampled shard cannot merge: {shard}")
                merged_selected += shard_counts["selected"]
                for key in ("passed", "quarantined", "skipped", "exceptions"):
                    counts[key] += shard_counts.get(key, 0)
            if merged_selected != len(urls):
                raise RuntimeError(f"merged shard URL count {merged_selected} != discovery {len(urls)}")

        manifest["status"] = "observed"
        manifest["shard_runs"] = [str(path) for path in shard_runs]
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["fatal_error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        finished = datetime.now(timezone.utc)
        manifest["finished_at"] = finished
        manifest["runtime_seconds"] = (finished - started).total_seconds()
        manifest["counts"] = counts
        _write_manifest(manifest_path, manifest)
        (run_dir / "summary.md").write_text(
            f"# Multiprocess observation for {config.source_name}\n\n"
            f"- Run: `{run_id}`\n- Status: {manifest['status']}\n"
            f"- Window: {start} through {end}\n"
            f"- Processes / threads each: {processes} / {threads_per_process}\n"
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
    parser.add_argument("--processes", type=int, default=8)
    parser.add_argument("--threads-per-process", type=int, default=2)
    args = parser.parse_args()
    print(observe_multiprocess(
        args.automation_root, args.source_file, args.start, args.end, args.output_root,
        args.processes, args.threads_per_process,
    ))


if __name__ == "__main__":
    main()
