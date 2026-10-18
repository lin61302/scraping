from __future__ import annotations

"""Relocate explicitly listed MongoDB rows after a verified date correction.

The manifest contains no query ranges. Every row is bound to its ObjectId,
source collection, URL, source domain, original document digest, old date, new
date, destination collection, and evidence. Dry run is the default. Standalone
commit writes a complete create-only backup, copies the original to quarantine,
inserts and verifies the corrected row, then removes the original.
"""

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId, json_util

from scraper_control.legacy_cleanup import document_sha256


def canonical(value) -> str:
    return json_util.dumps(
        value, json_options=json_util.CANONICAL_JSON_OPTIONS,
        sort_keys=True, separators=(",", ":"),
    )


def write_exclusive(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def load_manifest(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    payload = json_util.loads(raw.decode("utf-8"))
    if payload.get("schema_version") != 1 or payload.get("action") != "relocate_date":
        raise ValueError("manifest must be schema version 1 with action relocate_date")
    repair_id = payload.get("repair_id")
    rows = payload.get("rows")
    if not isinstance(repair_id, str) or len(repair_id) < 8 or not isinstance(rows, list) or not rows:
        raise ValueError("manifest requires repair_id and non-empty rows")
    identities = set()
    for index, row in enumerate(rows):
        required = {
            "collection", "_id", "url", "source_domain", "old_date_publish",
            "target_collection", "new_date_publish", "document_sha256", "evidence",
        }
        if set(row) != required:
            raise ValueError(f"rows[{index}] keys mismatch")
        if not ObjectId.is_valid(row["_id"]):
            raise ValueError(f"rows[{index}] has invalid ObjectId")
        identity = (row["collection"], str(row["_id"]))
        if identity in identities:
            raise ValueError(f"rows[{index}] duplicates an identity")
        identities.add(identity)
        if row["collection"] == row["target_collection"]:
            raise ValueError(f"rows[{index}] does not change collection")
        if not isinstance(row["old_date_publish"], datetime) or not isinstance(row["new_date_publish"], datetime):
            raise ValueError(f"rows[{index}] dates must be BSON datetimes")
        if not isinstance(row["evidence"], dict) or not row["evidence"]:
            raise ValueError(f"rows[{index}] requires evidence")
        if not isinstance(row["document_sha256"], str) or len(row["document_sha256"]) != 64:
            raise ValueError(f"rows[{index}] requires a document digest")
    return payload, hashlib.sha256(raw).hexdigest()


def corrected_document(document: dict, new_date: datetime) -> dict:
    output = dict(document)
    output["date_publish"] = new_date.replace(tzinfo=None)
    return output


def source_issues(row: dict, document: dict | None) -> list[str]:
    if document is None:
        return ["source_missing"]
    issues = []
    if document.get("url") != row["url"]:
        issues.append("url_mismatch")
    if document.get("source_domain") != row["source_domain"]:
        issues.append("source_domain_mismatch")
    old = row["old_date_publish"].replace(tzinfo=None)
    if document.get("date_publish") != old:
        issues.append("old_date_mismatch")
    if document_sha256(document) != row["document_sha256"]:
        issues.append("document_sha256_mismatch")
    return issues


def preflight(db, payload: dict) -> tuple[list[dict], list[dict]]:
    documents, mismatches = [], []
    for row in payload["rows"]:
        object_id = ObjectId(row["_id"])
        document = db[row["collection"]].find_one({"_id": object_id})
        issues = source_issues(row, document)
        target = db[row["target_collection"]].find_one({"_id": object_id})
        if target is not None:
            issues.append("target_object_id_already_exists")
        if issues:
            mismatches.append({"collection": row["collection"], "_id": str(object_id), "issues": issues})
        else:
            documents.append(document)
    return documents, mismatches


def run(db, manifest_path: Path, backup_path: Path, audit_path: Path, commit: bool) -> dict:
    payload, manifest_sha256 = load_manifest(manifest_path)
    repair_id = payload["repair_id"]
    quarantine_name = payload.get("quarantine_collection", "scraper_legacy_quarantine")
    audit = {
        "repair_id": repair_id, "manifest_sha256": manifest_sha256,
        "mode": "standalone_recoverable_commit" if commit else "dry_run",
        "requested_rows": len(payload["rows"]), "started_at": datetime.now(timezone.utc),
        "status": "validating", "mismatches": [],
    }
    if audit_path.exists():
        raise FileExistsError(audit_path)
    documents, mismatches = preflight(db, payload)
    if mismatches:
        audit.update(status="blocked_manifest_mismatch", mismatches=mismatches, matched_rows=len(documents), finished_at=datetime.now(timezone.utc))
        write_exclusive(audit_path, json_util.dumps(audit, indent=2) + "\n")
        return audit
    if not commit:
        audit.update(status="validated_dry_run", matched_rows=len(documents), finished_at=datetime.now(timezone.utc))
        write_exclusive(audit_path, json_util.dumps(audit, indent=2) + "\n")
        return audit

    backup_lines = []
    for row, document in zip(payload["rows"], documents):
        backup_lines.append(canonical({
            "repair_id": repair_id, "manifest_sha256": manifest_sha256,
            "collection": row["collection"], "target_collection": row["target_collection"],
            "document_sha256": row["document_sha256"], "document": document,
        }))
    backup_text = "\n".join(backup_lines) + "\n"
    write_exclusive(backup_path, backup_text)
    audit["backup_path"] = str(backup_path.resolve())
    audit["backup_sha256"] = hashlib.sha256(backup_text.encode()).hexdigest()

    completed = 0
    for row, document in zip(payload["rows"], documents):
        object_id = ObjectId(row["_id"])
        quarantine_query = {"repair_id": repair_id, "original_collection": row["collection"], "original_id": object_id}
        quarantine = db[quarantine_name].find_one(quarantine_query)
        if quarantine is None:
            db[quarantine_name].insert_one({
                **quarantine_query, "manifest_sha256": manifest_sha256,
                "source_domain": row["source_domain"], "original_url": row["url"],
                "reason": "verified_misparsed_publication_date", "evidence": row["evidence"],
                "document_sha256": row["document_sha256"], "quarantined_at": datetime.now(timezone.utc),
                "original_document": document,
            })
        repaired = corrected_document(document, row["new_date_publish"])
        expected_repaired_digest = document_sha256(repaired)
        target = db[row["target_collection"]].find_one({"_id": object_id})
        if target is None:
            db[row["target_collection"]].insert_one(repaired)
            target = db[row["target_collection"]].find_one({"_id": object_id})
        if target is None or document_sha256(target) != expected_repaired_digest:
            raise RuntimeError(f"target verification failed for {row['collection']}/{object_id}")
        live = db[row["collection"]].find_one({"_id": object_id})
        if source_issues(row, live):
            raise RuntimeError(f"source changed during repair for {row['collection']}/{object_id}")
        result = db[row["collection"]].delete_one({"_id": object_id})
        if result.deleted_count != 1 or db[row["collection"]].find_one({"_id": object_id}) is not None:
            raise RuntimeError(f"source deletion failed for {row['collection']}/{object_id}")
        completed += 1
    audit.update(status="committed", completed_rows=completed, finished_at=datetime.now(timezone.utc))
    write_exclusive(audit_path, json_util.dumps(audit, indent=2) + "\n")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework-root", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.framework_root).resolve()))
    from core.base_pipeline import MONGO_DB, MONGO_URI, MongoClient
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000)
    try:
        audit = run(client[MONGO_DB], args.manifest, args.backup, args.audit, args.commit)
    finally:
        client.close()
    print(json_util.dumps(audit, indent=2))
    raise SystemExit(0 if audit["status"] in {"validated_dry_run", "committed"} else 2)


if __name__ == "__main__":
    main()
