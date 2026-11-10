from __future__ import annotations

import ast
import builtins
import copy
import fcntl
import hashlib
import json
import os
import stat
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops.host_capacity_guard import build_capacity_scope
from ops import promote_moscow_exact_20261109 as operator


WORKSPACE = Path(__file__).resolve().parents[2]
PREPARATION_DIR = WORKSPACE / "outputs/operations/moscow_exact_promotion_preparation_20261109_0615"
PAYLOAD_DIR = WORKSPACE / "outputs/operations/moscow_recombination_20261014_2215"


def verified_inputs() -> operator.VerifiedInput:
    return operator.verify_static_inputs(
        PREPARATION_DIR / "promotion_preparation.private.json",
        PREPARATION_DIR / "independent_audit.private.json",
        PREPARATION_DIR / "promotion_preparation_seal.private.json",
        PREPARATION_DIR / "seal_independent_audit.private.json",
        PAYLOAD_DIR / "urls_430.jsonl",
        PAYLOAD_DIR,
    )


def private_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_name(path.name + ".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    sidecar.chmod(0o600)


def execution_fixture(tmp_path: Path, operator_sha: str):
    attempt_parent = tmp_path / "attempts"
    framework_root = tmp_path / "framework"
    attempt_parent.mkdir(mode=0o700)
    framework_root.mkdir()
    framework_file = framework_root / "core/base_pipeline.py"
    framework_file.parent.mkdir()
    framework_file.write_text("# pinned test connector\n", encoding="utf-8")
    lock_parent = operator.CANONICAL_LOCK_ROOT
    lock_parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock_parent.chmod(0o700)
    framework_proof = tmp_path / "framework_code_proof.private.json"
    capacity_receipt = tmp_path / "capacity_receipt.private.json"
    capacity_review = tmp_path / "capacity_review.private.json"
    private_json(framework_proof, {
        "kind": "private_moscow_promotion_framework_code_proof",
        "result": "approved",
        "database_runtime_imported": False,
        "dependency_metadata_without_imports": True,
        "dependency_closure_complete": True,
        "operator_sha256": operator_sha,
        "runtime_connector_path": "core/base_pipeline.py",
        "files": [{"path": "core/base_pipeline.py",
                   "sha256": operator.sha256_path(framework_file)}],
        "root_identity_sha256": operator.sha256_bytes(operator.canonical_json_bytes([
            {"path": "core/base_pipeline.py", "sha256": operator.sha256_path(framework_file)}
        ])),
    })
    current = datetime.now(timezone.utc).replace(microsecond=0)
    not_before = current - timedelta(minutes=10)
    expires = current + timedelta(minutes=80)
    scheduled_local = current.astimezone(operator.ZoneInfo("America/New_York")).replace(
        second=0, microsecond=0
    ).strftime("%Y-%m-%d %H:%M")
    capacity_scope = build_capacity_scope(
        [tmp_path / "scope-root-0"],
        [tmp_path / f"scope-root-{index}" for index in range(1, 5)],
        [f"scraper-{index}" for index in range(6)],
        [f"browser-{index}" for index in range(6)],
    )
    private_json(capacity_receipt, {
        "kind": "host_capacity_guard",
        "generated_at": current.isoformat().replace("+00:00", "Z"),
        "status": "reduced",
        "launch_allowed": True,
        "metrics": {
            "inspected_roots": 5,
            "configured_roots_total_bytes": 0,
            "scrape_processes": 0,
            "browser_processes": 0,
        },
        "recommended_limits": {"network_workers": 1, "browser_workers": 0},
        "scope": capacity_scope,
    })
    capacity_sha = operator.sha256_path(capacity_receipt)
    private_json(capacity_review, {
        "result": "approved",
        "blocker_count": 0,
        "guard_receipt_sha256": capacity_sha,
        "scheduled_window_local": scheduled_local,
        "capacity_scope_sha256": operator.sha256_bytes(
            operator.canonical_json_bytes(capacity_scope)
        ),
    })
    packet = tmp_path / "packet.private.json"
    authorization = tmp_path / "authorization.private.json"
    attempt_id = "attempt-1"
    packet_value = {
        "kind": "private_moscow_exact_promotion_execution_packet",
        "source_id": operator.SOURCE_ID,
        "operator_sha256": operator_sha,
        "preparation_sha256": operator.PREPARATION_SHA256,
        "preparation_seal_sha256": operator.PREPARATION_SEAL_SHA256,
        "identity_manifest_sha256": operator.IDENTITY_MANIFEST_SHA256,
        "identity_set_sha256": operator.EXPECTED_IDENTITY_SET_SHA256,
        "artifact_sha256": {
            "articles": operator.ARTICLES_SHA256,
            "field_checks": operator.FIELD_CHECKS_SHA256,
            "manifest": operator.MANIFEST_SHA256,
            "recombination_receipt": operator.RECOMBINATION_RECEIPT_SHA256,
        },
        "publication_date_field": operator.DATE_FIELD,
        "month_ledger": operator.EXPECTED_MONTH_LEDGER,
        "execution_allowed": True,
        "attempt_id": attempt_id,
        "window_id": scheduled_local.replace("-", "").replace(" ", "_").replace(":", ""),
        "scheduled_window_local": scheduled_local,
        "capacity_scope": capacity_scope,
        "paths": {
            "preparation_record": str((PREPARATION_DIR / "promotion_preparation.private.json").resolve()),
            "preparation_audit": str((PREPARATION_DIR / "independent_audit.private.json").resolve()),
            "preparation_seal": str((PREPARATION_DIR / "promotion_preparation_seal.private.json").resolve()),
            "seal_audit": str((PREPARATION_DIR / "seal_independent_audit.private.json").resolve()),
            "identity_manifest": str((PAYLOAD_DIR / "urls_430.jsonl").resolve()),
            "payload_dir": str(PAYLOAD_DIR.resolve()),
            "attempt_parent": str(attempt_parent.resolve()),
            "attempt_dir": str((attempt_parent / attempt_id).resolve()),
            "framework_root": str(framework_root.resolve()),
            "framework_code_proof": str(framework_proof.resolve()),
            "lock_root": str(lock_parent.resolve()),
            "global_lock": str((lock_parent / "exact-promotion-global.lock").resolve()),
            "source_lock": str((lock_parent /
                                "exact-promotion-mlp-int-themoscowtimes-com.lock").resolve()),
            "capacity_receipt": str(capacity_receipt.resolve()),
            "capacity_review": str(capacity_review.resolve()),
        },
        "evidence_sha256": {
            "framework_code_proof": operator.sha256_path(framework_proof),
            "capacity_receipt": capacity_sha,
            "capacity_review": operator.sha256_path(capacity_review),
        },
    }
    private_json(packet, packet_value)
    packet_sha = operator.sha256_path(packet)
    private_json(authorization, {
        "kind": "private_moscow_exact_promotion_authorization",
        "approved": True,
        "independent_review_approved": True,
        "execution_packet_sha256": packet_sha,
        "operator_sha256": operator_sha,
        "attempt_id": attempt_id,
        "window_id": packet_value["window_id"],
        "scheduled_window_local": scheduled_local,
        "not_before_utc": not_before.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": expires.isoformat().replace("+00:00", "Z"),
        "capacity_receipt_sha256": capacity_sha,
        "capacity_review_sha256": operator.sha256_path(capacity_review),
        "framework_code_proof_sha256": operator.sha256_path(framework_proof),
        "capacity_scope_sha256": operator.sha256_bytes(
            operator.canonical_json_bytes(capacity_scope)
        ),
        "process_quiescence_verified": True,
    })
    expected_inputs = {
        "preparation_record": PREPARATION_DIR / "promotion_preparation.private.json",
        "preparation_audit": PREPARATION_DIR / "independent_audit.private.json",
        "preparation_seal": PREPARATION_DIR / "promotion_preparation_seal.private.json",
        "seal_audit": PREPARATION_DIR / "seal_independent_audit.private.json",
        "identity_manifest": PAYLOAD_DIR / "urls_430.jsonl",
        "payload_dir": PAYLOAD_DIR,
    }
    return packet, authorization, expected_inputs, packet_value


def rebind_packet_and_authorization(packet: Path, authorization: Path,
                                    packet_value: dict, *, refresh_review=False) -> None:
    if refresh_review:
        capacity = Path(packet_value["paths"]["capacity_receipt"])
        review = Path(packet_value["paths"]["capacity_review"])
        review_value = json.loads(review.read_text())
        review_value["guard_receipt_sha256"] = operator.sha256_path(capacity)
        review.unlink()
        review.with_name(review.name + ".sha256").unlink()
        private_json(review, review_value)
    packet_value["evidence_sha256"]["capacity_receipt"] = operator.sha256_path(
        Path(packet_value["paths"]["capacity_receipt"])
    )
    packet_value["evidence_sha256"]["capacity_review"] = operator.sha256_path(
        Path(packet_value["paths"]["capacity_review"])
    )
    packet_value["evidence_sha256"]["framework_code_proof"] = operator.sha256_path(
        Path(packet_value["paths"]["framework_code_proof"])
    )
    packet.unlink()
    packet.with_name(packet.name + ".sha256").unlink()
    private_json(packet, packet_value)
    authorization_value = json.loads(authorization.read_text())
    authorization_value["execution_packet_sha256"] = operator.sha256_path(packet)
    authorization_value["capacity_receipt_sha256"] = \
        packet_value["evidence_sha256"]["capacity_receipt"]
    authorization_value["capacity_review_sha256"] = \
        packet_value["evidence_sha256"]["capacity_review"]
    authorization_value["framework_code_proof_sha256"] = \
        packet_value["evidence_sha256"]["framework_code_proof"]
    authorization_value["capacity_scope_sha256"] = operator.sha256_bytes(
        operator.canonical_json_bytes(packet_value["capacity_scope"])
    )
    authorization.unlink()
    authorization.with_name(authorization.name + ".sha256").unlink()
    private_json(authorization, authorization_value)


class Collection:
    def __init__(self, rows=None):
        self.rows = [copy.deepcopy(row) for row in (rows or [])]

    @staticmethod
    def _match(row, query):
        for key, expected in query.items():
            if isinstance(expected, dict) and "$in" in expected:
                if row.get(key) not in expected["$in"]:
                    return False
            elif row.get(key) != expected:
                return False
        return True

    def find(self, query, **kwargs):
        return [copy.deepcopy(row) for row in self.rows if self._match(row, query)]

    def count_documents(self, query, **kwargs):
        return sum(1 for row in self.rows if self._match(row, query))

    def insert_one(self, row, **kwargs):
        self.rows.append(copy.deepcopy(row))


class Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection())


