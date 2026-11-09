#!/usr/bin/env python3
"""Fail-closed, source-scoped Moscow Times exact promotion operator.

The default mode is offline verification only.  It verifies the sealed
preparation, exact 430-identity manifest, immutable payload, and monthly
``date_publish`` ledger without importing a database driver or opening a
network connection.

Production execution is intentionally unavailable without a separate,
current-window execution packet and authorization.  When those gates are
present, the operator takes two nonblocking locks, creates an immutable
attempt marker, dynamically loads the production connector, makes a complete
source-scoped backup, and uses a Mongo transaction for insert-only,
idempotent writes plus exact postwrite reconciliation.  It never deletes or
overwrites article rows and never invokes a generic or legacy promoter.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import os
import stat
import sys
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
SOURCE_ID = "mlp:INT:themoscowtimes.com"
SOURCE_DOMAIN = "themoscowtimes.com"
COUNTRY_CODE = "INT"
PUBLIC_SOURCE_KEY = "MLP|themoscowtimes.com"
DATE_FIELD = "date_publish"
RANGE_START = "2026-08-01"
RANGE_END = "2026-09-30"
EXPECTED_COUNT = 430
EXPECTED_IDENTITY_SET_SHA256 = (
    "d6a0d6b131208614c0164f1c314a29321cc93c6aaf29c8571e6842d45eeca20b"
)
EXPECTED_MONTH_LEDGER = {"2026-08": 212, "2026-09": 218}
EXPECTED_SCOPE_HASH = "sha256-base64url:6aopKmfiwS8dCEwku3G76TEJ1AaIxi2uIR_wCdi7GCw"
CANONICAL_LOCK_ROOT = Path("/var/tmp/mlp-mleed-exact-promotion-locks")

PREPARATION_SHA256 = "8ff1304f154086ed043bd2495fe69a5d8a7c5b13df7404c580c123d31163f8a2"
PREPARATION_AUDIT_SHA256 = "1a0cbeee14208130392d9045ad61612bcd371b9d98ab3e65c77068453f6f2f9d"
PREPARATION_SEAL_SHA256 = "15d881df830523f9054b1e18e700405d0585ae8434ff7f428ba4f3f022f1e81a"
PREPARATION_SEAL_AUDIT_SHA256 = (
    "ec41c7b1c1a3fdb943acea4def86bb2941d9404c324827ee84267bc0baf52b8a"
)
MANIFEST_SHA256 = "45296af4c140a64afa315a478d3bd3731e76ce74965832ab7b9bd71c0bfcee97"
RECOMBINATION_RECEIPT_SHA256 = (
    "f6e5fd44d3f51a2effaf01809c2247bed926124d3213fc6066f18f0d9bddc06a"
)
ARTICLES_SHA256 = "e48045a75725239af11a056487f1f0494e1f0bc373c8d49d067d698df461d03b"
IDENTITY_MANIFEST_SHA256 = (
    "76980ebe88e4826b4b94e1d5f106710bfe3ead6eb65d6ce8f67cf9366e54280c"
)
FIELD_CHECKS_SHA256 = (
    "a6c854f58a525efecf0370c48eb19506f2380bc512d7d49011ba705ad4c44b71"
)

ARTICLES_NAME = "articles_430.jsonl"
IDENTITIES_NAME = "urls_430.jsonl"
FIELD_CHECKS_NAME = "field_checks_430.jsonl"
MANIFEST_NAME = "manifest.json"
RECEIPT_NAME = "recombination_receipt.json"
EXACT_FIELDS = ("url", "source_domain", "title", "maintext", "date_publish", "download_via")
ARTICLE_COLLECTIONS = tuple(
    f"{prefix}-2026-{month}"
    for month in range(1, 13)
    for prefix in ("articles", "opinion-articles")
)
SOURCE_SCOPE_COLLECTIONS = (
    "articles-2026-8",
    "opinion-articles-2026-8",
    "articles-2026-9",
    "opinion-articles-2026-9",
)


class PromotionError(RuntimeError):
    """Stable fail-closed error whose message is safe for a private receipt."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PromotionError(code)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        + "\n"
    ).encode("utf-8")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionError("invalid_json") from exc
    require(isinstance(value, dict), "json_object_required")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PromotionError("invalid_jsonl") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PromotionError("invalid_jsonl") from exc
        require(isinstance(row, dict), "jsonl_object_required")
        rows.append(row)
    return rows


def require_regular(path: Path, expected_sha256: str | None = None, *, private: bool = False,
                    require_sidecar: bool = True) -> None:
    require(path.is_file() and not path.is_symlink(), "regular_file_required")
    if private:
        require(stat.S_IMODE(path.stat().st_mode) == 0o600, "private_file_mode_required")
    if expected_sha256 is not None:
        require(sha256_path(path) == expected_sha256, "file_hash_mismatch")
    sidecar = path.with_name(path.name + ".sha256")
    require(not require_sidecar or sidecar.exists(), "sidecar_required")
    if sidecar.exists():
        require(sidecar.is_file() and not sidecar.is_symlink(), "sidecar_regular_file_required")
        if private:
            require(stat.S_IMODE(sidecar.stat().st_mode) == 0o600,
                    "private_sidecar_mode_required")
        tokens = sidecar.read_text(encoding="utf-8").split()
        require(bool(tokens) and tokens[0] == sha256_path(path), "sidecar_hash_mismatch")


def create_private_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    digest = sha256_bytes(payload.encode("utf-8"))
    sidecar = path.with_name(path.name + ".sha256")
    sidecar_payload = f"{digest}  {path.name}\n"
    sidecar_fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(sidecar_fd, "w", encoding="utf-8") as stream:
        stream.write(sidecar_payload)
        stream.flush()
        os.fsync(stream.fileno())
    return digest


def parse_publish_date(value: object) -> datetime:
    require(isinstance(value, str) and bool(value.strip()), "date_publish_required")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PromotionError("date_publish_invalid") from exc
    return parsed


def normalize_datetime(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    require(isinstance(value, str), "date_publish_type_mismatch")
    return parse_publish_date(value).isoformat()


def canonical_url(value: object) -> str:
    require(isinstance(value, str) and bool(value.strip()), "url_required")
    parsed = urlsplit(value.strip())
    require(parsed.scheme.casefold() in {"http", "https"}, "url_scheme_invalid")
    require(bool(parsed.hostname), "url_host_required")
    host = parsed.hostname.casefold()
    port = parsed.port
    if port and not ((parsed.scheme.casefold() == "http" and port == 80) or
                     (parsed.scheme.casefold() == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), host, path, parsed.query, ""))


def canonical_variants(value: str) -> tuple[str, ...]:
    """Return a bounded set of legacy aliases for prewrite index inspection."""
    parsed = urlsplit(value)
    host = parsed.netloc
    path = parsed.path or "/"
    paths = {path}
    if path == "/":
        paths.add("")
    elif path.endswith("/"):
        paths.add(path.rstrip("/"))
    else:
        paths.add(path + "/")
    schemes = {parsed.scheme.casefold(), "http", "https"}
    variants = {
        urlunsplit((scheme, host, candidate_path, parsed.query, ""))
        for scheme in schemes
        for candidate_path in paths
    }
    variants.add(value)
    return tuple(sorted(variants))


