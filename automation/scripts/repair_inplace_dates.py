from __future__ import annotations

"""Repair explicitly listed publication dates without moving MongoDB collections.

The manifest binds each row to an ObjectId, URL, domain, old BSON date, exact
original document digest, new publisher date, and evidence. Dry run is the
default. Commit first writes an immutable backup of every original document,
then archives and conditionally updates one row at a time. The audit is fsynced
at every phase so a partial run can be reconstructed from the backup and
quarantine collection. This tool only accepts corrections within one month.
"""

import argparse
import hashlib
import os
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId, json_util

from scraper_control.legacy_cleanup import document_sha256


ROW_KEYS = {
    "collection", "_id", "url", "source_domain", "old_date_publish",
    "new_date_publish", "document_sha256", "evidence",
}


def canonical(value):
    return json_util.dumps(
        value, json_options=json_util.CANONICAL_JSON_OPTIONS,
        sort_keys=True, separators=(",", ":"),
    )


def naive_utc(value):
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_exclusive(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def replace_audit(path, audit):
    """Durably replace only this run's audit, retaining its create-only origin."""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        write_exclusive(temporary, json_util.dumps(audit, indent=2) + "\n")
        os.replace(str(temporary), str(path))
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_manifest(path):
    raw = path.read_bytes()
    payload = json_util.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("action") != "repair_date_in_place":
        raise ValueError("manifest must have schema_version=1 and action=repair_date_in_place")
    if set(payload) - {"schema_version", "action", "repair_id", "rows", "quarantine_collection"}:
        raise ValueError("manifest contains unknown keys")
    repair_id, rows = payload.get("repair_id"), payload.get("rows")
    if not isinstance(repair_id, str) or len(repair_id) < 8 or not isinstance(rows, list) or not rows:
        raise ValueError("manifest requires repair_id and non-empty rows")
    quarantine = payload.get("quarantine_collection", "scraper_legacy_quarantine")
    if not isinstance(quarantine, str) or not quarantine:
        raise ValueError("invalid quarantine_collection")
    identities, urls = set(), set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            raise ValueError("rows[{}] keys mismatch".format(index))
        if not isinstance(row["collection"], str) or not row["collection"]:
            raise ValueError("rows[{}] requires a collection".format(index))
        if not isinstance(row["_id"], str) or not ObjectId.is_valid(row["_id"]):
            raise ValueError("rows[{}] requires an ObjectId string".format(index))
        if not isinstance(row["url"], str) or not row["url"] or not isinstance(row["source_domain"], str) or not row["source_domain"]:
            raise ValueError("rows[{}] requires URL and source domain".format(index))
        identity = (row["collection"], str(ObjectId(row["_id"])))
        url_identity = (row["collection"], row["url"])
        if identity in identities or url_identity in urls:
            raise ValueError("rows[{}] duplicates an identity or URL".format(index))
        identities.add(identity)
        urls.add(url_identity)
        old, new = row["old_date_publish"], row["new_date_publish"]
        if not isinstance(old, datetime) or not isinstance(new, datetime):
            raise ValueError("rows[{}] dates must be BSON datetimes".format(index))
        old, new = naive_utc(old), naive_utc(new)
        if old == new or (old.year, old.month) != (new.year, new.month):
            raise ValueError("rows[{}] requires a changed date within one month".format(index))
        if not isinstance(row["document_sha256"], str) or len(row["document_sha256"]) != 64:
            raise ValueError("rows[{}] requires document_sha256".format(index))
        if not isinstance(row["evidence"], dict) or not row["evidence"]:
            raise ValueError("rows[{}] requires evidence".format(index))
    return payload, hashlib.sha256(raw).hexdigest()


def source_issues(row, document):
    if document is None:
        return ["source_missing"]
    issues = []
    if document.get("url") != row["url"]:
        issues.append("url_mismatch")
    if document.get("source_domain") != row["source_domain"]:
        issues.append("source_domain_mismatch")
    if document.get("date_publish") != naive_utc(row["old_date_publish"]):
        issues.append("old_date_mismatch")
    if document_sha256(document) != row["document_sha256"]:
        issues.append("document_sha256_mismatch")
    return issues


def preflight(db, rows, quarantine_name, repair_id):
    documents, mismatches = [], []
    for row in rows:
        object_id = ObjectId(row["_id"])
        document = db[row["collection"]].find_one({"_id": object_id})
        issues = source_issues(row, document)
        prior = db[quarantine_name].find_one({
            "repair_id": repair_id, "original_collection": row["collection"],
            "original_id": object_id,
        })
        if prior is not None:
            issues.append("quarantine_identity_already_exists")
        if issues:
            mismatches.append({"collection": row["collection"], "_id": row["_id"], "issues": issues})
        else:
            documents.append(document)
    return documents, mismatches


def run(db, manifest_path, backup_path, audit_path, commit=False):
    manifest_path, backup_path, audit_path = Path(manifest_path), Path(backup_path), Path(audit_path)
    if len({str(path.resolve()) for path in (manifest_path, backup_path, audit_path)}) != 3:
        raise ValueError("manifest, backup, and audit paths must differ")
    if audit_path.exists() or (commit and backup_path.exists()):
        raise FileExistsError("audit or backup already exists")
    payload, manifest_sha256 = load_manifest(manifest_path)
    rows, repair_id = payload["rows"], payload["repair_id"]
    quarantine_name = payload.get("quarantine_collection", "scraper_legacy_quarantine")
    documents, mismatches = preflight(db, rows, quarantine_name, repair_id)
    audit = {
        "repair_id": repair_id, "manifest_sha256": manifest_sha256,
        "mode": "standalone_recoverable_commit" if commit else "dry_run",
        "requested_rows": len(rows), "matched_rows": len(documents),
        "started_at": datetime.now(timezone.utc),
        "status": "blocked_manifest_mismatch" if mismatches else "validated_preflight",
        "mismatches": mismatches,
        "rows": [{"collection": row["collection"], "_id": row["_id"], "url": row["url"], "phase": "pending"} for row in rows],
    }
    write_exclusive(audit_path, json_util.dumps(audit, indent=2) + "\n")
    if mismatches:
        audit["finished_at"] = datetime.now(timezone.utc)
        replace_audit(audit_path, audit)
        return audit
    if not commit:
        audit["status"] = "validated_dry_run"
        audit["finished_at"] = datetime.now(timezone.utc)
        replace_audit(audit_path, audit)
        return audit

    try:
        backup_lines = [canonical({
            "repair_id": repair_id, "manifest_sha256": manifest_sha256,
            "collection": row["collection"], "document_sha256": row["document_sha256"],
            "document": document,
        }) for row, document in zip(rows, documents)]
        backup_text = "\n".join(backup_lines) + "\n"
        write_exclusive(backup_path, backup_text)
        audit["backup_path"] = str(backup_path.resolve())
        audit["backup_sha256"] = hashlib.sha256(backup_text.encode("utf-8")).hexdigest()
        audit["status"] = "backup_written"
        replace_audit(audit_path, audit)

        for index, (row, document) in enumerate(zip(rows, documents)):
            object_id = ObjectId(row["_id"])
            collection = db[row["collection"]]
            live = collection.find_one({"_id": object_id})
            issues = source_issues(row, live)
            if issues:
                raise RuntimeError("row {} changed before update: {}".format(index, ",".join(issues)))
            quarantine_query = {
                "repair_id": repair_id, "original_collection": row["collection"],
                "original_id": object_id,
            }
            db[quarantine_name].insert_one({
                **quarantine_query, "manifest_sha256": manifest_sha256,
                "source_domain": row["source_domain"], "original_url": row["url"],
                "reason": "verified_misparsed_publication_date",
                "evidence": row["evidence"], "document_sha256": row["document_sha256"],
                "quarantined_at": datetime.now(timezone.utc),
                "original_document": deepcopy(document),
            })
            archived = db[quarantine_name].find_one(quarantine_query)
            if archived is None or document_sha256(archived.get("original_document", {})) != row["document_sha256"]:
                raise RuntimeError("row {} quarantine verification failed".format(index))
            audit["rows"][index]["phase"] = "quarantine_verified"
            replace_audit(audit_path, audit)

            audit["rows"][index]["phase"] = "updating"
            replace_audit(audit_path, audit)
            new_date = naive_utc(row["new_date_publish"])
            result = collection.update_one({
                "_id": object_id, "url": row["url"],
                "source_domain": row["source_domain"],
                "date_publish": naive_utc(row["old_date_publish"]),
            }, {"$set": {"date_publish": new_date}})
            if result.matched_count != 1 or result.modified_count != 1:
                raise RuntimeError("row {} conditional update did not modify exactly one document".format(index))
            expected = dict(document)
            expected["date_publish"] = new_date
            updated = collection.find_one({"_id": object_id})
            if updated is None or document_sha256(updated) != document_sha256(expected):
                raise RuntimeError("row {} post-update document digest mismatch".format(index))
            audit["rows"][index]["phase"] = "verified"
            audit["rows"][index]["new_date_publish"] = new_date
            audit["rows"][index]["new_document_sha256"] = document_sha256(expected)
            replace_audit(audit_path, audit)

        audit["status"] = "committed"
        audit["completed_rows"] = len(rows)
        audit["finished_at"] = datetime.now(timezone.utc)
        replace_audit(audit_path, audit)
        return audit
    except Exception as exc:
        audit["status"] = "partial_failure"
        audit["error"] = "{}: {}".format(type(exc).__name__, exc)
        audit["completed_rows"] = sum(item["phase"] == "verified" for item in audit["rows"])
        audit["finished_at"] = datetime.now(timezone.utc)
        replace_audit(audit_path, audit)
        return audit


def main():
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
