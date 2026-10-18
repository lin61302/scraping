#!/usr/bin/env python3
"""Manifest-gated, resumable promotion of one verified observation run.

The observation framework's normal promoter is intentionally reused for its
strict evidence revalidation.  This wrapper adds an immutable prestate backup,
pins all observation inputs, serializes writes behind the production lock, and
performs exact document postchecks.  A commit can be resumed: a previously
inserted row is accepted only when its immutable scraped fields match the
reviewed manifest exactly.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from bson import json_util
from bson.json_util import CANONICAL_JSON_OPTIONS
from pymongo.errors import DuplicateKeyError


SCHEMA_VERSION = 1
PINNED_FILES = ("manifest.json", "urls.jsonl", "articles.jsonl", "field_checks.jsonl")
OPTIONAL_PINNED_FILES = ("issues.jsonl", "derivation.json")
EXACT_FIELDS = ("url", "source_domain", "title", "maintext", "date_publish", "download_via")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    """Return the SHA-256 digest for an already materialized payload."""
    return hashlib.sha256(payload).hexdigest()


def canonical(value: Any) -> str:
    return json_util.dumps(value, json_options=CANONICAL_JSON_OPTIONS,
                           sort_keys=True, separators=(",", ":"))


def manifest_hash(path: Path) -> str:
    sidecar = path.with_name(path.name + ".sha256")
    expected = sidecar.read_text().split()[0]
    actual = sha256_file(path)
    if expected != actual:
        raise RuntimeError(f"manifest hash mismatch: {expected} != {actual}")
    return actual


def validate_derivation_inputs(run_dir: Path) -> int:
    """Recheck every immutable parent named by a derived observation."""
    path = run_dir / "derivation.json"
    if not path.exists():
        return 0
    payload = json.loads(path.read_text())
    inputs = payload.get("pinned_inputs")
    if not isinstance(inputs, list) or not inputs:
        raise RuntimeError("derived observation lacks pinned parent inputs")
    seen: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) < {"path", "sha256"}:
            raise RuntimeError("invalid derived-observation parent entry")
        parent = Path(item["path"])
        expected = str(item["sha256"])
        if str(parent) in seen or len(expected) != 64:
            raise RuntimeError("duplicate or malformed derived-observation parent entry")
        seen.add(str(parent))
        if not parent.is_file() or sha256_file(parent) != expected:
            raise RuntimeError(f"derived-observation parent changed: {parent}")
    return len(inputs)


def load_runtime(framework_root: Path):
    root = framework_root.resolve()
    sys.path.insert(0, str(root / "scraping_automation"))
    code_root = root / "getnewurls_new" if (root / "getnewurls_new/core/base_pipeline.py").exists() else root
    sys.path.insert(0, str(code_root))
    from scraper_control.promote import _existing_urls, _load_candidates
    from scraper_control.observe import adapter_bundle_sha256
    from core.base_pipeline import MONGO_DB, MONGO_URI, MongoClient
    db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000)[MONGO_DB]
    return db, _load_candidates, _existing_urls, adapter_bundle_sha256


def target_payload(article: dict[str, Any], source: str, country: str,
                   is_env: bool) -> tuple[str, dict[str, Any]]:
    parsed = article["_parsed_date"]
    prefix = "opinion-articles" if str(article.get("collection", "")).startswith("opinion-articles") else "articles"
    collection = f"{prefix}-{parsed.year}-{parsed.month}"
    payload: dict[str, Any] = {
        "url": article["url"],
        "source_domain": source,
        "title": article["title"],
        "maintext": article["maintext"],
        "date_publish": parsed,
        # The exact timestamp is supplied during commit and is excluded from
        # immutable equality; download_via identifies the reviewed parser.
        "download_via": f"validated:{article.get('parser', 'unknown')}",
    }
    if is_env:
        payload["environmental"] = True
        payload["primary_location"] = f"ENV_{country}"
    return collection, payload


def matching_article_docs(db, source: str, url: str, year: int) -> list[tuple[str, dict[str, Any]]]:
    out = []
    query = {"source_domain": source, "url": url}
    for month in range(1, 13):
        for prefix in ("articles", "opinion-articles"):
            name = f"{prefix}-{year}-{month}"
            for row in db[name].find(query):
                out.append((name, row))
    return out


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def load_guarded_candidates(run_dir: Path, observation: dict[str, Any], load_candidates,
                            through_date: date | None, allow_held: bool
                            ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Revalidate an observation and account for every held URL exactly.

    The core loader classifies verified out-of-window quarantines as
    ``window_filtered`` rather than ``rejected``. It also intentionally refuses
    observations with source exceptions because those URLs have no article or
    field-check row. In partial mode, derive a temporary validation view that
    removes only the exact exception URLs, then require the immutable field
    checks, boundary classifications, and exception issues to reconcile to the
    manifest's quarantine/exception counts. No held URL becomes eligible.
    """
    if not allow_held:
        eligible, accounting = load_candidates(run_dir, observation, through_date)
        if accounting["rejected"]:
            raise RuntimeError(
                f"promotion revalidation rejected {len(accounting['rejected'])} URLs"
            )
        return eligible, accounting, []

    selected = read_jsonl(run_dir / "urls.jsonl")
    articles = read_jsonl(run_dir / "articles.jsonl")
    checks = read_jsonl(run_dir / "field_checks.jsonl")
    issues = read_jsonl(run_dir / "issues.jsonl")
    selected_urls = [row.get("url") for row in selected]
    if any(not url for url in selected_urls) or len(selected_urls) != len(set(selected_urls)):
        raise RuntimeError("selected URLs are missing or duplicated")
    article_urls = {row.get("url") for row in articles}
    check_urls = {row.get("url") for row in checks}
    exception_urls = {
        row.get("url") for row in issues
        if row.get("code") == "source_exception" and row.get("status") != "resolved"
    } - {None}
    expected_exception_count = int(observation["counts"].get("exceptions", 0))
    if len(exception_urls) != expected_exception_count:
        raise RuntimeError(
            f"source-exception evidence mismatch: manifest={expected_exception_count}, "
            f"issues={len(exception_urls)}"
        )
    selected_set = set(selected_urls)
    if exception_urls - selected_set:
        raise RuntimeError("source-exception issue is outside the selected URL set")
    if (selected_set - article_urls) != exception_urls or (selected_set - check_urls) != exception_urls:
        raise RuntimeError("only exact source-exception URLs may lack article/field-check rows")

    validation_manifest = copy.deepcopy(observation)
    validation_manifest["counts"]["exceptions"] = 0
    if exception_urls:
        validation_manifest["counts"]["selected"] -= len(exception_urls)
        validation_manifest["counts"]["discovered"] -= len(exception_urls)
        with tempfile.TemporaryDirectory(prefix="guarded-held-") as temporary:
            view = Path(temporary)
            write_jsonl(view / "urls.jsonl", [row for row in selected if row["url"] not in exception_urls])
            write_jsonl(view / "articles.jsonl", articles)
            write_jsonl(view / "field_checks.jsonl", checks)
            eligible, accounting = load_candidates(view, validation_manifest, through_date)
    else:
        eligible, accounting = load_candidates(run_dir, validation_manifest, through_date)

    accounting["selected"] = len(selected_urls)
    accounting["rejected"] = sorted(set(accounting["rejected"]) | exception_urls)
    recorded_holds = {
        row["url"] for row in checks
        if not row.get("eligible") and not row.get("excluded")
    } | exception_urls
    classified_holds = set(accounting["rejected"])
    classified_holds.update(row["url"] for row in accounting["window_filtered"])
    classified_holds.update(
        row["url"] for row in accounting["deferred_after_cutoff"]
        if not row.get("recorded_eligible")
    )
    eligible_urls = {row["url"] for row in eligible}
    if recorded_holds & eligible_urls:
        raise RuntimeError("an originally held URL became eligible during revalidation")
    if recorded_holds != classified_holds:
        missing = sorted(recorded_holds - classified_holds)
        extra = sorted(classified_holds - recorded_holds)
        raise RuntimeError(
            f"held URL classification mismatch: missing={missing[:5]}, extra={extra[:5]}"
        )
    expected_held = (
        int(observation["counts"].get("quarantined", 0))
        + expected_exception_count
    )
    if len(recorded_holds) != expected_held:
        raise RuntimeError(
            f"held-row accounting mismatch: expected {expected_held}, got {len(recorded_holds)}"
        )
    return eligible, accounting, sorted(recorded_holds)