def identity_set_digest(urls: Iterable[str]) -> str:
    payload = "".join(f"{url}\n" for url in sorted(urls)).encode("utf-8")
    return sha256_bytes(payload)


@dataclass(frozen=True)
class Candidate:
    url: str
    source_domain: str
    title: str
    maintext: str
    date_publish: datetime
    download_via: str
    collection: str

    @property
    def month(self) -> str:
        return self.date_publish.strftime("%Y-%m")

    def payload(self) -> dict[str, object]:
        return {
            "url": self.url,
            "source_domain": self.source_domain,
            "title": self.title,
            "maintext": self.maintext,
            "date_publish": self.date_publish,
            "download_via": self.download_via,
        }


@dataclass(frozen=True)
class VerifiedInput:
    candidates: tuple[Candidate, ...]
    artifact_hashes: Mapping[str, str]
    identity_set_sha256: str
    month_ledger: Mapping[str, int]


def verify_preparation(preparation: Path, preparation_audit: Path,
                       seal: Path, seal_audit: Path) -> None:
    require_regular(preparation, PREPARATION_SHA256, private=True)
    require_regular(preparation_audit, PREPARATION_AUDIT_SHA256, private=True)
    require_regular(seal, PREPARATION_SEAL_SHA256, private=True)
    require_regular(seal_audit, PREPARATION_SEAL_AUDIT_SHA256, private=True)
    prep = read_json(preparation)
    prep_audit = read_json(preparation_audit)
    sealed = read_json(seal)
    sealed_audit = read_json(seal_audit)
    require(prep.get("source_id") == SOURCE_ID, "preparation_source_mismatch")
    require(prep.get("public_source_key") == PUBLIC_SOURCE_KEY, "preparation_key_mismatch")
    require(prep.get("status") == "sealed_preparation_write_disabled_pending_guarded_operator",
            "preparation_status_mismatch")
    require(prep.get("terminal_accounting", {}).get("production_writes") is False,
            "preparation_writes_not_false")
    evidence = prep.get("input_evidence", {})
    require(evidence.get("manifest_sha256") == MANIFEST_SHA256 and
            evidence.get("recombination_receipt_sha256") == RECOMBINATION_RECEIPT_SHA256,
            "preparation_parent_pin_mismatch")
    prep_artifacts = evidence.get("payload_artifacts", {})
    require(prep_artifacts.get("articles", {}).get("sha256") == ARTICLES_SHA256 and
            prep_artifacts.get("urls", {}).get("sha256") == IDENTITY_MANIFEST_SHA256 and
            prep_artifacts.get("field_checks", {}).get("sha256") == FIELD_CHECKS_SHA256,
            "preparation_payload_pin_mismatch")
    contract = prep.get("count_contract", {})
    require(contract.get("publication_date_field") == DATE_FIELD, "date_field_mismatch")
    require(contract.get("range_start") == RANGE_START and contract.get("range_end") == RANGE_END,
            "count_range_mismatch")
    require(contract.get("scope_hash") == EXPECTED_SCOPE_HASH, "scope_hash_mismatch")
    require(dict(contract.get("verified_payload_month_ledger", [])) == EXPECTED_MONTH_LEDGER,
            "preparation_month_ledger_mismatch")
    require(contract.get("verified_payload_total") == EXPECTED_COUNT,
            "preparation_total_mismatch")
    require(sealed.get("preparation_record", {}).get("sha256") == PREPARATION_SHA256,
            "seal_preparation_mismatch")
    require(sealed.get("independent_audit", {}).get("sha256") == PREPARATION_AUDIT_SHA256,
            "seal_audit_mismatch")
    require(sealed.get("independent_audit", {}).get("result") == "approved",
            "seal_audit_not_approved")
    require(sealed.get("artifact_set", {}).get("exact_identity_count") == EXPECTED_COUNT,
            "seal_identity_count_mismatch")
    require(sealed.get("artifact_set", {}).get("identity_set_sha256") ==
            EXPECTED_IDENTITY_SET_SHA256, "seal_identity_digest_mismatch")
    require(sealed.get("execution_state", {}).get("execution_allowed") is False,
            "sealed_preparation_must_not_authorize")
    require(sealed.get("immutability", {}).get("sealed") is True, "preparation_not_sealed")
    require(prep_audit.get("result") == "approved" and
            prep_audit.get("preparation_sha256") == PREPARATION_SHA256 and
            prep_audit.get("checks_passed") == prep_audit.get("checks_total"),
            "preparation_audit_not_approved")
    require(sealed_audit.get("result") == "approved" and
            sealed_audit.get("seal_sha256") == PREPARATION_SEAL_SHA256 and
            sealed_audit.get("checks_passed") == sealed_audit.get("checks_total"),
            "seal_audit_not_approved")


