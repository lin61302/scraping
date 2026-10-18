from __future__ import annotations

"""Fail-closed cleanup of explicitly identified legacy MongoDB rows.

This module deliberately has no discovery logic.  An operator must supply the
exact collection, ObjectId, URL, source, document digest, reason, and review
evidence for every row.  A commit is transaction-only and writes a complete
Extended JSON backup before opening the transaction.
"""

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from bson import ObjectId, json_util


SCHEMA_VERSION = 1
_CHANGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,127}$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
_COLLECTION = re.compile(r"^(?!system\.)(?!.*\x00)[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()


class CleanupManifestError(ValueError):
    """The manifest is not sufficiently specific to authorize a cleanup."""


class CleanupCommitError(RuntimeError):
    """The requested commit could not be proven to have completed safely."""


@dataclass(frozen=True)
class CleanupEntry:
    collection: str
    object_id: ObjectId
    url: str | None
    source_domain: str
    reason: str
    evidence: Mapping[str, Any]
    document_sha256: str


@dataclass(frozen=True)
class CleanupManifest:
    cleanup_id: str
    action: str
    quarantine_collection: str | None
    entries: tuple[CleanupEntry, ...]
    sha256: str


def _canonical_json(value: Any) -> str:
    return json_util.dumps(
        value,
        json_options=json_util.CANONICAL_JSON_OPTIONS,
        sort_keys=True,
        separators=(",", ":"),
    )


def document_sha256(document: Mapping[str, Any]) -> str:
    """Return the stable digest used to bind a manifest to one live document."""

    return hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CleanupManifestError(f"{label} must be an object")
    return value


def _parse_object_id(value: Any, label: str) -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    raise CleanupManifestError(f"{label} must be one exact 24-character MongoDB ObjectId")


def load_manifest(path: Path) -> CleanupManifest:
    """Load and strictly validate an explicit cleanup manifest."""

    raw = path.read_bytes()
    try:
        payload = json_util.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise CleanupManifestError("manifest is not valid UTF-8 Extended JSON") from exc
    root = _require_mapping(payload, "manifest")
    allowed_root = {
        "schema_version", "cleanup_id", "action", "quarantine_collection",
        "created_at", "created_by", "change_ticket", "notes", "rows",
    }
    unknown = set(root) - allowed_root
    if unknown:
        raise CleanupManifestError(f"manifest has unknown keys: {sorted(unknown)}")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise CleanupManifestError(f"schema_version must be {SCHEMA_VERSION}")
    cleanup_id = root.get("cleanup_id")
    if not isinstance(cleanup_id, str) or not _CHANGE_ID.fullmatch(cleanup_id):
        raise CleanupManifestError("cleanup_id must be 8-128 safe identifier characters")
    action = root.get("action")
    if action not in {"delete", "quarantine"}:
        raise CleanupManifestError("action must be delete or quarantine")
    quarantine_collection = root.get("quarantine_collection")
    if action == "quarantine":
        if not isinstance(quarantine_collection, str) or not _COLLECTION.fullmatch(quarantine_collection):
            raise CleanupManifestError("quarantine requires a safe quarantine_collection")
    elif quarantine_collection is not None:
        raise CleanupManifestError("quarantine_collection is allowed only for quarantine action")
    rows = root.get("rows")
    if not isinstance(rows, list) or not rows:
        raise CleanupManifestError("rows must be a non-empty array")

    entries: list[CleanupEntry] = []
    identities: set[tuple[str, ObjectId]] = set()
    allowed_row = {
        "collection", "_id", "url", "source_domain", "reason", "evidence",
        "document_sha256",
    }
    for index, raw_entry in enumerate(rows):
        label = f"rows[{index}]"
        entry = _require_mapping(raw_entry, label)
        unknown = set(entry) - allowed_row
        missing = allowed_row - set(entry)
        if unknown or missing:
            raise CleanupManifestError(
                f"{label} keys mismatch; missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        collection = entry["collection"]
        if not isinstance(collection, str) or not _COLLECTION.fullmatch(collection):
            raise CleanupManifestError(f"{label}.collection is unsafe")
        if collection == quarantine_collection:
            raise CleanupManifestError(f"{label}.collection cannot be the quarantine collection")
        object_id = _parse_object_id(entry["_id"], f"{label}._id")
        identity = (collection, object_id)
        if identity in identities:
            raise CleanupManifestError(f"{label} duplicates collection and _id")
        identities.add(identity)
        url = entry["url"]
        if not (
            (isinstance(url, str) and url.startswith(("https://", "http://")))
            or (url is None and entry.get("reason") == "missing_url")
        ):
            raise CleanupManifestError(
                f"{label}.url must be one exact HTTP(S) URL, or null only for reason missing_url"
            )
        source = entry["source_domain"]
        if not isinstance(source, str) or not source.strip() or source != source.strip():
            raise CleanupManifestError(f"{label}.source_domain must be a non-empty exact value")
        reason = entry["reason"]
        if not isinstance(reason, str) or not _REASON.fullmatch(reason):
            raise CleanupManifestError(f"{label}.reason must be a lowercase machine code")
        evidence = _require_mapping(entry["evidence"], f"{label}.evidence")
        if not evidence:
            raise CleanupManifestError(f"{label}.evidence cannot be empty")
        expected_fields = evidence.get("expected_fields")
        if not isinstance(expected_fields, Mapping) or not expected_fields:
            raise CleanupManifestError(f"{label}.evidence.expected_fields must be a non-empty object")
        invalid_paths = [
            path for path in expected_fields
            if not isinstance(path, str)
            or not path
            or any(not component or component.startswith("$") for component in path.split("."))
        ]
        if invalid_paths:
            raise CleanupManifestError(f"{label}.evidence.expected_fields has unsafe field paths")
        forbidden = {
            path for path in expected_fields
            if path.split(".", 1)[0] in {"_id", "url", "source_domain"}
        }
        if forbidden:
            raise CleanupManifestError(
                f"{label}.evidence.expected_fields repeats identity fields: {sorted(forbidden)}"
            )
        digest = entry["document_sha256"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise CleanupManifestError(f"{label}.document_sha256 must be lowercase SHA-256")
        entries.append(CleanupEntry(
            collection=collection,
            object_id=object_id,
            url=url,
            source_domain=source,
            reason=reason,
            evidence=dict(evidence),
            document_sha256=digest,
        ))

    return CleanupManifest(
        cleanup_id=cleanup_id,
        action=action,
        quarantine_collection=quarantine_collection,
        entries=tuple(entries),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _get_path(document: Mapping[str, Any], dotted_path: str) -> Any:
    value: Any = document
    for component in dotted_path.split("."):
        if not isinstance(value, Mapping) or component not in value:
            return _MISSING
        value = value[component]
    return value


def _entry_mismatches(entry: CleanupEntry, document: Mapping[str, Any] | None) -> list[str]:
    if document is None:
        return ["row_not_found"]
    issues: list[str] = []
    if document.get("_id") != entry.object_id:
        issues.append("object_id_mismatch")
    if entry.url is None:
        if "url" not in document or document["url"] is not None:
            issues.append("url_mismatch")
    elif document.get("url", _MISSING) != entry.url:
        issues.append("url_mismatch")
    if document.get("source_domain", _MISSING) != entry.source_domain:
        issues.append("source_domain_mismatch")
    expected_fields = entry.evidence["expected_fields"]
    for path, expected in expected_fields.items():
        if not isinstance(path, str) or not path or path.startswith("$"):
            issues.append("invalid_expected_field_path")
            continue
        if _get_path(document, path) != expected:
            issues.append(f"expected_field_mismatch:{path}")
    if document_sha256(document) != entry.document_sha256:
        issues.append("document_sha256_mismatch")
    return issues


def _preflight(db, manifest: CleanupManifest, session=None) -> tuple[list[Mapping[str, Any]], list[dict]]:
    documents: list[Mapping[str, Any]] = []
    mismatches: list[dict] = []
    for entry in manifest.entries:
        kwargs = {"session": session} if session is not None else {}
        document = db[entry.collection].find_one({"_id": entry.object_id}, **kwargs)
        issues = _entry_mismatches(entry, document)
        if issues:
            mismatches.append({
                "collection": entry.collection,
                "_id": str(entry.object_id),
                "url": entry.url,
                "issues": issues,
            })
        elif document is not None:
            documents.append(document)
        if manifest.action == "quarantine":
            quarantine_query = {
                "cleanup_id": manifest.cleanup_id,
                "original_collection": entry.collection,
                "original_id": entry.object_id,
            }
            if db[manifest.quarantine_collection].count_documents(
                quarantine_query, limit=1, **kwargs
            ):
                mismatches.append({
                    "collection": entry.collection,
                    "_id": str(entry.object_id),
                    "url": entry.url,
                    "issues": ["quarantine_destination_already_exists"],
                })
    return documents, mismatches


def _write_exclusive(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_audit(path: Path, audit: Mapping[str, Any]) -> None:
    _write_exclusive(
        path,
        json_util.dumps(
            audit,
            json_options=json_util.RELAXED_JSON_OPTIONS,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
    )


def _backup_content(manifest: CleanupManifest, documents: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for entry, document in zip(manifest.entries, documents):
        payload = {
            "backup_schema_version": 1,
            "cleanup_id": manifest.cleanup_id,
            "action": manifest.action,
            "collection": entry.collection,
            "reason": entry.reason,
            "evidence": entry.evidence,
            "document_sha256": document_sha256(document),
            "document": document,
        }
        lines.append(_canonical_json(payload))
    return "\n".join(lines) + "\n"


def _guard_filter(entry: CleanupEntry) -> dict[str, Any]:
    query: dict[str, Any] = {
        "_id": entry.object_id,
        "source_domain": entry.source_domain,
    }
    query["url"] = {"$type": 10} if entry.url is None else entry.url
    # $eq makes even mapping-shaped BSON values literal rather than allowing a
    # manifest value to be interpreted as a MongoDB query operator.
    query.update({
        path: {"$eq": expected}
        for path, expected in entry.evidence["expected_fields"].items()
    })
    return query


def _apply_transaction(db, manifest: CleanupManifest) -> None:
    """Revalidate and apply all rows in one MongoDB transaction."""

    client = getattr(db, "client", None)
    if client is None or not hasattr(client, "start_session"):
        raise CleanupCommitError("database handle does not support required transactions")
    with client.start_session() as session:
        with session.start_transaction():
            _, mismatches = _preflight(db, manifest, session=session)
            if mismatches:
                raise CleanupCommitError("live rows changed after backup; transaction aborted")
            for entry in manifest.entries:
                if manifest.action == "quarantine":
                    source_document = db[entry.collection].find_one(
                        {"_id": entry.object_id}, session=session
                    )
                    quarantine_document = {
                        "cleanup_id": manifest.cleanup_id,
                        "manifest_sha256": manifest.sha256,
                        "original_collection": entry.collection,
                        "original_id": entry.object_id,
                        "original_url": entry.url,
                        "source_domain": entry.source_domain,
                        "reason": entry.reason,
                        "evidence": entry.evidence,
                        "document_sha256": entry.document_sha256,
                        "quarantined_at": datetime.now(timezone.utc),
                        "original_document": source_document,
                    }
                    db[manifest.quarantine_collection].insert_one(
                        quarantine_document, session=session
                    )
                result = db[entry.collection].delete_one(
                    _guard_filter(entry), session=session
                )
                if result.deleted_count != 1:
                    raise CleanupCommitError("guarded deletion did not match exactly one row")


def _quarantine_query(manifest: CleanupManifest, entry: CleanupEntry) -> dict[str, Any]:
    return {
        "cleanup_id": manifest.cleanup_id,
        "original_collection": entry.collection,
        "original_id": entry.object_id,
    }


def _validate_quarantine_copy(
    manifest: CleanupManifest,
    entry: CleanupEntry,
    document: Mapping[str, Any] | None,
) -> list[str]:
    if document is None:
        return ["quarantine_copy_missing"]
    issues = []
    if document.get("manifest_sha256") != manifest.sha256:
        issues.append("quarantine_manifest_sha256_mismatch")
    if document.get("document_sha256") != entry.document_sha256:
        issues.append("quarantine_document_sha256_field_mismatch")
    original = document.get("original_document")
    if not isinstance(original, Mapping) or document_sha256(original) != entry.document_sha256:
        issues.append("quarantine_original_document_mismatch")
    return issues


def _read_backup_documents(path: Path, manifest: CleanupManifest) -> list[Mapping[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                records.append(json_util.loads(line))
    if len(records) != len(manifest.entries):
        raise CleanupCommitError("existing backup row count does not match manifest")
    documents = []
    for entry, record in zip(manifest.entries, records):
        document = record.get("document") if isinstance(record, Mapping) else None
        if not isinstance(document, Mapping):
            raise CleanupCommitError("existing backup has no complete source document")
        if record.get("cleanup_id") != manifest.cleanup_id:
            raise CleanupCommitError("existing backup cleanup_id mismatch")
        if record.get("collection") != entry.collection:
            raise CleanupCommitError("existing backup collection mismatch")
        if record.get("document_sha256") != entry.document_sha256:
            raise CleanupCommitError("existing backup digest field mismatch")
        if _entry_mismatches(entry, document):
            raise CleanupCommitError("existing backup document no longer matches manifest")
        documents.append(document)
    return documents


def _journal_event(db, manifest: CleanupManifest, event: str, **detail: Any) -> None:
    db["scraper_cleanup_journal"].insert_one({
        "cleanup_id": manifest.cleanup_id,
        "manifest_sha256": manifest.sha256,
        "event": event,
        "recorded_at": datetime.now(timezone.utc),
        **detail,
    })


def _apply_standalone_recoverable(
    db,
    manifest: CleanupManifest,
    backup_documents: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    """Apply quarantine-first cleanup with a durable event journal.

    This mode is for standalone MongoDB servers that cannot provide multirow
    transactions.  Each source row is deleted only after its exact full copy
    is present and verified in the quarantine collection.  A retry can safely
    resume from source+copy or copy-only state using the immutable backup.
    """
    if manifest.action != "quarantine":
        raise CleanupCommitError("standalone recoverable mode requires quarantine action")
    _journal_event(db, manifest, "attempt_started", requested_rows=len(manifest.entries))
    completed = 0
    resumed = 0
    for entry, backup_document in zip(manifest.entries, backup_documents):
        source = db[entry.collection].find_one({"_id": entry.object_id})
        source_issues = _entry_mismatches(entry, source) if source is not None else []
        if source_issues:
            raise CleanupCommitError(
                f"live source row changed before recoverable cleanup: {entry.collection}/{entry.object_id}"
            )
        query = _quarantine_query(manifest, entry)
        copies = db[manifest.quarantine_collection].count_documents(query, limit=2)
        if copies > 1:
            raise CleanupCommitError("multiple quarantine copies block recoverable cleanup")
        quarantine = db[manifest.quarantine_collection].find_one(query) if copies else None
        if quarantine is not None:
            quarantine_issues = _validate_quarantine_copy(manifest, entry, quarantine)
            if quarantine_issues:
                raise CleanupCommitError("existing quarantine copy failed digest validation")
            resumed += 1
        else:
            if source is None:
                raise CleanupCommitError("source and quarantine copy are both absent; restore review required")
            if document_sha256(backup_document) != entry.document_sha256:
                raise CleanupCommitError("backup digest changed before quarantine insert")
            quarantine_document = {
                "cleanup_id": manifest.cleanup_id,
                "manifest_sha256": manifest.sha256,
                "original_collection": entry.collection,
                "original_id": entry.object_id,
                "original_url": entry.url,
                "source_domain": entry.source_domain,
                "reason": entry.reason,
                "evidence": entry.evidence,
                "document_sha256": entry.document_sha256,
                "quarantined_at": datetime.now(timezone.utc),
                "original_document": backup_document,
            }
            db[manifest.quarantine_collection].insert_one(quarantine_document)
            quarantine = db[manifest.quarantine_collection].find_one(query)
            if _validate_quarantine_copy(manifest, entry, quarantine):
                raise CleanupCommitError("inserted quarantine copy failed verification")
            _journal_event(
                db, manifest, "quarantine_verified",
                original_collection=entry.collection, original_id=entry.object_id,
                document_sha256=entry.document_sha256,
            )
        if source is not None:
            result = db[entry.collection].delete_one(_guard_filter(entry))
            if result.deleted_count != 1:
                raise CleanupCommitError("guarded standalone deletion did not match exactly one row")
        if db[entry.collection].find_one({"_id": entry.object_id}) is not None:
            raise CleanupCommitError("source row remains after guarded standalone deletion")
        final_copy = db[manifest.quarantine_collection].find_one(query)
        if _validate_quarantine_copy(manifest, entry, final_copy):
            raise CleanupCommitError("quarantine copy failed post-deletion verification")
        completed += 1
        _journal_event(
            db, manifest, "row_completed",
            original_collection=entry.collection, original_id=entry.object_id,
            document_sha256=entry.document_sha256,
        )
    _journal_event(db, manifest, "attempt_completed", completed_rows=completed)
    return completed, resumed


def _execute_standalone_recoverable(
    db,
    manifest: CleanupManifest,
    *,
    backup_path: Path,
    audit_path: Path,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    audit: dict[str, Any] = {
        "cleanup_id": manifest.cleanup_id,
        "manifest_sha256": manifest.sha256,
        "action": manifest.action,
        "mode": "standalone_recoverable_commit",
        "requested_rows": len(manifest.entries),
        "started_at": started.isoformat(),
        "backup_path": str(backup_path.resolve()),
        "status": "validating",
        "mismatches": [],
        "postcheck_issues": [],
    }
    if audit_path.exists():
        raise CleanupCommitError(f"audit path already exists: {audit_path}")
    try:
        if backup_path.exists():
            documents = _read_backup_documents(backup_path, manifest)
            audit["resumed_from_existing_backup"] = True
            backup = backup_path.read_text(encoding="utf-8")
        else:
            documents, mismatches = _preflight(db, manifest)
            if mismatches:
                audit["status"] = "blocked_manifest_mismatch"
                audit["matched_rows"] = len(documents)
                audit["mismatches"] = mismatches
                audit["finished_at"] = datetime.now(timezone.utc).isoformat()
                _write_audit(audit_path, audit)
                return audit
            backup = _backup_content(manifest, documents)
            _write_exclusive(backup_path, backup)
            audit["resumed_from_existing_backup"] = False
        audit["backup_sha256"] = hashlib.sha256(backup.encode("utf-8")).hexdigest()
        audit["backup_rows"] = len(documents)
        completed, resumed = _apply_standalone_recoverable(db, manifest, documents)
        audit["completed_rows"] = completed
        audit["resumed_rows"] = resumed
    except Exception as exc:
        audit["status"] = "recoverable_commit_incomplete"
        audit["error_type"] = type(exc).__name__
        audit["error"] = str(exc)[:1000]
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_audit(audit_path, audit)
        return audit
    try:
        issues = _postcheck(db, manifest)
    except Exception as exc:
        audit["status"] = "committed_postcheck_unknown"
        audit["error_type"] = type(exc).__name__
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_audit(audit_path, audit)
        return audit
    audit["postcheck_issues"] = issues
    audit["status"] = "committed" if not issues else "committed_postcheck_failed"
    audit["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_audit(audit_path, audit)
    return audit


def _postcheck(db, manifest: CleanupManifest) -> list[dict]:
    issues: list[dict] = []
    for entry in manifest.entries:
        if db[entry.collection].find_one({"_id": entry.object_id}) is not None:
            issues.append({
                "collection": entry.collection,
                "_id": str(entry.object_id),
                "issue": "source_row_still_present",
            })
        if manifest.action == "quarantine":
            query = {
                "cleanup_id": manifest.cleanup_id,
                "original_collection": entry.collection,
                "original_id": entry.object_id,
                "document_sha256": entry.document_sha256,
            }
            if db[manifest.quarantine_collection].count_documents(query, limit=2) != 1:
                issues.append({
                    "collection": entry.collection,
                    "_id": str(entry.object_id),
                    "issue": "quarantine_copy_count_not_one",
                })
    return issues


def execute_cleanup(
    db,
    manifest: CleanupManifest,
    *,
    backup_path: Path,
    audit_path: Path,
    commit: bool = False,
    standalone_recoverable: bool = False,
) -> dict[str, Any]:
    """Validate a manifest and optionally execute it.

    Dry-run is the default and never writes a backup or database row.  Both dry
    runs and commits write an immutable audit file.  Commit requires a new
    backup path; an existing backup or audit path blocks the operation.
    """

    if standalone_recoverable:
        if not commit:
            raise CleanupCommitError("standalone recoverable mode requires commit=True")
        return _execute_standalone_recoverable(
            db, manifest, backup_path=backup_path, audit_path=audit_path,
        )

    started = datetime.now(timezone.utc)
    audit: dict[str, Any] = {
        "cleanup_id": manifest.cleanup_id,
        "manifest_sha256": manifest.sha256,
        "action": manifest.action,
        "mode": "commit" if commit else "dry_run",
        "requested_rows": len(manifest.entries),
        "started_at": started.isoformat(),
        "backup_path": str(backup_path.resolve()) if commit else None,
        "status": "validating",
        "mismatches": [],
        "postcheck_issues": [],
    }
    if audit_path.exists():
        raise CleanupCommitError(f"audit path already exists: {audit_path}")
    if commit and backup_path.exists():
        raise CleanupCommitError(f"backup path already exists: {backup_path}")

    try:
        documents, mismatches = _preflight(db, manifest)
    except Exception as exc:
        audit["status"] = "validation_failed"
        audit["error_type"] = type(exc).__name__
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_audit(audit_path, audit)
        return audit
    if mismatches:
        audit["status"] = "blocked_manifest_mismatch"
        audit["matched_rows"] = len(documents)
        audit["mismatches"] = mismatches
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_audit(audit_path, audit)
        return audit

    audit["matched_rows"] = len(documents)
    if not commit:
        audit["status"] = "validated_dry_run"
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_audit(audit_path, audit)
        return audit

    backup = _backup_content(manifest, documents)
    try:
        _write_exclusive(backup_path, backup)
    except Exception as exc:
        audit["status"] = "backup_failed"
        audit["error_type"] = type(exc).__name__
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_audit(audit_path, audit)
        return audit
    audit["backup_sha256"] = hashlib.sha256(backup.encode("utf-8")).hexdigest()
    audit["backup_rows"] = len(documents)
    try:
        _apply_transaction(db, manifest)
    except Exception as exc:
        audit["status"] = "commit_failed_or_outcome_unknown"
        audit["error_type"] = type(exc).__name__
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_audit(audit_path, audit)
        return audit

    try:
        postcheck_issues = _postcheck(db, manifest)
    except Exception as exc:
        audit["status"] = "committed_postcheck_unknown"
        audit["error_type"] = type(exc).__name__
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_audit(audit_path, audit)
        return audit
    audit["postcheck_issues"] = postcheck_issues
    audit["status"] = "committed" if not postcheck_issues else "committed_postcheck_failed"
    audit["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_audit(audit_path, audit)
    return audit


def audit_exit_code(audit: Mapping[str, Any]) -> int:
    return 0 if audit.get("status") in {"validated_dry_run", "committed"} else 2