def build_plan(run_dir: Path, framework_root: Path, artifact_dir: Path,
               through_date: date | None, allow_held: bool = False) -> Path:
    if artifact_dir.exists() and any(artifact_dir.iterdir()):
        raise RuntimeError(f"artifact directory is not empty: {artifact_dir}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    db, load_candidates, existing_urls, bundle_sha = load_runtime(framework_root)
    observation = json.loads((run_dir / "manifest.json").read_text())
    if observation.get("status") != "observed":
        raise RuntimeError("observation is not complete")
    derivation_input_count = validate_derivation_inputs(run_dir)
    source_file = Path(observation["source_file"])
    if sha256_file(source_file) != observation.get("source_sha256"):
        raise RuntimeError("adapter changed after observation; rerun observation")
    if observation.get("source_bundle_sha256") and bundle_sha(source_file) != observation["source_bundle_sha256"]:
        raise RuntimeError("adapter dependency bundle changed after observation; rerun observation")
    eligible, accounting, held_urls = load_guarded_candidates(
        run_dir, observation, load_candidates, through_date, allow_held
    )
    existing = existing_urls(db, eligible, observation["source"])
    new_articles = [row for row in eligible if row["url"] not in existing]
    operations = []
    for article in sorted(new_articles, key=lambda row: row["url"]):
        collection, payload = target_payload(
            article, observation["source"], observation["country"], bool(observation.get("is_env"))
        )
        operations.append({
            "collection": collection,
            "url": article["url"],
            "payload": json.loads(canonical(payload)),
            "payload_sha256": hashlib.sha256(canonical(payload).encode()).hexdigest(),
        })

    backup_path = artifact_dir / "precommit_prestate.jsonl"
    with backup_path.open("xb") as f:
        for op in operations:
            dt = json_util.loads(json.dumps(op["payload"]))["date_publish"]
            prior = matching_article_docs(db, observation["source"], op["url"], dt.year)
            envelope = {
                "url": op["url"],
                "article_documents": [
                    {"collection": name, "document": doc} for name, doc in prior
                ],
                # The central URLs collection is intentionally not read here:
                # it has no usable URL index and contains tens of millions of
                # rows.  The established promoter performs an append and
                # treats a unique-key collision as already indexed.
                "url_index_prestate": "not_read_unindexed_append_only_collection",
            }
            f.write(canonical(envelope).encode() + b"\n")
        f.write(canonical({
            "source_meta": list(db["source_meta"].find({"source_domain": observation["source"]}))
        }).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(backup_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    pinned = {name: sha256_file(run_dir / name) for name in PINNED_FILES}
    pinned.update({
        name: sha256_file(run_dir / name)
        for name in OPTIONAL_PINNED_FILES if (run_dir / name).exists()
    })
    pinned["source_file"] = sha256_file(source_file)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry_run",
        "generated_at": now_iso(),
        "run_dir": str(run_dir.resolve()),
        "framework_root": str(framework_root.resolve()),
        "source": observation["source"],
        "country": observation["country"],
        "requested_start": observation["requested_start"],
        "through_date": through_date.isoformat() if through_date else observation["requested_end"],
        "allow_held": allow_held,
        "derivation_input_count": derivation_input_count,
        "observation_hashes": pinned,
        "accounting": {
            "selected": accounting["selected"],
            "eligible": len(eligible),
            "already_present": len(existing),
            "new": len(operations),
            "held": len(held_urls),
            "held_urls_sha256": sha256_bytes(("\n".join(held_urls) + "\n").encode()),
            "rejected": len(accounting["rejected"]),
            "excluded": len(accounting["excluded"]),
            "window_filtered": len(accounting["window_filtered"]),
            "deferred_after_cutoff": len(accounting["deferred_after_cutoff"]),
        },
        "backup": {
            "path": backup_path.name,
            "sha256": sha256_file(backup_path),
            "line_count": len(operations) + 1,
            "format": "MongoDB canonical Extended JSON",
        },
        "operations": operations,
        "held_urls": held_urls,
    }
    path = artifact_dir / "manifest.json"
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    path.with_name(path.name + ".sha256").write_text(f"{digest}  manifest.json\n")
    print(json.dumps({"manifest": str(path), "sha256": digest,
                      "source": manifest["source"], **manifest["accounting"]}, sort_keys=True))
    return path


def decoded_payload(op: dict[str, Any]) -> dict[str, Any]:
    return json_util.loads(json.dumps(op["payload"]))


def exact_match(doc: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(doc.get(key) == value for key, value in expected.items())


def commit(plan_path: Path) -> None:
    digest = manifest_hash(plan_path)
    plan = json.loads(plan_path.read_text())
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("mode") != "dry_run":
        raise RuntimeError("unsupported manifest")
    run_dir = Path(plan["run_dir"])
    framework_root = Path(plan["framework_root"])
    for name, expected in plan["observation_hashes"].items():
        path = Path(json.loads((run_dir / "manifest.json").read_text())["source_file"]) if name == "source_file" else run_dir / name
        if sha256_file(path) != expected:
            raise RuntimeError(f"pinned observation input changed: {name}")
    if validate_derivation_inputs(run_dir) != int(plan.get("derivation_input_count", 0)):
        raise RuntimeError("derived-observation parent count changed")
    backup = plan_path.parent / plan["backup"]["path"]
    if sha256_file(backup) != plan["backup"]["sha256"]:
        raise RuntimeError("prestate backup hash mismatch")
    if os.stat(backup).st_mode & stat.S_IWUSR:
        raise RuntimeError("prestate backup must be read-only")

    db, load_candidates, existing_urls, bundle_sha = load_runtime(framework_root)
    observation = json.loads((run_dir / "manifest.json").read_text())
    source_file = Path(observation["source_file"])
    if observation.get("source_bundle_sha256") and bundle_sha(source_file) != observation["source_bundle_sha256"]:
        raise RuntimeError("adapter dependency bundle changed after dry run")
    cutoff = date.fromisoformat(plan["through_date"])
    eligible, accounting, held_urls = load_guarded_candidates(
        run_dir, observation, load_candidates, cutoff, bool(plan.get("allow_held"))
    )
    expected_new_urls = {op["url"] for op in plan["operations"]}
    eligible_urls = {row["url"] for row in eligible}
    if held_urls != plan.get("held_urls", []) or not expected_new_urls <= eligible_urls:
        raise RuntimeError("live observation revalidation differs from manifest")

    lock_path = framework_root / "scraping_automation/var/locks/production-observation-promote.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path = plan_path.parent / "commit_journal.jsonl"
    receipt_path = plan_path.parent / "commit_receipt.json"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        inserted = already_applied = 0
        ids = []
        dates = []
        with journal_path.open("a", encoding="utf-8") as journal:
            for op in plan["operations"]:
                expected = decoded_payload(op)
                dt = expected["date_publish"]
                current = matching_article_docs(db, plan["source"], op["url"], dt.year)
                if current:
                    if len(current) != 1 or current[0][0] != op["collection"] or not exact_match(current[0][1], expected):
                        raise RuntimeError(f"unexpected existing production row: {op['url']}")
                    doc_id = current[0][1]["_id"]
                    already_applied += 1
                    state = "already_applied_exact"
                else:
                    payload = dict(expected)
                    payload["date_download"] = datetime.utcnow()
                    result = db[op["collection"]].insert_one(payload)
                    doc_id = result.inserted_id
                    inserted += 1
                    state = "inserted"
                url_index_state = "inserted"
                try:
                    db["urls"].insert_one({"url": op["url"]})
                except DuplicateKeyError:
                    url_index_state = "already_present_unique_key"
                ids.append({"collection": op["collection"], "id": str(doc_id), "url": op["url"]})
                dates.append(dt)
                journal.write(json.dumps({"at": now_iso(), "state": state,
                                          "url_index_state": url_index_state,
                                          **ids[-1]}, sort_keys=True) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
        if dates:
            db["source_meta"].update_one(
                {"source_domain": plan["source"]},
                {"$max": {"last_date_publish": max(dates)}, "$set": {"updated_at": datetime.utcnow()}},
                upsert=True,
            )

        checked = 0
        for op in plan["operations"]:
            expected = decoded_payload(op)
            dt = expected["date_publish"]
            current = matching_article_docs(db, plan["source"], op["url"], dt.year)
            if len(current) != 1 or current[0][0] != op["collection"] or not exact_match(current[0][1], expected):
                raise RuntimeError(f"exact postcheck failed: {op['url']}")
            checked += 1
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "mode": "committed",
            "promotion_id": f"guarded-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
            "manifest_sha256": digest,
            "source": plan["source"],
            "country": plan["country"],
            "through_date": plan["through_date"],
            "expected_new": len(plan["operations"]),
            "inserted": inserted,
            "already_applied_exact": already_applied,
            "postchecked": checked,
            "document_ids": ids,
            "completed_at": now_iso(),
        }
        payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
        receipt_path.write_bytes(payload)
        receipt_path.with_name(receipt_path.name + ".sha256").write_text(
            f"{hashlib.sha256(payload).hexdigest()}  commit_receipt.json\n"
        )
        print(json.dumps(receipt, sort_keys=True))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--framework-root", type=Path)
    p.add_argument("--run-dir", type=Path)
    p.add_argument("--artifact-dir", type=Path)
    p.add_argument("--through-date")
    p.add_argument("--allow-held", action="store_true")
    p.add_argument("--commit-manifest", type=Path)
    args = p.parse_args()
    if args.commit_manifest:
        commit(args.commit_manifest)
    else:
        if not (args.framework_root and args.run_dir and args.artifact_dir):
            p.error("dry-run requires --framework-root, --run-dir, and --artifact-dir")
        build_plan(args.run_dir.resolve(), args.framework_root.resolve(), args.artifact_dir.resolve(),
                   date.fromisoformat(args.through_date) if args.through_date else None,
                   args.allow_held)


if __name__ == "__main__":
    main()