def verify_static_inputs(preparation: Path, preparation_audit: Path, seal: Path,
                         seal_audit: Path, identity_manifest: Path,
                         payload_dir: Path) -> VerifiedInput:
    verify_preparation(preparation, preparation_audit, seal, seal_audit)
    require(payload_dir.is_dir() and not payload_dir.is_symlink(), "payload_directory_required")
    articles_path = payload_dir / ARTICLES_NAME
    identities_path = identity_manifest
    checks_path = payload_dir / FIELD_CHECKS_NAME
    manifest_path = payload_dir / MANIFEST_NAME
    receipt_path = payload_dir / RECEIPT_NAME
    require(identities_path.resolve() == (payload_dir / IDENTITIES_NAME).resolve(),
            "identity_manifest_path_mismatch")
    pinned = {
        "articles": (articles_path, ARTICLES_SHA256),
        "identities": (identities_path, IDENTITY_MANIFEST_SHA256),
        "field_checks": (checks_path, FIELD_CHECKS_SHA256),
        "manifest": (manifest_path, MANIFEST_SHA256),
        "recombination_receipt": (receipt_path, RECOMBINATION_RECEIPT_SHA256),
    }
    for path, expected in pinned.values():
        require_regular(path, expected)
    manifest = read_json(manifest_path)
    receipt = read_json(receipt_path)
    require(manifest.get("source_id") == SOURCE_ID and
            manifest.get("status") == "exact_430_observation_recombination_verified",
            "manifest_contract_mismatch")
    manifest_artifacts = manifest.get("artifacts", {})
    require(manifest_artifacts.get("articles", {}).get("sha256") == ARTICLES_SHA256 and
            manifest_artifacts.get("urls", {}).get("sha256") == IDENTITY_MANIFEST_SHA256 and
            manifest_artifacts.get("field_checks", {}).get("sha256") == FIELD_CHECKS_SHA256 and
            manifest.get("receipt", {}).get("sha256") == RECOMBINATION_RECEIPT_SHA256,
            "manifest_artifact_pin_mismatch")
    require(receipt.get("source_id") == SOURCE_ID and receipt.get("production_writes") is False,
            "recombination_receipt_mismatch")
    combined_artifacts = receipt.get("combined_artifacts", {})
    require(combined_artifacts.get("articles", {}).get("sha256") == ARTICLES_SHA256 and
            combined_artifacts.get("urls", {}).get("sha256") == IDENTITY_MANIFEST_SHA256 and
            combined_artifacts.get("field_checks", {}).get("sha256") == FIELD_CHECKS_SHA256,
            "receipt_artifact_pin_mismatch")
    require(receipt.get("set_proofs", {}).get("original_url_set_sha256") ==
            EXPECTED_IDENTITY_SET_SHA256, "receipt_identity_digest_mismatch")
    counts = receipt.get("counts", {})
    require(counts.get("combined_articles") == EXPECTED_COUNT and
            counts.get("combined_field_checks") == EXPECTED_COUNT and
            counts.get("preserved_parent_passes") == 425 and
            counts.get("exact_repair_passes") == 5 and
            counts.get("quarantined") == 0 and counts.get("exceptions") == 0,
            "recombination_accounting_mismatch")

    article_rows = read_jsonl(articles_path)
    identity_rows = read_jsonl(identities_path)
    check_rows = read_jsonl(checks_path)
    require(len(article_rows) == len(identity_rows) == len(check_rows) == EXPECTED_COUNT,
            "payload_row_count_mismatch")
    article_urls = [str(row.get("url", "")) for row in article_rows]
    identity_urls = [str(row.get("url", "")) for row in identity_rows]
    check_urls = [str(row.get("url", "")) for row in check_rows]
    require(len(set(article_urls)) == len(set(identity_urls)) == len(set(check_urls)) ==
            EXPECTED_COUNT, "payload_duplicate_identity")
    require(set(article_urls) == set(identity_urls) == set(check_urls),
            "payload_identity_set_mismatch")
    require(identity_set_digest(identity_urls) == EXPECTED_IDENTITY_SET_SHA256,
            "payload_identity_digest_mismatch")
    aliases = [canonical_url(url) for url in identity_urls]
    require(len(set(aliases)) == EXPECTED_COUNT, "payload_canonical_alias_collision")
    checks_by_url = {str(row["url"]): row for row in check_rows}
    candidates: list[Candidate] = []
    months: Counter[str] = Counter()
    for row in article_rows:
        url = str(row.get("url", ""))
        check = checks_by_url[url]
        require(check.get("eligible") is True and check.get("excluded") is not True,
                "field_check_not_eligible")
        require(row.get("source_domain") == SOURCE_DOMAIN and row.get("country_code") == COUNTRY_CODE,
                "payload_source_mismatch")
        require(row.get("collection") in (None, "", "articles"), "unexpected_opinion_route")
        title = row.get("title")
        maintext = row.get("maintext")
        parser = row.get("parser")
        require(isinstance(title, str) and bool(title.strip()), "title_required")
        require(isinstance(maintext, str) and bool(maintext.strip()), "maintext_required")
        require(isinstance(parser, str) and bool(parser.strip()), "parser_required")
        date_publish = parse_publish_date(row.get(DATE_FIELD))
        day = date_publish.date().isoformat()
        require(RANGE_START <= day <= RANGE_END, "date_publish_out_of_scope")
        month = date_publish.strftime("%Y-%m")
        require(month in EXPECTED_MONTH_LEDGER, "unexpected_month")
        months[month] += 1
        candidates.append(Candidate(
            url=url,
            source_domain=SOURCE_DOMAIN,
            title=title,
            maintext=maintext,
            date_publish=date_publish,
            download_via=f"validated:{parser}",
            collection=f"articles-{date_publish.year}-{date_publish.month}",
        ))
    require(dict(months) == EXPECTED_MONTH_LEDGER, "payload_month_ledger_mismatch")
    return VerifiedInput(
        candidates=tuple(sorted(candidates, key=lambda item: item.url)),
        artifact_hashes={name: digest for name, (_, digest) in pinned.items()},
        identity_set_sha256=EXPECTED_IDENTITY_SET_SHA256,
        month_ledger=dict(months),
    )


def normalize_doc(value: Mapping[str, Any]) -> dict[str, object]:
    out = {field: value.get(field) for field in EXACT_FIELDS}
    out[DATE_FIELD] = normalize_datetime(out[DATE_FIELD])
    return out


def expected_doc(candidate: Candidate) -> dict[str, object]:
    value = candidate.payload()
    value[DATE_FIELD] = candidate.date_publish.isoformat()
    return value


def row_digest(rows: Iterable[tuple[str, Mapping[str, Any]]]) -> str:
    normalized = []
    for collection, row in rows:
        normalized.append({
            "collection": collection,
            "document": {key: row[key] for key in sorted(row) if key != "_id"},
        })
    normalized.sort(key=lambda item: canonical_json_bytes(item))
    return sha256_bytes(canonical_json_bytes(normalized))


def find_rows(collection: Any, query: Mapping[str, Any], *, session: Any = None) -> list[dict[str, Any]]:
    kwargs = {} if session is None else {"session": session}
    return [dict(row) for row in collection.find(dict(query), **kwargs)]


def count_rows(collection: Any, query: Mapping[str, Any], *, session: Any = None) -> int:
    kwargs = {} if session is None else {"session": session}
    return int(collection.count_documents(dict(query), **kwargs))


def capture_snapshot(db: Any, candidates: Sequence[Candidate], *, session: Any = None) -> dict[str, Any]:
    urls = [item.url for item in candidates]
    alias_urls = sorted({variant for url in urls for variant in canonical_variants(url)})
    target: dict[str, list[tuple[str, dict[str, Any]]]] = {url: [] for url in urls}
    article_alias_rows: list[tuple[str, dict[str, Any]]] = []
    collection_totals: dict[str, int] = {}
    for name in ARTICLE_COLLECTIONS:
        collection_totals[name] = count_rows(db[name], {}, session=session)
        for row in find_rows(db[name], {"url": {"$in": alias_urls}}, session=session):
            article_alias_rows.append((name, row))
            if row.get("url") in target:
                target[str(row["url"])].append((name, row))
    source_scope: list[tuple[str, dict[str, Any]]] = []
    for name in SOURCE_SCOPE_COLLECTIONS:
        for row in find_rows(db[name], {"source_domain": SOURCE_DOMAIN}, session=session):
            source_scope.append((name, row))
    indexes: dict[str, list[dict[str, Any]]] = {url: [] for url in urls}
    for row in find_rows(db["urls"], {"url": {"$in": urls}}, session=session):
        if row.get("url") in indexes:
            indexes[str(row["url"])].append(row)
    source_index_rows = find_rows(
        db["urls"], {"source_domain": SOURCE_DOMAIN}, session=session
    ) + find_rows(db["urls"], {"url": {"$in": alias_urls}}, session=session)
    source_indexes: list[dict[str, Any]] = []
    seen_source_indexes: set[str] = set()
    for row in source_index_rows:
        digest = sha256_bytes(canonical_json_bytes(row))
        if digest not in seen_source_indexes:
            seen_source_indexes.add(digest)
            source_indexes.append(row)
    source_meta = find_rows(db["source_meta"], {"source_domain": SOURCE_DOMAIN}, session=session)
    return {
        "target": target,
        "article_alias_rows": article_alias_rows,
        "indexes": indexes,
        "source_scope": source_scope,
        "source_indexes": source_indexes,
        "source_meta": source_meta,
        "collection_totals": collection_totals,
        "url_total": count_rows(db["urls"], {}, session=session),
    }