class Session:
    def __init__(self, *, commit_error=False):
        self.commit_error = commit_error
        self.started = False
        self.committed = False
        self.aborted = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def start_transaction(self):
        self.started = True

    def commit_transaction(self):
        if self.commit_error:
            raise RuntimeError("ambiguous commit")
        self.committed = True

    def abort_transaction(self):
        self.aborted = True


class Client:
    def __init__(self, *, session=None):
        self.closed = False
        self.session = session or Session()

    def start_session(self):
        return self.session

    def close(self):
        self.closed = True


def seed_exact(db: Database, candidates, *, count=None, indexes=True):
    chosen = candidates if count is None else candidates[:count]
    for candidate in chosen:
        db[candidate.collection].rows.append(candidate.payload())
        if indexes:
            db["urls"].rows.append({"url": candidate.url, "source_domain": operator.SOURCE_DOMAIN})


def test_static_verification_reconciles_exact_payload_and_months():
    verified = verified_inputs()
    assert len(verified.candidates) == 430
    assert verified.identity_set_sha256 == operator.EXPECTED_IDENTITY_SET_SHA256
    assert verified.month_ledger == {"2026-08": 212, "2026-09": 218}


def test_import_has_no_database_driver_or_network_side_effect_imports():
    source_path = WORKSPACE / "automation/ops/promote_moscow_exact_20261109.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    top_imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imports.update(item.name.split(".")[0] for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_imports.add(node.module.split(".")[0])
    assert "pymongo" not in top_imports
    assert "bson" not in top_imports
    assert "subprocess" not in top_imports