def verify_prewrite(snapshot: Mapping[str, Any], candidates: Sequence[Candidate]) -> dict[str, Any]:
    by_url = {item.url: item for item in candidates}
    aliases = {canonical_url(url): url for url in by_url}
    for _, row in snapshot["article_alias_rows"]:
        url = row.get("url")
        require(isinstance(url, str), "prewrite_article_alias_url_missing")
        alias = canonical_url(url)
        require(alias in aliases and aliases[alias] == url,
                "prewrite_article_canonical_alias_collision")
    for collection, row in snapshot["source_scope"]:
        url = row.get("url")
        require(isinstance(url, str) and url in by_url, "prewrite_unexpected_source_row")
        alias = canonical_url(url)
        require(alias not in aliases or aliases[alias] == url, "database_canonical_alias_collision")
    for row in snapshot["source_indexes"]:
        url = row.get("url")
        require(isinstance(url, str), "prewrite_index_url_missing")
        alias = canonical_url(url)
        require(alias in aliases, "prewrite_unexpected_source_index")
        require(aliases[alias] == url, "index_canonical_alias_collision")

    exact: list[str] = []
    missing_articles: list[str] = []
    missing_indexes: list[str] = []
    for url, candidate in by_url.items():
        articles = snapshot["target"][url]
        indexes = snapshot["indexes"][url]
        require(len(articles) <= 1, "prewrite_duplicate_article")
        require(len(indexes) <= 1, "prewrite_duplicate_index")
        if articles:
            collection, row = articles[0]
            require(collection == candidate.collection, "prewrite_wrong_collection")
            require(row.get("source_domain") == SOURCE_DOMAIN, "prewrite_wrong_source")
            require(normalize_doc(row) == expected_doc(candidate), "prewrite_existing_mismatch")
            exact.append(url)
            if not indexes:
                missing_indexes.append(url)
        else:
            require(not indexes, "prewrite_orphan_index")
            missing_articles.append(url)
            missing_indexes.append(url)
        if indexes:
            require(indexes[0].get("source_domain") in (None, SOURCE_DOMAIN),
                    "prewrite_index_source_mismatch")
    return {
        "already_applied_exact": tuple(sorted(exact)),
        "missing_articles": tuple(sorted(missing_articles)),
        "missing_indexes": tuple(sorted(missing_indexes)),
        "non_target_source_digest": row_digest(
            (collection, row) for collection, row in snapshot["source_scope"]
            if row.get("url") not in by_url
        ),
        "non_target_index_digest": row_digest(
            ("urls", row) for row in snapshot["source_indexes"]
            if row.get("url") not in by_url
        ),
        "source_meta_digest": row_digest(("source_meta", row) for row in snapshot["source_meta"]),
    }


def verify_postwrite(pre: Mapping[str, Any], post: Mapping[str, Any],
                     candidates: Sequence[Candidate], plan: Mapping[str, Any]) -> dict[str, Any]:
    by_url = {item.url: item for item in candidates}
    target_months: Counter[str] = Counter()
    for url, candidate in by_url.items():
        articles = post["target"][url]
        indexes = post["indexes"][url]
        require(len(articles) == 1, "postwrite_article_count_mismatch")
        require(len(indexes) == 1, "postwrite_index_count_mismatch")
        collection, row = articles[0]
        require(collection == candidate.collection, "postwrite_wrong_collection")
        require(normalize_doc(row) == expected_doc(candidate), "postwrite_field_mismatch")
        require(indexes[0].get("source_domain") in (None, SOURCE_DOMAIN),
                "postwrite_index_source_mismatch")
        target_months[candidate.month] += 1
    require(dict(target_months) == EXPECTED_MONTH_LEDGER, "postwrite_month_ledger_mismatch")
    source_rows = post["source_scope"]
    source_urls = [row.get("url") for _, row in source_rows]
    require(len(source_rows) == EXPECTED_COUNT and set(source_urls) == set(by_url) and
            len(set(source_urls)) == EXPECTED_COUNT, "postwrite_source_exact_set_mismatch")
    source_months: Counter[str] = Counter()
    for _, row in source_rows:
        source_months[parse_publish_date(row.get(DATE_FIELD)).strftime("%Y-%m")] += 1
    require(dict(source_months) == EXPECTED_MONTH_LEDGER,
            "postwrite_source_month_ledger_mismatch")
    source_index_urls = [row.get("url") for row in post["source_indexes"]]
    require(len(source_index_urls) == EXPECTED_COUNT and
            set(source_index_urls) == set(by_url) and
            len(set(source_index_urls)) == EXPECTED_COUNT,
            "postwrite_source_index_exact_set_mismatch")
    require(identity_set_digest(by_url) == EXPECTED_IDENTITY_SET_SHA256,
            "postwrite_identity_digest_mismatch")
    post_non_target = row_digest(
        (collection, row) for collection, row in post["source_scope"]
        if row.get("url") not in by_url
    )
    require(post_non_target == plan["non_target_source_digest"],
            "postwrite_unrelated_source_mutation")
    post_non_target_indexes = row_digest(
        ("urls", row) for row in post["source_indexes"] if row.get("url") not in by_url
    )
    require(post_non_target_indexes == plan["non_target_index_digest"],
            "postwrite_unrelated_index_mutation")
    require(row_digest(("source_meta", row) for row in post["source_meta"]) ==
            plan["source_meta_digest"], "postwrite_source_meta_mutation")
    article_insert_count = len(plan["missing_articles"])
    index_insert_count = len(plan["missing_indexes"])
    total_article_delta = sum(
        post["collection_totals"][name] - pre["collection_totals"][name]
        for name in ARTICLE_COLLECTIONS
    )
    require(total_article_delta == article_insert_count, "postwrite_unrelated_collection_delta")
    require(post["url_total"] - pre["url_total"] == index_insert_count,
            "postwrite_unrelated_index_delta")
    require(article_insert_count + len(plan["already_applied_exact"]) == EXPECTED_COUNT,
            "postwrite_terminal_accounting_mismatch")
    return {
        "exact_identity_count": EXPECTED_COUNT,
        "already_applied_exact": len(plan["already_applied_exact"]),
        "inserted_articles": article_insert_count,
        "inserted_indexes": index_insert_count,
        "identity_set_sha256": EXPECTED_IDENTITY_SET_SHA256,
        "month_ledger": dict(source_months),
        "no_unrelated_mutation": True,
    }


def write_backup(attempt: Path, snapshot: Mapping[str, Any],
                 bson_encode: Any) -> dict[str, Any]:
    """Create fsynced, type-preserving BSON backups before any mutation."""
    backup_root = attempt / "prewrite_backup.private"
    backup_root.mkdir(mode=0o700)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for records in snapshot["target"].values():
        for collection, row in records:
            grouped.setdefault(collection, []).append(row)
    grouped["urls"] = [row for rows in snapshot["indexes"].values() for row in rows]
    grouped["source_scope_indexes"] = list(snapshot["source_indexes"])
    grouped["bounded_article_alias_scope"] = [
        {"collection": collection, "document": row}
        for collection, row in snapshot["article_alias_rows"]
    ]
    grouped["source_meta"] = list(snapshot["source_meta"])
    grouped["source_scope_aug_sep"] = [
        {"collection": collection, "document": row}
        for collection, row in snapshot["source_scope"]
    ]
    artifacts: dict[str, Any] = {}
    for name, rows in sorted(grouped.items()):
        path = backup_root / f"{name}.bson"
        payload = b"".join(bson_encode(dict(row)) for row in rows)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        artifacts[name] = {"rows": len(rows), "sha256": sha256_bytes(payload)}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "private_moscow_exact_prewrite_backup_manifest",
        "created_at_utc": now_iso(),
        "source_id": SOURCE_ID,
        "artifacts": artifacts,
    }
    manifest_sha = create_private_json(attempt / "prewrite_backup_manifest.private.json", manifest)
    return {"manifest_sha256": manifest_sha, "artifacts": artifacts}


def insert_missing(db: Any, candidates: Sequence[Candidate], plan: Mapping[str, Any],
                   *, session: Any) -> None:
    by_url = {item.url: item for item in candidates}
    for url in plan["missing_articles"]:
        candidate = by_url[url]
        db[candidate.collection].insert_one(candidate.payload(), session=session)
    for url in plan["missing_indexes"]:
        db["urls"].insert_one({"url": url, "source_domain": SOURCE_DOMAIN}, session=session)


def _packet_path(paths: Mapping[str, Any], name: str) -> Path:
    value = paths.get(name)
    require(isinstance(value, str) and bool(value), "execution_packet_path_missing")
    path = Path(value)
    require(path.is_absolute(), "execution_packet_path_not_absolute")
    return path


def verify_framework_code_proof(proof_path: Path, framework_root: Path,
                                operator_sha256: str) -> dict[str, Any]:
    proof = read_json(proof_path)
    require(proof.get("kind") == "private_moscow_promotion_framework_code_proof" and
            proof.get("result") == "approved" and
            proof.get("database_runtime_imported") is False and
            proof.get("dependency_metadata_without_imports") is True and
            proof.get("dependency_closure_complete") is True and
            proof.get("operator_sha256") == operator_sha256,
            "framework_code_proof_not_approved")
    files = proof.get("files")
    require(isinstance(files, list) and 1 <= len(files) <= 100,
            "framework_code_proof_files_invalid")
    root = framework_root.resolve()
    seen: set[str] = set()
    verified_files: list[dict[str, str]] = []
    for item in files:
        require(isinstance(item, dict) and set(item) >= {"path", "sha256"},
                "framework_code_proof_file_invalid")
        relative = Path(str(item["path"]))
        require(not relative.is_absolute() and ".." not in relative.parts,
                "framework_code_proof_path_invalid")
        path = (root / relative).resolve()
        require(path != root and root in path.parents and str(relative) not in seen,
                "framework_code_proof_path_escape")
        seen.add(str(relative))
        require_regular(path, str(item["sha256"]), require_sidecar=False)
        verified_files.append({"path": str(relative), "sha256": str(item["sha256"])})
    connector = str(proof.get("runtime_connector_path", ""))
    require(connector in seen and connector in {
        "core/base_pipeline.py", "getnewurls_new/core/base_pipeline.py"
    }, "framework_connector_not_pinned")
    package_init = str(Path(connector).parent / "__init__.py")
    if (root / package_init).exists():
        require(package_init in seen, "framework_package_init_not_pinned")
    verified_files.sort(key=lambda item: item["path"])
    require(proof.get("root_identity_sha256") ==
            sha256_bytes(canonical_json_bytes(verified_files)),
            "framework_root_identity_mismatch")
    connector_sha256 = next(
        item["sha256"] for item in verified_files if item["path"] == connector
    )
    return {
        "connector_path": connector,
        "connector_sha256": connector_sha256,
        "verified_files": verified_files,
    }