def test_source_forbids_legacy_or_destructive_operations():
    text = (WORKSPACE / "automation/ops/promote_moscow_exact_20261109.py").read_text()
    for forbidden in (
        "guarded_observation_promote", "delete_one", "delete_many", "update_many",
        "replace_one", "bulk_write", "subprocess.",
    ):
        assert forbidden not in text


def test_default_cli_mode_is_offline_and_does_not_load_database(monkeypatch, capsys):
    monkeypatch.setattr(operator, "load_database", lambda *_: pytest.fail("database opened"))
    rc = operator.main([
        "--preparation-record", str(PREPARATION_DIR / "promotion_preparation.private.json"),
        "--preparation-audit", str(PREPARATION_DIR / "independent_audit.private.json"),
        "--preparation-seal", str(PREPARATION_DIR / "promotion_preparation_seal.private.json"),
        "--seal-audit", str(PREPARATION_DIR / "seal_independent_audit.private.json"),
        "--identity-manifest", str(PAYLOAD_DIR / "urls_430.jsonl"),
        "--payload-dir", str(PAYLOAD_DIR),
    ])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["state"] == "offline_verified_no_write"
    assert summary["production_write_performed"] is False


def test_identity_manifest_hash_mismatch_fails_closed(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in (operator.ARTICLES_NAME, operator.IDENTITIES_NAME,
                 operator.FIELD_CHECKS_NAME, operator.MANIFEST_NAME, operator.RECEIPT_NAME):
        (payload / name).write_bytes((PAYLOAD_DIR / name).read_bytes())
        source_sidecar = PAYLOAD_DIR / (name + ".sha256")
        if source_sidecar.exists():
            (payload / (name + ".sha256")).write_bytes(source_sidecar.read_bytes())
    changed = payload / operator.IDENTITIES_NAME
    changed.write_bytes(changed.read_bytes() + b"\n")
    with pytest.raises(operator.PromotionError, match="file_hash_mismatch"):
        operator.verify_static_inputs(
            PREPARATION_DIR / "promotion_preparation.private.json",
            PREPARATION_DIR / "independent_audit.private.json",
            PREPARATION_DIR / "promotion_preparation_seal.private.json",
            PREPARATION_DIR / "seal_independent_audit.private.json",
            changed,
            payload,
        )


@pytest.mark.parametrize("field,value,code", [
    ("source_domain", "wrong.example", "payload_source_mismatch"),
    ("collection", "opinion-articles", "unexpected_opinion_route"),
    ("title", "", "title_required"),
    ("maintext", "", "maintext_required"),
    ("date_publish", "2026-10-01T00:00:00", "date_publish_out_of_scope"),
])
def test_changed_payload_contract_fails_before_database(tmp_path, monkeypatch, field, value, code):
    payload = tmp_path / "payload"
    payload.mkdir()
    for name in (operator.IDENTITIES_NAME, operator.FIELD_CHECKS_NAME,
                 operator.MANIFEST_NAME, operator.RECEIPT_NAME):
        (payload / name).write_bytes((PAYLOAD_DIR / name).read_bytes())
    rows = operator.read_jsonl(PAYLOAD_DIR / operator.ARTICLES_NAME)
    rows[0][field] = value
    article_path = payload / operator.ARTICLES_NAME
    article_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(operator, "require_regular", lambda *args, **kwargs: None)
    monkeypatch.setattr(operator, "verify_preparation", lambda *args, **kwargs: None)
    with pytest.raises(operator.PromotionError, match=code):
        operator.verify_static_inputs(
            PREPARATION_DIR / "promotion_preparation.private.json",
            PREPARATION_DIR / "independent_audit.private.json",
            PREPARATION_DIR / "promotion_preparation_seal.private.json",
            PREPARATION_DIR / "seal_independent_audit.private.json",
            payload / operator.IDENTITIES_NAME,
            payload,
        )


def test_canonical_alias_collision_is_rejected():
    verified = verified_inputs()
    db = Database()
    candidate = verified.candidates[0]
    alias = next(value for value in operator.canonical_variants(candidate.url)
                 if value != candidate.url and operator.canonical_url(value) ==
                 operator.canonical_url(candidate.url))
    db["urls"].rows.append({"url": alias})
    snapshot = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="index_canonical_alias_collision"):
        operator.verify_prewrite(snapshot, verified.candidates)