def validate_execution_documents(packet_path: Path, authorization_path: Path,
                                 operator_sha256: str,
                                 expected_inputs: Mapping[str, Path],
                                 *, now: datetime | None = None) -> dict[str, Any]:
    require_regular(packet_path, private=True)
    require_regular(authorization_path, private=True)
    packet = read_json(packet_path)
    authorization = read_json(authorization_path)
    packet_sha = sha256_path(packet_path)
    authorization_sha = sha256_path(authorization_path)
    require(packet.get("kind") == "private_moscow_exact_promotion_execution_packet",
            "execution_packet_kind_mismatch")
    require(packet.get("source_id") == SOURCE_ID, "execution_packet_source_mismatch")
    require(packet.get("operator_sha256") == operator_sha256,
            "execution_packet_operator_mismatch")
    require(packet.get("preparation_sha256") == PREPARATION_SHA256 and
            packet.get("preparation_seal_sha256") == PREPARATION_SEAL_SHA256,
            "execution_packet_preparation_mismatch")
    require(packet.get("identity_manifest_sha256") == IDENTITY_MANIFEST_SHA256 and
            packet.get("identity_set_sha256") == EXPECTED_IDENTITY_SET_SHA256,
            "execution_packet_identity_mismatch")
    require(packet.get("artifact_sha256") == {
        "articles": ARTICLES_SHA256,
        "field_checks": FIELD_CHECKS_SHA256,
        "manifest": MANIFEST_SHA256,
        "recombination_receipt": RECOMBINATION_RECEIPT_SHA256,
    }, "execution_packet_artifact_mismatch")
    require(packet.get("publication_date_field") == DATE_FIELD and
            packet.get("month_ledger") == EXPECTED_MONTH_LEDGER,
            "execution_packet_count_contract_mismatch")
    require(packet.get("execution_allowed") is True and packet.get("attempt_id"),
            "execution_packet_not_authorized_for_attempt")
    require(isinstance(packet.get("window_id"), str) and packet.get("window_id") and
            isinstance(packet.get("scheduled_window_local"), str) and
            packet.get("scheduled_window_local"), "execution_packet_window_missing")
    paths_value = packet.get("paths")
    require(isinstance(paths_value, dict), "execution_packet_paths_missing")
    paths = {name: _packet_path(paths_value, name) for name in (
        "preparation_record", "preparation_audit", "preparation_seal", "seal_audit",
        "identity_manifest", "payload_dir", "attempt_parent", "attempt_dir",
        "framework_root", "framework_code_proof", "lock_root", "global_lock", "source_lock",
        "capacity_receipt", "capacity_review",
    )}
    for name, expected in expected_inputs.items():
        require(paths[name].resolve() == expected.resolve(), "execution_packet_input_path_mismatch")
    attempt_id = str(packet["attempt_id"])
    require(paths["attempt_dir"].name == attempt_id and
            paths["attempt_dir"].parent.resolve() == paths["attempt_parent"].resolve(),
            "execution_packet_attempt_path_mismatch")
    require(paths["global_lock"].resolve() != paths["source_lock"].resolve(),
            "execution_packet_locks_not_distinct")
    canonical_lock_root = CANONICAL_LOCK_ROOT.resolve()
    require(paths["lock_root"].resolve() == canonical_lock_root and
            paths["lock_root"].is_dir() and not paths["lock_root"].is_symlink() and
            stat.S_IMODE(paths["lock_root"].stat().st_mode) == 0o700 and
            paths["global_lock"].parent.resolve() == paths["lock_root"].resolve() and
            paths["source_lock"].parent.resolve() == paths["lock_root"].resolve() and
            paths["global_lock"].name == "exact-promotion-global.lock" and
            paths["source_lock"].name == "exact-promotion-mlp-int-themoscowtimes-com.lock",
            "execution_packet_lock_paths_invalid")
    require(paths["attempt_parent"].is_dir() and not paths["attempt_parent"].is_symlink() and
            stat.S_IMODE(paths["attempt_parent"].stat().st_mode) == 0o700,
            "attempt_parent_not_private")
    require(paths["framework_root"].is_dir() and not paths["framework_root"].is_symlink(),
            "framework_root_invalid")

    evidence_hashes = packet.get("evidence_sha256")
    require(isinstance(evidence_hashes, dict), "execution_packet_evidence_hashes_missing")
    for name in ("framework_code_proof", "capacity_receipt", "capacity_review"):
        expected_hash = evidence_hashes.get(name)
        require(isinstance(expected_hash, str) and len(expected_hash) == 64,
                "execution_packet_evidence_hash_invalid")
        require_regular(paths[name], expected_hash, private=True)
    framework_binding = verify_framework_code_proof(
        paths["framework_code_proof"], paths["framework_root"], operator_sha256
    )
    capacity = read_json(paths["capacity_receipt"])
    capacity_review = read_json(paths["capacity_review"])
    require(capacity.get("kind") == "host_capacity_guard" and
            capacity.get("status") in {"green", "reduced"} and
            capacity.get("launch_allowed") is True,
            "capacity_receipt_not_launchable")
    metrics = capacity.get("metrics", {})
    limits = capacity.get("recommended_limits", {})
    require(metrics.get("inspected_roots") == 5 and
            metrics.get("configured_roots_total_bytes") == 0 and
            metrics.get("scrape_processes") == 0 and metrics.get("browser_processes") == 0,
            "capacity_receipt_not_quiescent")
    require(int(limits.get("network_workers", 0)) >= 1 and
            int(limits.get("browser_workers", -1)) == 0,
            "capacity_worker_profile_invalid")
    capacity_scope = packet.get("capacity_scope")
    require(isinstance(capacity_scope, dict) and
            capacity_scope.get("root_count") == 5 and
            capacity_scope.get("scraper_pattern_count") == 6 and
            capacity_scope.get("browser_pattern_count") == 6,
            "capacity_scope_counts_invalid")
    for key in ("root_set_sha256", "scraper_pattern_set_sha256",
                "browser_pattern_set_sha256"):
        require(isinstance(capacity_scope.get(key), str) and
                len(str(capacity_scope[key])) == 64,
                "capacity_scope_hash_invalid")
    capacity_scope_sha = sha256_bytes(canonical_json_bytes(capacity_scope))
    require(capacity.get("scope") == capacity_scope and
            capacity_review.get("capacity_scope_sha256") == capacity_scope_sha,
            "capacity_scope_binding_mismatch")
    require(capacity_review.get("result") == "approved" and
            capacity_review.get("guard_receipt_sha256") == evidence_hashes["capacity_receipt"] and
            capacity_review.get("blocker_count") == 0 and
            capacity_review.get("scheduled_window_local") == packet["scheduled_window_local"],
            "capacity_review_not_approved")
    try:
        capacity_generated = datetime.fromisoformat(
            str(capacity["generated_at"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError) as exc:
        raise PromotionError("capacity_timestamp_invalid") from exc
    require(authorization.get("kind") == "private_moscow_exact_promotion_authorization",
            "authorization_kind_mismatch")
    require(authorization.get("approved") is True and
            authorization.get("independent_review_approved") is True,
            "authorization_not_approved")
    require(authorization.get("execution_packet_sha256") == packet_sha and
            authorization.get("operator_sha256") == operator_sha256 and
            authorization.get("attempt_id") == packet.get("attempt_id") and
            authorization.get("window_id") == packet.get("window_id") and
            authorization.get("scheduled_window_local") == packet.get("scheduled_window_local"),
            "authorization_binding_mismatch")
    require(authorization.get("capacity_receipt_sha256") ==
            evidence_hashes["capacity_receipt"] and
            authorization.get("capacity_review_sha256") == evidence_hashes["capacity_review"] and
            authorization.get("framework_code_proof_sha256") ==
            evidence_hashes["framework_code_proof"] and
            authorization.get("capacity_scope_sha256") == capacity_scope_sha,
            "authorization_evidence_binding_mismatch")
    current = now or datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(str(authorization["expires_at_utc"]).replace("Z", "+00:00"))
        not_before = datetime.fromisoformat(str(authorization["not_before_utc"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise PromotionError("authorization_time_invalid") from exc
    require(0 < (expires - not_before).total_seconds() <= 6000,
            "authorization_window_too_wide")
    require(not_before <= current <= expires, "authorization_outside_time_window")
    require(not_before <= capacity_generated <= expires,
            "capacity_outside_authorization_window")
    try:
        scheduled_local = datetime.strptime(
            str(packet["scheduled_window_local"]), "%Y-%m-%d %H:%M"
        ).replace(tzinfo=ZoneInfo("America/New_York"))
    except ValueError as exc:
        raise PromotionError("scheduled_window_invalid") from exc
    scheduled_utc = scheduled_local.astimezone(timezone.utc)
    require(not_before <= scheduled_utc <= expires and
            abs((capacity_generated - scheduled_utc).total_seconds()) <= 6000,
            "capacity_scheduled_window_mismatch")
    require(authorization.get("process_quiescence_verified") is True,
            "authorization_capacity_or_quiescence_missing")
    return {
        "packet": packet,
        "authorization": authorization,
        "packet_sha256": packet_sha,
        "authorization_sha256": authorization_sha,
        "paths": paths,
        "evidence_sha256": evidence_hashes,
        "framework_binding": framework_binding,
        "capacity_scope_sha256": capacity_scope_sha,
    }


def create_attempt_marker(attempt: Path, attempt_id: str, packet_sha256: str) -> str:
    require(not attempt.exists(), "attempt_directory_already_exists")
    attempt.mkdir(parents=True, mode=0o700)
    attempt.chmod(0o700)
    return create_private_json(attempt / "attempt_marker.private.json", {
        "schema_version": SCHEMA_VERSION,
        "kind": "private_moscow_exact_promotion_attempt_marker",
        "created_at_utc": now_iso(),
        "attempt_id": attempt_id,
        "execution_packet_sha256": packet_sha256,
        "source_id": SOURCE_ID,
    })


def acquire_lock(stack: ExitStack, path: Path) -> None:
    require(path.parent.is_dir() and not path.parent.is_symlink() and
            stat.S_IMODE(path.parent.stat().st_mode) == 0o700,
            "lock_parent_not_private")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PromotionError("lock_open_failed") from exc
    descriptor = os.fstat(fd)
    if not stat.S_ISREG(descriptor.st_mode) or descriptor.st_nlink != 1:
        os.close(fd)
        raise PromotionError("lock_not_exclusive_regular_file")
    os.fchmod(fd, 0o600)
    stream = os.fdopen(fd, "r+", encoding="utf-8")
    stack.callback(stream.close)
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise PromotionError("promotion_lock_busy") from exc


def load_database(framework_root: Path,
                  framework_binding: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    """Dynamically import the connector only after every static gate and lock."""
    require(framework_root.is_dir() and not framework_root.is_symlink(),
            "framework_root_invalid")
    connector_path = str(framework_binding.get("connector_path", ""))
    verified_files = framework_binding.get("verified_files")
    require(isinstance(verified_files, list) and verified_files,
            "framework_runtime_binding_missing")
    for item in verified_files:
        relative = Path(str(item["path"]))
        require_regular(framework_root / relative, str(item["sha256"]),
                        require_sidecar=False)
    if connector_path == "getnewurls_new/core/base_pipeline.py":
        code_root = framework_root / "getnewurls_new"
    elif connector_path == "core/base_pipeline.py":
        code_root = framework_root
    else:
        raise PromotionError("framework_connector_not_pinned")
    connector = (framework_root / connector_path).resolve()
    require(connector.is_file() and not connector.is_symlink() and
            sha256_path(connector) == framework_binding.get("connector_sha256"),
            "framework_connector_changed_after_verification")
    require("core.base_pipeline" not in sys.modules,
            "framework_connector_module_preloaded")
    sys.path.insert(0, str(code_root.resolve()))
    importlib.invalidate_caches()
    module = importlib.import_module("core.base_pipeline")
    module_file = Path(str(getattr(module, "__file__", ""))).resolve()
    require(module_file == connector and
            sha256_path(module_file) == framework_binding.get("connector_sha256"),
            "framework_import_binding_mismatch")
    bson_module = importlib.import_module("bson")
    client = module.MongoClient(module.MONGO_URI, serverSelectionTimeoutMS=20000,
                                retryWrites=False)
    client.admin.command("ping")
    return client, client[module.MONGO_DB], bson_module.BSON.encode


def execute_promotion(db: Any, client: Any, verified: VerifiedInput, attempt: Path,
                      bson_encode: Any, transaction_state: dict[str, Any]) -> dict[str, Any]:
    with client.start_session() as session:
        session.start_transaction()
        transaction_state["stage"] = "transaction_started"
        try:
            pre = capture_snapshot(db, verified.candidates, session=session)
            transaction_state["stage"] = "prewrite_snapshot_complete"
            backup = write_backup(attempt, pre, bson_encode)
            plan = verify_prewrite(pre, verified.candidates)
            prewrite_proof_sha = create_private_json(
                attempt / "prewrite_set_proof.private.json", {
                "schema_version": SCHEMA_VERSION,
                "kind": "private_moscow_exact_prewrite_set_proof",
                "created_at_utc": now_iso(),
                "source_id": SOURCE_ID,
                "identity_set_sha256": EXPECTED_IDENTITY_SET_SHA256,
                "already_applied_exact": len(plan["already_applied_exact"]),
                "missing_articles": len(plan["missing_articles"]),
                "missing_indexes": len(plan["missing_indexes"]),
                "backup_manifest_sha256": backup["manifest_sha256"],
            })
            transaction_state["stage"] = "prewrite_evidence_frozen"
            insert_missing(db, verified.candidates, plan, session=session)
            transaction_state["stage"] = "transaction_writes_staged"
            post = capture_snapshot(db, verified.candidates, session=session)
            terminal = verify_postwrite(pre, post, verified.candidates, plan)
            transaction_state["stage"] = "in_transaction_postwrite_verified"
        except BaseException:
            transaction_state["stage"] = "transaction_aborting_before_commit"
            session.abort_transaction()
            transaction_state["stage"] = "transaction_aborted_before_commit"
            raise
        transaction_state["stage"] = "transaction_commit_in_progress"
        try:
            session.commit_transaction()
        except BaseException as exc:
            transaction_state["stage"] = "transaction_commit_outcome_ambiguous"
            raise PromotionError("transaction_commit_outcome_ambiguous") from exc
        transaction_state["committed"] = True
        transaction_state["stage"] = "transaction_commit_acknowledged"
        return {
            "terminal": terminal,
            "supporting_receipt_sha256": {
                "prewrite_backup_manifest": backup["manifest_sha256"],
                "prewrite_set_proof": prewrite_proof_sha,
            },
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--preparation-record", type=Path, required=True)
    result.add_argument("--preparation-audit", type=Path, required=True)
    result.add_argument("--preparation-seal", type=Path, required=True)
    result.add_argument("--seal-audit", type=Path, required=True)
    result.add_argument("--identity-manifest", type=Path, required=True)
    result.add_argument("--payload-dir", type=Path, required=True)
    result.add_argument("--execution-packet", type=Path)
    result.add_argument("--authorization", type=Path)
    result.add_argument("--execute", action="store_true")
    return result


def safe_summary(verified: VerifiedInput, *, state: str,
                 inserted_articles: int = 0, inserted_indexes: int = 0) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "moscow_exact_promotion_operator_summary",
        "generated_at_utc": now_iso(),
        "source_id": SOURCE_ID,
        "state": state,
        "exact_identity_count": len(verified.candidates),
        "publication_date_field": DATE_FIELD,
        "month_ledger": verified.month_ledger,
        "terminal_verification_complete": state == "terminal_success",
        "production_write_performed": inserted_articles > 0 or inserted_indexes > 0,
    }


def failure_write_state(transaction_state: Mapping[str, Any]) -> tuple[str, bool]:
    """Return a conservative, receipt-safe interpretation of transaction state."""
    stage = str(transaction_state.get("stage", "unknown"))
    if stage == "transaction_commit_outcome_ambiguous":
        return "transaction_commit_outcome_ambiguous_requires_reconciliation", True
    if transaction_state.get("committed") is True:
        return "transaction_committed_requires_receipt_reconciliation", True
    return "not_committed_or_transaction_aborted", False


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    operator_sha = sha256_path(Path(__file__))
    verified = verify_static_inputs(
        args.preparation_record, args.preparation_audit,
        args.preparation_seal, args.seal_audit,
        args.identity_manifest, args.payload_dir,
    )
    if not args.execute:
        print(json.dumps(safe_summary(verified, state="offline_verified_no_write"), sort_keys=True))
        return 0

    require(args.execution_packet is not None and args.authorization is not None,
            "execute_arguments_incomplete")
    execution = validate_execution_documents(
        args.execution_packet, args.authorization, operator_sha,
        {
            "preparation_record": args.preparation_record,
            "preparation_audit": args.preparation_audit,
            "preparation_seal": args.preparation_seal,
            "seal_audit": args.seal_audit,
            "identity_manifest": args.identity_manifest,
            "payload_dir": args.payload_dir,
        },
    )
    packet = execution["packet"]
    paths = execution["paths"]
    attempt = paths["attempt_dir"]
    marker_sha = create_attempt_marker(
        attempt, str(packet["attempt_id"]), execution["packet_sha256"]
    )
    stage = "attempt_marker_created"
    transaction_state: dict[str, Any] = {
        "stage": "attempt_marker_created",
        "committed": False,
    }
    terminal_receipt_frozen = False
    output_summary: dict[str, Any] | None = None

    def freeze_failure(exc: BaseException) -> None:
        if terminal_receipt_frozen:
            return
        failure_path = attempt / "failure_receipt.private.json"
        if failure_path.exists():
            return
        code = exc.code if isinstance(exc, PromotionError) else "unexpected_failure"
        write_state, manual = failure_write_state(transaction_state)
        create_private_json(failure_path, {
            "schema_version": SCHEMA_VERSION,
            "kind": "private_moscow_exact_promotion_failure_receipt",
            "failed_at_utc": now_iso(),
            "source_id": SOURCE_ID,
            "attempt_id": packet["attempt_id"],
            "failure_stage": str(transaction_state.get("stage", stage)),
            "failure_code": code,
            "production_write_state": write_state,
            "manual_reconciliation_required": manual,
            "terminal_receipt_present": False,
        })

    try:
        with ExitStack() as stack:
            acquire_lock(stack, paths["global_lock"])
            acquire_lock(stack, paths["source_lock"])
            stage = "locks_acquired"
            transaction_state["stage"] = stage
            try:
                input_sha = create_private_json(
                    attempt / "input_verification.private.json", {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "private_moscow_exact_promotion_input_verification",
                    "created_at_utc": now_iso(),
                    "source_id": SOURCE_ID,
                    "operator_sha256": operator_sha,
                    "execution_packet_sha256": execution["packet_sha256"],
                    "authorization_sha256": execution["authorization_sha256"],
                    "preparation_sha256": PREPARATION_SHA256,
                    "preparation_seal_sha256": PREPARATION_SEAL_SHA256,
                    "identity_manifest_sha256": IDENTITY_MANIFEST_SHA256,
                    "identity_set_sha256": EXPECTED_IDENTITY_SET_SHA256,
                    "exact_identity_count": EXPECTED_COUNT,
                    "month_ledger": EXPECTED_MONTH_LEDGER,
                    "capacity_receipt_sha256": execution["evidence_sha256"]["capacity_receipt"],
                    "capacity_review_sha256": execution["evidence_sha256"]["capacity_review"],
                    "framework_code_proof_sha256":
                        execution["evidence_sha256"]["framework_code_proof"],
                    "capacity_scope_sha256": execution["capacity_scope_sha256"],
                })
                stage = "inputs_verified"
                transaction_state["stage"] = stage
                client, db, bson_encode = load_database(
                    paths["framework_root"], execution["framework_binding"]
                )
                stack.callback(client.close)
                stage = "database_connected"
                transaction_state["stage"] = stage
                promotion = execute_promotion(
                    db, client, verified, attempt, bson_encode, transaction_state
                )
                terminal = promotion["terminal"]
                stage = str(transaction_state["stage"])
                receipt_hashes = {
                    "attempt_marker": marker_sha,
                    "authorization": execution["authorization_sha256"],
                    "input_verification": input_sha,
                    **promotion["supporting_receipt_sha256"],
                }
                receipt_hashes["guarded_write"] = create_private_json(
                    attempt / "guarded_write_receipt.private.json", {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "private_moscow_exact_guarded_write_receipt",
                    "completed_at_utc": now_iso(),
                    "source_id": SOURCE_ID,
                    "attempt_id": packet["attempt_id"],
                    "inserted_articles": terminal["inserted_articles"],
                    "inserted_indexes": terminal["inserted_indexes"],
                    "already_applied_exact": terminal["already_applied_exact"],
                    "idempotent_terminal_equation":
                        "inserted_articles+already_applied_exact=430",
                    "transaction_commit_acknowledged": True,
                    "prewrite_backup_manifest_sha256":
                        receipt_hashes["prewrite_backup_manifest"],
                    "prewrite_set_proof_sha256": receipt_hashes["prewrite_set_proof"],
                })
                receipt_hashes["postwrite_identity_set"] = create_private_json(
                    attempt / "postwrite_identity_set.private.json", {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "private_moscow_exact_postwrite_identity_set",
                    "completed_at_utc": now_iso(),
                    "source_id": SOURCE_ID,
                    "exact_identity_count": EXPECTED_COUNT,
                    "identity_set_sha256": EXPECTED_IDENTITY_SET_SHA256,
                    "duplicates": 0,
                    "missing": 0,
                    "field_mismatches": 0,
                })
                receipt_hashes["postwrite_month_reconciliation"] = create_private_json(
                    attempt / "postwrite_month_reconciliation.private.json", {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "private_moscow_exact_postwrite_month_reconciliation",
                    "completed_at_utc": now_iso(),
                    "source_id": SOURCE_ID,
                    "publication_date_field": DATE_FIELD,
                    "month_ledger": EXPECTED_MONTH_LEDGER,
                    "total": EXPECTED_COUNT,
                    "reconciled": True,
                })
                receipt_hashes["no_unrelated_mutation"] = create_private_json(
                    attempt / "no_unrelated_mutation.private.json", {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "private_moscow_exact_no_unrelated_mutation",
                    "completed_at_utc": now_iso(),
                    "source_id": SOURCE_ID,
                    "non_target_source_unchanged": True,
                    "source_meta_unchanged": True,
                    "collection_delta_exact": True,
                    "url_index_delta_exact": True,
                })
                stage = "postwrite_receipts_frozen"
                transaction_state["stage"] = stage
                terminal_receipt = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "private_moscow_exact_promotion_terminal_receipt",
                    "completed_at_utc": now_iso(),
                    "source_id": SOURCE_ID,
                    "attempt_id": packet["attempt_id"],
                    "execution_packet_sha256": execution["packet_sha256"],
                    "operator_sha256": operator_sha,
                    **terminal,
                    "supporting_receipt_sha256": receipt_hashes,
                    "transaction_commit_acknowledged": True,
                    "status": "terminal_success",
                }
                create_private_json(
                    attempt / "terminal_receipt.private.json", terminal_receipt
                )
                stage = "terminal_receipt_frozen"
                transaction_state["stage"] = stage
                terminal_receipt_frozen = True
                output_summary = safe_summary(
                    verified,
                    state="terminal_success",
                    inserted_articles=int(terminal["inserted_articles"]),
                    inserted_indexes=int(terminal["inserted_indexes"]),
                )
            except BaseException as exc:
                stage = str(transaction_state.get("stage", stage))
                freeze_failure(exc)
                raise
    except BaseException as exc:
        freeze_failure(exc)
        raise

    require(output_summary is not None and terminal_receipt_frozen,
            "terminal_summary_missing")
    try:
        print(json.dumps(output_summary, sort_keys=True))
    except (BrokenPipeError, OSError, ValueError):
        # A closed stdout cannot invalidate already-frozen terminal evidence.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