def test_canonical_article_alias_without_source_domain_is_rejected():
    verified = verified_inputs()
    db = Database()
    candidate = verified.candidates[0]
    alias = next(value for value in operator.canonical_variants(candidate.url)
                 if value != candidate.url and operator.canonical_url(value) ==
                 operator.canonical_url(candidate.url))
    row = candidate.payload()
    row["url"] = alias
    row.pop("source_domain")
    db[candidate.collection].rows.append(row)
    snapshot = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError,
                       match="prewrite_article_canonical_alias_collision"):
        operator.verify_prewrite(snapshot, verified.candidates)


def test_prewrite_empty_database_plans_all_rows_and_indexes():
    verified = verified_inputs()
    db = Database()
    snapshot = operator.capture_snapshot(db, verified.candidates)
    plan = operator.verify_prewrite(snapshot, verified.candidates)
    assert len(plan["missing_articles"]) == 430
    assert len(plan["missing_indexes"]) == 430
    assert not plan["already_applied_exact"]


def test_prewrite_fully_exact_database_is_idempotent():
    verified = verified_inputs()
    db = Database()
    seed_exact(db, verified.candidates)
    snapshot = operator.capture_snapshot(db, verified.candidates)
    plan = operator.verify_prewrite(snapshot, verified.candidates)
    assert len(plan["already_applied_exact"]) == 430
    assert not plan["missing_articles"]
    assert not plan["missing_indexes"]


def test_prewrite_mixed_state_inserts_only_missing():
    verified = verified_inputs()
    db = Database()
    seed_exact(db, verified.candidates, count=125)
    pre = operator.capture_snapshot(db, verified.candidates)
    plan = operator.verify_prewrite(pre, verified.candidates)
    operator.insert_missing(db, verified.candidates, plan, session=object())
    post = operator.capture_snapshot(db, verified.candidates)
    result = operator.verify_postwrite(pre, post, verified.candidates, plan)
    assert result["already_applied_exact"] == 125
    assert result["inserted_articles"] == 305
    assert result["month_ledger"] == operator.EXPECTED_MONTH_LEDGER


def test_prewrite_duplicate_article_fails_without_write():
    verified = verified_inputs()
    db = Database()
    candidate = verified.candidates[0]
    db[candidate.collection].rows.extend([candidate.payload(), candidate.payload()])
    snapshot = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="prewrite_duplicate_article"):
        operator.verify_prewrite(snapshot, verified.candidates)


def test_prewrite_existing_field_mismatch_fails():
    verified = verified_inputs()
    db = Database()
    candidate = verified.candidates[0]
    wrong = candidate.payload()
    wrong["title"] = "changed"
    db[candidate.collection].rows.append(wrong)
    snapshot = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="prewrite_existing_mismatch"):
        operator.verify_prewrite(snapshot, verified.candidates)


def test_prewrite_wrong_collection_fails():
    verified = verified_inputs()
    db = Database()
    candidate = verified.candidates[0]
    db["opinion-articles-2026-8"].rows.append(candidate.payload())
    snapshot = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="prewrite_wrong_collection"):
        operator.verify_prewrite(snapshot, verified.candidates)


def test_prewrite_orphan_index_fails():
    verified = verified_inputs()
    db = Database()
    db["urls"].rows.append({"url": verified.candidates[0].url, "source_domain": operator.SOURCE_DOMAIN})
    snapshot = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="prewrite_orphan_index"):
        operator.verify_prewrite(snapshot, verified.candidates)


def test_prewrite_duplicate_index_fails():
    verified = verified_inputs()
    db = Database()
    candidate = verified.candidates[0]
    db[candidate.collection].rows.append(candidate.payload())
    db["urls"].rows.extend([
        {"url": candidate.url, "source_domain": operator.SOURCE_DOMAIN},
        {"url": candidate.url, "source_domain": operator.SOURCE_DOMAIN},
    ])
    snapshot = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="prewrite_duplicate_index"):
        operator.verify_prewrite(snapshot, verified.candidates)


def test_prewrite_unexpected_source_owned_index_fails():
    verified = verified_inputs()
    db = Database()
    db["urls"].rows.append({
        "url": "https://themoscowtimes.com/outside-exact-set",
        "source_domain": operator.SOURCE_DOMAIN,
    })
    snapshot = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="prewrite_unexpected_source_index"):
        operator.verify_prewrite(snapshot, verified.candidates)


def test_postwrite_missing_article_fails():
    verified = verified_inputs()
    db = Database()
    pre = operator.capture_snapshot(db, verified.candidates)
    plan = operator.verify_prewrite(pre, verified.candidates)
    operator.insert_missing(db, verified.candidates, plan, session=object())
    candidate = verified.candidates[0]
    db[candidate.collection].rows = [row for row in db[candidate.collection].rows
                                     if row["url"] != candidate.url]
    post = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="postwrite_article_count_mismatch"):
        operator.verify_postwrite(pre, post, verified.candidates, plan)


def test_prewrite_unexpected_scoped_source_row_fails():
    verified = verified_inputs()
    db = Database()
    db["articles-2026-8"].rows.append({
        "url": "https://themoscowtimes.com/non-target",
        "source_domain": operator.SOURCE_DOMAIN,
        "title": "Other", "maintext": "Other", "date_publish": datetime(2026, 8, 1),
        "download_via": "legacy",
    })
    pre = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="prewrite_unexpected_source_row"):
        operator.verify_prewrite(pre, verified.candidates)


def test_postwrite_unexpected_scoped_source_row_fails_exact_set():
    verified = verified_inputs()
    db = Database()
    pre = operator.capture_snapshot(db, verified.candidates)
    plan = operator.verify_prewrite(pre, verified.candidates)
    operator.insert_missing(db, verified.candidates, plan, session=object())
    db["articles-2026-8"].rows.append({
        "url": "https://themoscowtimes.com/non-target",
        "source_domain": operator.SOURCE_DOMAIN,
        "title": "Other", "maintext": "Other", "date_publish": datetime(2026, 8, 1),
        "download_via": "legacy",
    })
    post = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="postwrite_source_exact_set_mismatch"):
        operator.verify_postwrite(pre, post, verified.candidates, plan)


def test_postwrite_source_meta_mutation_fails():
    verified = verified_inputs()
    db = Database()
    db["source_meta"].rows.append({"source_domain": operator.SOURCE_DOMAIN, "value": 1})
    pre = operator.capture_snapshot(db, verified.candidates)
    plan = operator.verify_prewrite(pre, verified.candidates)
    operator.insert_missing(db, verified.candidates, plan, session=object())
    db["source_meta"].rows[0]["value"] = 2
    post = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="postwrite_source_meta_mutation"):
        operator.verify_postwrite(pre, post, verified.candidates, plan)


def test_postwrite_unrelated_collection_delta_fails():
    verified = verified_inputs()
    db = Database()
    pre = operator.capture_snapshot(db, verified.candidates)
    plan = operator.verify_prewrite(pre, verified.candidates)
    operator.insert_missing(db, verified.candidates, plan, session=object())
    db["articles-2026-8"].rows.append({"url": "https://other.example/unrelated"})
    post = operator.capture_snapshot(db, verified.candidates)
    with pytest.raises(operator.PromotionError, match="postwrite_unrelated_collection_delta"):
        operator.verify_postwrite(pre, post, verified.candidates, plan)


def test_private_receipt_is_create_only_owner_only_and_hash_pinned(tmp_path):
    path = tmp_path / "receipt.private.json"
    digest = operator.create_private_json(path, {"value": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert digest == operator.sha256_path(path)
    assert path.with_name(path.name + ".sha256").read_text().split()[0] == digest
    with pytest.raises(FileExistsError):
        operator.create_private_json(path, {"value": 2})


def test_execution_evidence_without_sidecar_is_rejected(tmp_path):
    path = tmp_path / "evidence.private.json"
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(operator.PromotionError, match="sidecar_required"):
        operator.require_regular(path, private=True)


def test_attempt_marker_is_single_use(tmp_path):
    attempt = tmp_path / "attempt"
    operator.create_attempt_marker(attempt, "attempt-1", "a" * 64)
    with pytest.raises(operator.PromotionError, match="attempt_directory_already_exists"):
        operator.create_attempt_marker(attempt, "attempt-1", "a" * 64)


def test_lock_is_nonblocking_and_rejects_busy_file(tmp_path):
    lock_root = tmp_path / "locks"
    lock_root.mkdir(mode=0o700)
    path = lock_root / "source.lock"
    with path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ExitStack() as stack, pytest.raises(operator.PromotionError,
                                                  match="promotion_lock_busy"):
            operator.acquire_lock(stack, path)


def test_lock_rejects_symlink(tmp_path):
    lock_root = tmp_path / "locks"
    lock_root.mkdir(mode=0o700)
    target = lock_root / "target.lock"
    target.touch(mode=0o600)
    link = lock_root / "source.lock"
    link.symlink_to(target)
    with ExitStack() as stack, pytest.raises(operator.PromotionError,
                                              match="lock_open_failed"):
        operator.acquire_lock(stack, link)


def test_execution_documents_require_current_exact_authorization(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, _ = execution_fixture(tmp_path, operator_sha)
    result = operator.validate_execution_documents(
        packet, authorization, operator_sha, expected_inputs,
        now=datetime.now(timezone.utc),
    )
    assert result["packet_sha256"] == operator.sha256_path(packet)
    assert result["paths"]["attempt_dir"].name == "attempt-1"


def test_execution_documents_reject_expired_or_mismatched_authorization(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, _ = execution_fixture(tmp_path, operator_sha)
    auth_value = json.loads(authorization.read_text())
    current = datetime.now(timezone.utc)
    auth_value["not_before_utc"] = (current - timedelta(hours=2)).isoformat()
    auth_value["expires_at_utc"] = (current - timedelta(hours=1)).isoformat()
    authorization.unlink()
    authorization.with_name(authorization.name + ".sha256").unlink()
    private_json(authorization, auth_value)
    with pytest.raises(operator.PromotionError, match="authorization_outside_time_window"):
        operator.validate_execution_documents(
            packet, authorization, operator_sha, expected_inputs,
            now=current,
        )


def test_execution_documents_reject_replayed_attempt_path(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, packet_value = execution_fixture(tmp_path, operator_sha)
    packet_value["paths"]["attempt_dir"] = str((tmp_path / "other" / "attempt-1").resolve())
    rebind_packet_and_authorization(packet, authorization, packet_value)
    with pytest.raises(operator.PromotionError, match="execution_packet_attempt_path_mismatch"):
        operator.validate_execution_documents(
            packet, authorization, operator_sha, expected_inputs,
            now=datetime.now(timezone.utc),
        )


def test_execution_documents_reject_nonlaunchable_capacity_receipt(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, packet_value = execution_fixture(tmp_path, operator_sha)
    capacity = Path(packet_value["paths"]["capacity_receipt"])
    capacity.unlink()
    capacity.with_name(capacity.name + ".sha256").unlink()
    private_json(capacity, {
        "kind": "host_capacity_guard", "status": "blocked", "launch_allowed": False,
        "metrics": {"inspected_roots": 5, "configured_roots_total_bytes": 0,
                    "scrape_processes": 0, "browser_processes": 0},
        "recommended_limits": {"network_workers": 0, "browser_workers": 0},
    })
    rebind_packet_and_authorization(
        packet, authorization, packet_value, refresh_review=True
    )
    with pytest.raises(operator.PromotionError, match="capacity_receipt_not_launchable"):
        operator.validate_execution_documents(
            packet, authorization, operator_sha, expected_inputs,
            now=datetime.now(timezone.utc),
        )


def test_execution_documents_reject_stale_capacity_outside_authorization(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, packet_value = execution_fixture(tmp_path, operator_sha)
    capacity = Path(packet_value["paths"]["capacity_receipt"])
    capacity_value = json.loads(capacity.read_text())
    authorization_value = json.loads(authorization.read_text())
    not_before = datetime.fromisoformat(authorization_value["not_before_utc"])
    capacity_value["generated_at"] = (not_before - timedelta(seconds=1)).isoformat()
    capacity.unlink()
    capacity.with_name(capacity.name + ".sha256").unlink()
    private_json(capacity, capacity_value)
    rebind_packet_and_authorization(
        packet, authorization, packet_value, refresh_review=True
    )
    with pytest.raises(operator.PromotionError, match="capacity_outside_authorization_window"):
        operator.validate_execution_documents(
            packet, authorization, operator_sha, expected_inputs,
            now=datetime.now(timezone.utc),
        )


def test_execution_documents_reject_framework_runtime_hash_mismatch(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, packet_value = execution_fixture(tmp_path, operator_sha)
    framework_file = Path(packet_value["paths"]["framework_root"]) / "core/base_pipeline.py"
    framework_file.write_text("# changed after proof\n", encoding="utf-8")
    with pytest.raises(operator.PromotionError, match="file_hash_mismatch"):
        operator.validate_execution_documents(
            packet, authorization, operator_sha, expected_inputs,
            now=datetime.now(timezone.utc),
        )


def test_execution_documents_require_canonical_cross_packet_locks(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, packet_value = execution_fixture(tmp_path, operator_sha)
    packet_value["paths"]["global_lock"] = str((tmp_path / "private-global.lock").resolve())
    rebind_packet_and_authorization(packet, authorization, packet_value)
    with pytest.raises(operator.PromotionError, match="execution_packet_lock_paths_invalid"):
        operator.validate_execution_documents(
            packet, authorization, operator_sha, expected_inputs,
            now=datetime.now(timezone.utc),
        )


def test_two_framework_roots_share_one_canonical_lock_namespace(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_packet, _, _, first_value = execution_fixture(first, "b" * 64)
    second_packet, _, _, second_value = execution_fixture(second, "b" * 64)
    assert first_packet != second_packet
    assert first_value["paths"]["framework_root"] != second_value["paths"]["framework_root"]
    assert first_value["paths"]["global_lock"] == second_value["paths"]["global_lock"]
    assert first_value["paths"]["source_lock"] == second_value["paths"]["source_lock"]


def test_framework_mutation_between_validation_and_import_fails_before_import(tmp_path,
                                                                             monkeypatch):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, packet_value = execution_fixture(tmp_path, operator_sha)
    execution = operator.validate_execution_documents(
        packet, authorization, operator_sha, expected_inputs,
        now=datetime.now(timezone.utc),
    )
    framework_file = Path(packet_value["paths"]["framework_root"]) / "core/base_pipeline.py"
    framework_file.write_text("# mutated after validation\n", encoding="utf-8")
    monkeypatch.setattr(operator.importlib, "import_module",
                        lambda *_: pytest.fail("import reached after mutation"))
    with pytest.raises(operator.PromotionError, match="file_hash_mismatch"):
        operator.load_database(
            Path(packet_value["paths"]["framework_root"]), execution["framework_binding"]
        )


def test_execution_documents_reject_wrong_capacity_scope(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, packet_value = execution_fixture(tmp_path, operator_sha)
    capacity = Path(packet_value["paths"]["capacity_receipt"])
    capacity_value = json.loads(capacity.read_text())
    capacity_value["scope"]["root_set_sha256"] = "f" * 64
    capacity.unlink()
    capacity.with_name(capacity.name + ".sha256").unlink()
    private_json(capacity, capacity_value)
    rebind_packet_and_authorization(
        packet, authorization, packet_value, refresh_review=True
    )
    with pytest.raises(operator.PromotionError, match="capacity_scope_binding_mismatch"):
        operator.validate_execution_documents(
            packet, authorization, operator_sha, expected_inputs,
            now=datetime.now(timezone.utc),
        )


def test_execution_documents_reject_missing_guard_scope(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, packet_value = execution_fixture(tmp_path, operator_sha)
    capacity = Path(packet_value["paths"]["capacity_receipt"])
    capacity_value = json.loads(capacity.read_text())
    capacity_value.pop("scope")
    capacity.unlink()
    capacity.with_name(capacity.name + ".sha256").unlink()
    private_json(capacity, capacity_value)
    rebind_packet_and_authorization(
        packet, authorization, packet_value, refresh_review=True
    )
    with pytest.raises(operator.PromotionError, match="capacity_scope_binding_mismatch"):
        operator.validate_execution_documents(
            packet, authorization, operator_sha, expected_inputs,
            now=datetime.now(timezone.utc),
        )


def test_execution_documents_reject_non_hex_capacity_scope_digest(tmp_path):
    operator_sha = "b" * 64
    packet, authorization, expected_inputs, packet_value = execution_fixture(tmp_path, operator_sha)
    packet_value["capacity_scope"]["root_set_sha256"] = "g" * 64
    rebind_packet_and_authorization(packet, authorization, packet_value)
    with pytest.raises(operator.PromotionError, match="capacity_scope_hash_invalid"):
        operator.validate_execution_documents(
            packet, authorization, operator_sha, expected_inputs,
            now=datetime.now(timezone.utc),
        )


def test_backup_is_create_only_and_hash_pinned(tmp_path):
    verified = verified_inputs()
    db = Database()
    seed_exact(db, verified.candidates, count=2)
    snapshot = operator.capture_snapshot(db, verified.candidates)
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    encoder = lambda row: operator.canonical_json_bytes(row)
    backup = operator.write_backup(attempt, snapshot, encoder)
    assert len(backup["manifest_sha256"]) == 64
    assert (attempt / "prewrite_backup_manifest.private.json.sha256").is_file()
    with pytest.raises(FileExistsError):
        operator.write_backup(attempt, snapshot, encoder)


def test_safe_summary_never_contains_raw_identity_or_local_path():
    summary = operator.safe_summary(verified_inputs(), state="offline_verified_no_write")
    text = json.dumps(summary)
    assert "https://" not in text
    assert str(WORKSPACE) not in text
    assert summary["exact_identity_count"] == 430


def operator_argv(packet: Path, authorization: Path):
    return [
        "--preparation-record", str(PREPARATION_DIR / "promotion_preparation.private.json"),
        "--preparation-audit", str(PREPARATION_DIR / "independent_audit.private.json"),
        "--preparation-seal", str(PREPARATION_DIR / "promotion_preparation_seal.private.json"),
        "--seal-audit", str(PREPARATION_DIR / "seal_independent_audit.private.json"),
        "--identity-manifest", str(PAYLOAD_DIR / "urls_430.jsonl"),
        "--payload-dir", str(PAYLOAD_DIR),
        "--execution-packet", str(packet),
        "--authorization", str(authorization),
        "--execute",
    ]


def test_mocked_execute_creates_complete_immutable_receipt_set(tmp_path, monkeypatch):
    operator_sha = operator.sha256_path(WORKSPACE / "automation/ops/promote_moscow_exact_20261109.py")
    packet, authorization, _, packet_value = execution_fixture(tmp_path, operator_sha)
    db = Database()
    client = Client()
    monkeypatch.setattr(operator, "load_database", lambda *args: (
        client, db, lambda row: operator.canonical_json_bytes(row)
    ))
    assert operator.main(operator_argv(packet, authorization)) == 0
    attempt = Path(packet_value["paths"]["attempt_dir"])
    expected = {
        "attempt_marker.private.json",
        "input_verification.private.json",
        "prewrite_backup_manifest.private.json",
        "prewrite_set_proof.private.json",
        "guarded_write_receipt.private.json",
        "postwrite_identity_set.private.json",
        "postwrite_month_reconciliation.private.json",
        "no_unrelated_mutation.private.json",
        "terminal_receipt.private.json",
    }
    assert expected <= {path.name for path in attempt.iterdir() if path.is_file()}
    assert all((attempt / (name + ".sha256")).is_file() for name in expected)
    terminal = json.loads((attempt / "terminal_receipt.private.json").read_text())
    assert terminal["inserted_articles"] == 430
    assert terminal["month_ledger"] == operator.EXPECTED_MONTH_LEDGER
    assert set(terminal["supporting_receipt_sha256"]) == {
        "attempt_marker", "authorization", "input_verification", "prewrite_backup_manifest",
        "prewrite_set_proof", "guarded_write", "postwrite_identity_set",
        "postwrite_month_reconciliation", "no_unrelated_mutation",
    }
    assert all(len(value) == 64 for value in terminal["supporting_receipt_sha256"].values())
    assert client.closed is True


def test_locks_remain_held_through_terminal_receipt(tmp_path, monkeypatch):
    operator_sha = operator.sha256_path(WORKSPACE / "automation/ops/promote_moscow_exact_20261109.py")
    packet, authorization, _, packet_value = execution_fixture(tmp_path, operator_sha)
    db = Database()
    client = Client()
    monkeypatch.setattr(operator, "load_database", lambda *args: (
        client, db, lambda row: operator.canonical_json_bytes(row)
    ))
    original = operator.create_private_json
    observed = {"held": False}

    def inspect_terminal_lock(path, value):
        if path.name == "terminal_receipt.private.json":
            with ExitStack() as competing:
                with pytest.raises(operator.PromotionError, match="promotion_lock_busy"):
                    operator.acquire_lock(
                        competing, Path(packet_value["paths"]["source_lock"])
                    )
            observed["held"] = True
        return original(path, value)

    monkeypatch.setattr(operator, "create_private_json", inspect_terminal_lock)
    assert operator.main(operator_argv(packet, authorization)) == 0
    assert observed["held"] is True


def test_post_terminal_stdout_failure_creates_no_failure_receipt(tmp_path, monkeypatch):
    operator_sha = operator.sha256_path(WORKSPACE / "automation/ops/promote_moscow_exact_20261109.py")
    packet, authorization, _, packet_value = execution_fixture(tmp_path, operator_sha)
    db = Database()
    client = Client()
    monkeypatch.setattr(operator, "load_database", lambda *args: (
        client, db, lambda row: operator.canonical_json_bytes(row)
    ))
    monkeypatch.setattr(builtins, "print", lambda *args, **kwargs: (_ for _ in ()).throw(
        BrokenPipeError("closed stdout")
    ))
    assert operator.main(operator_argv(packet, authorization)) == 0
    attempt = Path(packet_value["paths"]["attempt_dir"])
    assert (attempt / "terminal_receipt.private.json").is_file()
    assert not (attempt / "failure_receipt.private.json").exists()


def test_mocked_repeat_is_idempotent_zero_article_writes(tmp_path, monkeypatch, capsys):
    operator_sha = operator.sha256_path(WORKSPACE / "automation/ops/promote_moscow_exact_20261109.py")
    packet, authorization, _, packet_value = execution_fixture(tmp_path, operator_sha)
    db = Database()
    seed_exact(db, verified_inputs().candidates)
    client = Client()
    monkeypatch.setattr(operator, "load_database", lambda *args: (
        client, db, lambda row: operator.canonical_json_bytes(row)
    ))
    assert operator.main(operator_argv(packet, authorization)) == 0
    attempt = Path(packet_value["paths"]["attempt_dir"])
    terminal = json.loads((attempt / "terminal_receipt.private.json").read_text())
    assert terminal["inserted_articles"] == 0
    assert terminal["already_applied_exact"] == 430
    assert json.loads(capsys.readouterr().out)["production_write_performed"] is False


def test_safe_summary_reports_index_only_write():
    summary = operator.safe_summary(
        verified_inputs(), state="terminal_success", inserted_articles=0, inserted_indexes=1
    )
    assert summary["production_write_performed"] is True


def test_ambiguous_commit_is_never_reported_as_aborted(tmp_path):
    verified = verified_inputs()
    db = Database()
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    state = {"stage": "database_connected", "committed": False}
    client = Client(session=Session(commit_error=True))
    with pytest.raises(operator.PromotionError,
                       match="transaction_commit_outcome_ambiguous"):
        operator.execute_promotion(
            db, client, verified, attempt,
            lambda row: operator.canonical_json_bytes(row), state,
        )
    assert state["stage"] == "transaction_commit_outcome_ambiguous"
    write_state, manual = operator.failure_write_state(state)
    assert write_state == "transaction_commit_outcome_ambiguous_requires_reconciliation"
    assert manual is True


def test_postcommit_receipt_failure_reports_committed_state(tmp_path, monkeypatch):
    operator_sha = operator.sha256_path(WORKSPACE / "automation/ops/promote_moscow_exact_20261109.py")
    packet, authorization, _, packet_value = execution_fixture(tmp_path, operator_sha)
    db = Database()
    client = Client()
    monkeypatch.setattr(operator, "load_database", lambda *args: (
        client, db, lambda row: operator.canonical_json_bytes(row)
    ))
    def committed_promotion(*args, **kwargs):
        state = args[-1]
        state["committed"] = True
        state["stage"] = "transaction_commit_acknowledged"
        return {
            "terminal": {
                "exact_identity_count": 430,
                "already_applied_exact": 430,
                "inserted_articles": 0,
                "inserted_indexes": 0,
                "identity_set_sha256": operator.EXPECTED_IDENTITY_SET_SHA256,
                "month_ledger": operator.EXPECTED_MONTH_LEDGER,
                "no_unrelated_mutation": True,
            },
            "supporting_receipt_sha256": {
                "prewrite_backup_manifest": "c" * 64,
                "prewrite_set_proof": "d" * 64,
            },
        }

    monkeypatch.setattr(operator, "execute_promotion", committed_promotion)
    original = operator.create_private_json

    def fail_guarded(path, value):
        if path.name == "guarded_write_receipt.private.json":
            raise RuntimeError("simulated receipt failure")
        return original(path, value)

    monkeypatch.setattr(operator, "create_private_json", fail_guarded)
    with pytest.raises(RuntimeError, match="simulated receipt failure"):
        operator.main(operator_argv(packet, authorization))
    failure = json.loads((Path(packet_value["paths"]["attempt_dir"]) /
                          "failure_receipt.private.json").read_text())
    assert failure["production_write_state"] == \
        "transaction_committed_requires_receipt_reconciliation"
    assert failure["manual_reconciliation_required"] is True
