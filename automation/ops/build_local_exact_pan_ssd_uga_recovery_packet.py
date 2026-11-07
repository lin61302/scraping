#!/usr/bin/env python3
"""Build the fresh PAN/SSD/UGA exact repair-recovery packet offline.

This helper is construction-only.  It never invokes the capacity guard,
inspects processes, imports deployment modules, contacts a source, performs a
preflight, creates an authorization, or launches a controller.  It reads only
the sealed classification/extraction ledgers and explicit publication proofs.
The predecessor packet and every closed runtime remain unopened and unchanged.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SEQUENCE_ID = "local-exact-pan-ssd-uga-repair-recovery2-20261107-through-20260930"
PACKET_DIRECTORY = "local_exact_pan_ssd_uga_repair_recovery2_20261107_through_20260930"
TARGET_RELATIVE = Path("outputs/operations") / PACKET_DIRECTORY
BUILD_RELATIVE = Path("outputs/operations") / ("." + PACKET_DIRECTORY + ".building")
BINDING_DIRECTORY = "launcher_binding_readiness_20261107"
CLASSIFICATION_RELATIVE = Path(
    "outputs/operations/"
    "local_exact_unstarted4_recovery4_offline_classification_20261028_2215"
)
EXTRACTION_RELATIVE = Path(
    "outputs/operations/"
    "local_exact_unstarted4_recovery4_20261027_through_20260930_"
    "identity_extraction_20261028_1415"
)

CLASSIFICATION_SEAL_SHA256 = "9fed6d2e968eb4d5cc48a4f1dc1135c509c6a2bfc71c3e9b46334eb9f7743c02"
CLASSIFICATION_AUDIT_SHA256 = "90995151c41430f31c8c9ea672fee3836df4a143bb4c04f084a2dcebb55d3926"
CLASSIFICATION_COMPLETION_SHA256 = "0aafad789451a8e6534d20d2aa189af4d26f7947854a333cae443cb1bca053e6"
CLASSIFICATION_REVIEW_SHA256 = "ce9fc2a3c745ca82f7de20d83b4d5c8e2052eea91da1d8b909bba2e6f1184c8c"
PREREQUISITE_STATUS_SHA256 = "e0495bfab54115675110fb7d9142e6b81fc813b8531c6260cbc3ba089fdb450a"
GLOBAL_EXACT_URL_SHA256 = "8574b5103e7b82761f532c5e07fcd8aa03f6297dcf20390a749383ecfddce2af"

PREDECESSOR_PINS = {
    "sequence_id": "local-exact-pan-ssd-uga-repair-20261029-through-20260930",
    "full_integrity_report_sha256": "eb722e093a5664ff580bbb5ed2b7d45f879bc9bc0df329ec77ee92b6a9113d04",
    "independent_packet_audit_sha256": "1b33b8831bdc3710aa73c7b8526ee33d4cd3ce420ef723dc16ff789e3b6e7edb",
    "latest_completion_receipt_sha256": "0a2362a9282241e7abf92fa4d41fe5039dcc6e137074756b54bc398430b7e753",
    "latest_capacity_review_sha256": "d676cc56e456998fa15fb1c88dbbdcc5eddb5784279bffe108d001c139d5080e",
}

CONSUMED_PREDECESSOR_FAILURE_PINS = {
    "sequence_id": "local-exact-pan-ssd-uga-repair-recovery-20261106-through-20260930",
    "full_integrity_report_sha256": "deeab7fdf568deb3d5b65a5844561276ce91ecf68fc0ab9571f437fa74672b01",
    "independent_packet_audit_sha256": "f2d8918eaf8abd14e62fd380987cd4e04bcdd052d7d3ce9e483e8effe963378e",
    "attempt_receipt_sha256": "c129d068433406d225830807c3d3d4f495a1185f261c709cc8baeb729894c640",
    "launch_receipt_sha256": "327cb164e37fb2fc7889bb454484959505b7d78e8830e939bd0fb1ab70328b32",
    "independent_failure_review_sha256": "65db0b1739a068df25dbd707578bdd34585cbe17c7b642b989be21e87c070cdd",
    "sanitized_failure_provenance_commit": "0441e3f8fe12d7ed58471aa2113bf42f2d17f610",
    "sanitized_failure_record_sha256": "aaa27b8c21c65d18285a8bccffdfab462e6984c65756e540938b9aa18620f1ed",
}

PACKET_NETWORK_WORKER_CEILING = 1
BROWSER_CEILING = 0
LOG_MAX_BYTES = 50 * 1024 * 1024
MAX_WALL_SECONDS = 43_200
LANE_TIMEOUT_SECONDS = 10_800
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

SOURCE_SPECS = {
    "PAN": {
        "source_id": "mlp:PAN:newsroompanama.com", "domain": "newsroompanama.com",
        "classified_count": 221,
        "classified_sha256": "035c3e7b217d8e2a202c327d5947db77d81f93d03164caaaa7cd2a668efa89d3",
        "summary_sha256": "0ce3844c290b7a5c5b0029bef83a0b83cb86d9279d9cf4c8c5fe385a489d0f68",
        "preserved_count": 617,
        "preserved_sha256": "42a23d219bd233c8e94b70059c5d68ad7133aac6f56ef336999aeb61ebc8132d",
        "exclusion_count": 210,
        "exclusion_sha256": "347435a3c0a92c15527b0694842975b6f42af9f29892e06ac6c4066bd454cfdf",
        "adapter": "automation/adapters/PAN/newsroompanama.py",
        "start": "2026-07-21", "end": "2026-09-30",
        "monthly_buckets": ["2026-07", "2026-08", "2026-09"],
    },
    "SSD": {
        "source_id": "mlp:SSD:eyeradio.org", "domain": "eyeradio.org",
        "classified_count": 144,
        "classified_sha256": "255abbf58b9559e820855bfeb163900c95633d35184d688c5072fc4e7e7c06ca",
        "summary_sha256": "7e2b4c3aed5c47e3ef8c9c6fc4a64f2d994a870192c470141a0b0d5133496d2b",
        "preserved_count": 395,
        "preserved_sha256": "39c4b02c0f716f6919a660aa3e865774d1e57af03fa740c0b7087a493ecd4475",
        "exclusion_count": 263,
        "exclusion_sha256": "06713e9f9deeff2614fd76e577678712bbbfbee378ea1bf49943a653b9bfb47b",
        "adapter": "automation/adapters/SSD/eyeradio.py",
        "start": "2026-08-10", "end": "2026-09-30",
        "monthly_buckets": ["2026-08", "2026-09"],
    },
    "UGA": {
        "source_id": "mlp:UGA:eagle.co.ug", "domain": "eagle.co.ug",
        "classified_count": 31,
        "classified_sha256": "ca0ef32217b376ad11d5c3f308a83e2c56341044939368769eadd3a56f3e6e99",
        "summary_sha256": "715a9751c7b8680f30d736100df13a578a4ecc7a66475aaad58dce1de45b03cd",
        "preserved_count": 228,
        "preserved_sha256": "2dc013142a06dd7d8e21952e337490c4048455d3884ec3d3b5a8fb15de5220ec",
        "exclusion_count": 73,
        "exclusion_sha256": "bda9c92186fbc5dac445888cd2cc4869785410ec2c677014ef4adf4bcfac7d02",
        "adapter": "automation/adapters/UGA/eagle.py",
        "start": "2026-08-10", "end": "2026-09-30",
        "monthly_buckets": ["2026-08", "2026-09"],
    },
}

LANE_SPECS = (
    {
        "lane_id": "pan-newsroompanama-parser-exact-221", "source_code": "PAN",
        "repair_class": "parser_exact", "expected_count": 221, "selector": "all",
        "exact_url_sha256": "c45c685819fdccf1a0dde817e98ef9efeaa29cc7b31ff52774962deda7ae8b74",
        "derivation_reason": "sealed_source_local_title_and_visible_maintext_parser_repair",
        "targeted_issue_codes_zero": ["adapter_parser_error", "maintext_missing", "maintext_too_short"],
    },
    {
        "lane_id": "ssd-eyeradio-parser-exact-143", "source_code": "SSD",
        "repair_class": "parser_exact", "expected_count": 143, "selector": "not_redirect",
        "exact_url_sha256": "ef3f4a55419e82e5bfd9d5c52bb7929d73d0099e3fa3488d6b49a05b6b5499e9",
        "derivation_reason": "sealed_source_local_title_and_publication_date_parser_repair",
        "targeted_issue_codes_zero": ["adapter_parser_error"],
    },
    {
        "lane_id": "ssd-eyeradio-redirect-exact-1", "source_code": "SSD",
        "repair_class": "redirect_exact", "expected_count": 1, "selector": "redirect_only",
        "exact_url_sha256": "d99891f996f5a6f79dcf32b028b181dedf9a75ee51c702994150bc5465b5186d",
        "derivation_reason": "sealed_single_identity_access_blocked_reviewed_redirect_repair",
        "targeted_issue_codes_zero": ["adapter_parser_error"],
    },
    {
        "lane_id": "uga-eagle-parser-exact-31", "source_code": "UGA",
        "repair_class": "parser_exact", "expected_count": 31, "selector": "all",
        "exact_url_sha256": "7e35508090668c5ab54fb5dcb6872c78e7903b625409326ddedbfa5dd0854296",
        "derivation_reason": "sealed_source_local_title_parser_repair",
        "targeted_issue_codes_zero": ["adapter_parser_error"],
    },
)

RUNTIME_CODE_FILES = (
    "automation/adapters/__init__.py", "automation/adapters/INT/__init__.py",
    "automation/adapters/INT/common.py", "automation/adapters/PAN/__init__.py",
    "automation/adapters/PAN/newsroompanama.py", "automation/adapters/SSD/__init__.py",
    "automation/adapters/SSD/eyeradio.py", "automation/adapters/UGA/__init__.py",
    "automation/adapters/UGA/eagle.py", "automation/adapters/candidate_common.py",
    "automation/ops/host_capacity_guard.py", "automation/ops/launch_local_exact_sequence_once.py",
    "automation/ops/run_pure_discovery_diagnostic.py",
    "automation/ops/unattended_exact_observation_sequence.py",
    "automation/scraper_control/__init__.py", "automation/scraper_control/evidence.py",
    "automation/scraper_control/observe.py", "automation/scraper_control/thesun_five.py",
    "automation/scraper_control/validation.py", "automation/scripts/__init__.py",
    "automation/scripts/parallel_observe_pure.py",
)


class BuildError(RuntimeError):
    """A pin or invariant prevents safe offline construction."""


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_hash(path: Path, expected: str, field: str) -> None:
    if not path.is_file() or path.is_symlink() or sha256_path(path) != expected:
        raise BuildError(f"{field} hash mismatch")


def read_json(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"invalid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise BuildError(f"JSON object required: {path.name}")
    return value


def read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BuildError(f"invalid JSONL: {path.name}") from exc
            if not isinstance(row, dict):
                raise BuildError(f"JSON object row required: {path.name}")
            rows.append(row)
    return rows


def target_url_sha256(urls: Iterable[str]) -> str:
    values = list(dict.fromkeys(value.strip() for value in urls if value.strip()))
    return sha256_bytes("".join(f"{value}\n" for value in values).encode("utf-8"))


def identity_facts(path: Path, source_id: str) -> dict[str, object]:
    rows = read_jsonl(path)
    urls = [str(row.get("url", "")).strip() for row in rows]
    if any(row.get("source_id") != source_id for row in rows) or any(not url for url in urls):
        raise BuildError(f"invalid source identity ledger: {path.name}")
    if len(urls) != len(set(urls)):
        raise BuildError(f"duplicate source identities: {path.name}")
    return {"count": len(urls), "sha256": sha256_path(path),
            "url_set_sha256": target_url_sha256(urls), "urls": urls}


def select_rows(rows: Sequence[dict[str, object]], selector: str) -> list[dict[str, object]]:
    def is_redirect(row: Mapping[str, object]) -> bool:
        value = row.get("offline_classification")
        return isinstance(value, Mapping) and value.get("reason_code") == "unreviewed_redirect_access_blocked"
    if selector == "all":
        return list(rows)
    if selector == "redirect_only":
        return [row for row in rows if is_redirect(row)]
    if selector == "not_redirect":
        return [row for row in rows if not is_redirect(row)]
    raise BuildError("unsupported sealed worklist selector")


def mkdir_private(path: Path) -> None:
    path.mkdir(parents=False, exist_ok=False)
    path.chmod(0o700)


def ensure_private_directory(path: Path, root: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while cursor != root and not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if cursor != root and not cursor.exists():
        raise BuildError("directory creation escaped the build root")
    for directory in reversed(missing):
        directory.mkdir()
        directory.chmod(0o700)


def write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    if path.exists() or path.is_symlink():
        raise BuildError(f"refusing to replace {path.name}")
    path.write_bytes(payload)
    path.chmod(mode)


def write_json(path: Path, value: object, mode: int = 0o600) -> None:
    write_bytes(path, json_bytes(value), mode)


def copy_exact(source: Path, destination: Path, mode: int) -> None:
    if not source.is_file() or source.is_symlink() or destination.exists() or destination.is_symlink():
        raise BuildError(f"unsafe copy: {destination.name}")
    shutil.copyfile(source, destination)
    destination.chmod(mode)
    if sha256_path(source) != sha256_path(destination):
        raise BuildError("copy verification failed")


def add_sidecar(path: Path) -> Path:
    sidecar = path.with_name(path.name + ".sha256")
    write_bytes(sidecar, (sha256_path(path) + "\n").encode("ascii"))
    return sidecar


def argv_sha256(argv: Sequence[str]) -> str:
    return sha256_bytes(canonical_bytes(list(argv)))


def deployment_digest(rows: Sequence[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    for relative, file_hash, size, mode in sorted(
        (str(row["deployment_relative_path"]), str(row["sha256"]),
         int(row["bytes"]), str(row["mode"])) for row in rows
    ):
        for value in (relative, file_hash, str(size), mode):
            digest.update(value.encode("utf-8") + b"\0")
    return digest.hexdigest()


def exact_no_bytecode_prefix(python: Path, launcher: Path) -> list[str]:
    return ["/usr/bin/env", "PYTHONDONTWRITEBYTECODE=1", str(python), "-B", str(launcher)]


def validate_publisher_receipt(
    value: Mapping[str, object], *, commit: str, required_paths: Sequence[str], field: str,
) -> None:
    changed = value.get("changed_paths")
    if (
        value.get("outcome") != "published"
        or value.get("repository") != "lin61302/scraping"
        or value.get("branch") != "main"
        or value.get("commit_sha") != commit
        or value.get("remote_sha") != commit
        or not isinstance(changed, list)
        or any(path not in changed for path in required_paths)
    ):
        raise BuildError(f"{field} does not prove the required published commit")


def validate_runtime_code_proof(
    value: Mapping[str, object], *, commit: str, guard_commit: str,
    guard_receipt_sha256: str, local_hashes: Mapping[str, str],
) -> dict[str, str]:
    pins = value.get("runtime_code")
    if (
        value.get("kind") != "private_single_worker_runtime_code_proof"
        or value.get("status") != "published_reviewed"
        or value.get("repository") != "lin61302/scraping"
        or value.get("branch") != "main"
        or value.get("runtime_commit") != commit
        or value.get("capacity_guard_publication_commit") != guard_commit
        or value.get("capacity_guard_publication_receipt_sha256") != guard_receipt_sha256
        or value.get("packet_network_worker_ceiling") != 1
        or value.get("browser_worker_ceiling") != 0
        or value.get("plan_network_ceiling_supported") is not True
        or value.get("runtime_publication_change_scope") != "launcher_and_focused_test_only"
        or value.get("unchanged_controller_pinned_by_runtime_code_proof") is not True
        or value.get("controller_publication_change_required") is not False
        or not isinstance(pins, Mapping)
        or dict(pins) != dict(local_hashes)
    ):
        raise BuildError("runtime code proof does not match the single-worker runtime")
    required = {
        "capacity_guard_sha256": "automation/ops/host_capacity_guard.py",
        "controller_sha256": "automation/ops/unattended_exact_observation_sequence.py",
        "launcher_sha256": "automation/ops/launch_local_exact_sequence_once.py",
    }
    result: dict[str, str] = {}
    for field, relative in required.items():
        digest = str(value.get(field, ""))
        if not HEX64.fullmatch(digest) or digest != local_hashes[relative]:
            raise BuildError(f"runtime code proof {field} is invalid")
        result[field] = digest
    return result


def validate_independent_review(value: Mapping[str, object], *, commit: str) -> None:
    if (
        value.get("kind") != "private_single_worker_runtime_independent_review"
        or value.get("result") != "approved"
        or value.get("blocker_count") != 0
        or value.get("runtime_commit") != commit
        or value.get("plan_network_ceiling_supported") is not True
        or value.get("packet_network_worker_ceiling") != 1
        or value.get("browser_worker_ceiling") != 0
        or value.get("runtime_publication_change_scope") != "launcher_and_focused_test_only"
        or value.get("unchanged_controller_pinned_by_runtime_code_proof") is not True
        or value.get("controller_publication_change_required") is not False
    ):
        raise BuildError("independent runtime review is not approved")


def validate_builder_review(
    value: Mapping[str, object], *, builder_commit: str, builder_sha256: str,
    test_sha256: str,
) -> None:
    if (
        value.get("kind") != "private_single_worker_packet_builder_independent_review"
        or value.get("result") != "approved"
        or value.get("blocker_count") != 0
        or value.get("builder_commit") != builder_commit
        or value.get("builder_sha256") != builder_sha256
        or value.get("builder_test_sha256") != test_sha256
    ):
        raise BuildError("independent builder review is stale or not approved")


def validate_test_record(
    value: Mapping[str, object], *, commit: str, builder_commit: str,
    builder_sha256: str, test_sha256: str,
) -> None:
    verification = value.get("verification")
    focused = verification.get("single_worker_runtime_and_builder_tests") if isinstance(verification, Mapping) else None
    file_pins = value.get("file_pins")
    if (
        value.get("kind") != "sanitized_immutable_single_worker_recovery_test_provenance"
        or value.get("outcome") != "passed"
        or value.get("runtime_commit") != commit
        or value.get("builder_commit") != builder_commit
        or value.get("runtime_publication_change_scope") != "launcher_and_focused_test_only"
        or value.get("unchanged_controller_pinned_by_runtime_code_proof") is not True
        or value.get("controller_publication_change_required") is not False
        or not isinstance(file_pins, Mapping)
        or file_pins.get("builder_sha256") != builder_sha256
        or file_pins.get("builder_test_sha256") != test_sha256
        or not isinstance(focused, Mapping)
        or int(focused.get("passed", 0)) < 1
        or focused.get("failed") != 0
        or focused.get("errors") != 0
        or verification.get("python_dont_write_bytecode") is not True
    ):
        raise BuildError("focused tests do not prove the single-worker runtime and builder")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--created-at-utc", required=True)
    parser.add_argument("--runtime-commit", required=True)
    parser.add_argument("--runtime-publication-receipt", required=True, type=Path)
    parser.add_argument("--runtime-publication-receipt-sha256", required=True)
    parser.add_argument("--guard-commit", required=True)
    parser.add_argument("--guard-publication-receipt", required=True, type=Path)
    parser.add_argument("--guard-publication-receipt-sha256", required=True)
    parser.add_argument("--runtime-code-proof", required=True, type=Path)
    parser.add_argument("--runtime-code-proof-sha256", required=True)
    parser.add_argument("--builder-commit", required=True)
    parser.add_argument("--builder-publication-receipt", required=True, type=Path)
    parser.add_argument("--builder-publication-receipt-sha256", required=True)
    parser.add_argument("--runtime-independent-review-record", required=True, type=Path)
    parser.add_argument("--runtime-independent-review-record-sha256", required=True)
    parser.add_argument("--builder-independent-review-record", required=True, type=Path)
    parser.add_argument("--builder-independent-review-record-sha256", required=True)
    parser.add_argument("--test-record", required=True, type=Path)
    parser.add_argument("--test-record-sha256", required=True)
    parser.add_argument("--python", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    inferred_workspace = Path(__file__).resolve().parents[2]
    workspace = (args.workspace_root or inferred_workspace).expanduser().resolve()
    if workspace != inferred_workspace or not workspace.is_dir() or workspace.is_symlink():
        raise BuildError("workspace root differs from the builder location")
    if not all(HEX40.fullmatch(value) for value in (
        args.runtime_commit, args.guard_commit, args.builder_commit,
    )):
        raise BuildError("runtime, guard, and builder commits must be lowercase 40-hex")
    try:
        created = dt.datetime.fromisoformat(
            args.created_at_utc[:-1] + "+00:00"
            if args.created_at_utc.endswith("Z") else args.created_at_utc
        )
    except ValueError as exc:
        raise BuildError("created_at_utc must be ISO-8601") from exc
    if not args.created_at_utc.endswith("Z") or created.utcoffset() != dt.timedelta(0):
        raise BuildError("created_at_utc must use UTC Z notation")
    python_path = args.python.expanduser().resolve()
    if not python_path.is_file() or python_path.is_symlink():
        raise BuildError("pinned Python executable is missing or unsafe")

    raw_receipts = {
        "runtime_publication_receipt.private.json": (
            args.runtime_publication_receipt, args.runtime_publication_receipt_sha256,
        ),
        "runtime_code_proof.private.json": (
            args.runtime_code_proof, args.runtime_code_proof_sha256,
        ),
        "guard_publication_receipt.private.json": (
            args.guard_publication_receipt, args.guard_publication_receipt_sha256,
        ),
        "builder_publication_receipt.private.json": (
            args.builder_publication_receipt, args.builder_publication_receipt_sha256,
        ),
        "runtime_independent_review.private.json": (
            args.runtime_independent_review_record,
            args.runtime_independent_review_record_sha256,
        ),
        "builder_independent_review.private.json": (
            args.builder_independent_review_record,
            args.builder_independent_review_record_sha256,
        ),
        "focused_test_record.private.json": (args.test_record, args.test_record_sha256),
    }
    receipts: dict[str, tuple[Path, str]] = {}
    for name, (raw_path, expected) in raw_receipts.items():
        if not HEX64.fullmatch(expected):
            raise BuildError(f"{name} SHA-256 is invalid")
        path = raw_path.expanduser().resolve()
        if workspace not in path.parents:
            raise BuildError(f"{name} must remain inside the workspace")
        require_hash(path, expected, name)
        receipts[name] = (path, expected)

    code_sources = {relative: workspace / relative for relative in RUNTIME_CODE_FILES}
    for relative, path in code_sources.items():
        if not path.is_file() or path.is_symlink():
            raise BuildError(f"runtime code is missing or unsafe: {relative}")
    local_hashes = {relative: sha256_path(path) for relative, path in sorted(code_sources.items())}
    builder_path = Path(__file__).resolve()
    builder_test_path = workspace / "automation/tests/test_build_local_exact_pan_ssd_uga_recovery_packet.py"
    if not builder_test_path.is_file() or builder_test_path.is_symlink():
        raise BuildError("builder focused test file is missing or unsafe")
    builder_sha = sha256_path(builder_path)
    builder_test_sha = sha256_path(builder_test_path)
    runtime_pin_values = validate_runtime_code_proof(
        read_json(receipts["runtime_code_proof.private.json"][0]),
        commit=args.runtime_commit, guard_commit=args.guard_commit,
        guard_receipt_sha256=args.guard_publication_receipt_sha256,
        local_hashes=local_hashes,
    )
    validate_publisher_receipt(
        read_json(receipts["runtime_publication_receipt.private.json"][0]),
        commit=args.runtime_commit,
        required_paths=[
            "automation/ops/launch_local_exact_sequence_once.py",
            "automation/tests/test_launch_local_exact_sequence_once.py",
        ],
        field="runtime publication receipt",
    )
    validate_publisher_receipt(
        read_json(receipts["guard_publication_receipt.private.json"][0]),
        commit=args.guard_commit,
        required_paths=["automation/ops/host_capacity_guard.py"],
        field="capacity guard publication receipt",
    )
    validate_publisher_receipt(
        read_json(receipts["builder_publication_receipt.private.json"][0]),
        commit=args.builder_commit,
        required_paths=[
            "automation/ops/build_local_exact_pan_ssd_uga_recovery_packet.py",
            "automation/tests/test_build_local_exact_pan_ssd_uga_recovery_packet.py",
        ],
        field="builder publication receipt",
    )
    validate_independent_review(
        read_json(receipts["runtime_independent_review.private.json"][0]), commit=args.runtime_commit,
    )
    validate_builder_review(
        read_json(receipts["builder_independent_review.private.json"][0]),
        builder_commit=args.builder_commit, builder_sha256=builder_sha,
        test_sha256=builder_test_sha,
    )
    validate_test_record(
        read_json(receipts["focused_test_record.private.json"][0]),
        commit=args.runtime_commit, builder_commit=args.builder_commit,
        builder_sha256=builder_sha, test_sha256=builder_test_sha,
    )

    target_root = workspace / TARGET_RELATIVE
    build_root = workspace / BUILD_RELATIVE
    if target_root.exists() or target_root.is_symlink():
        raise BuildError("immutable destination already exists")
    if build_root.exists() or build_root.is_symlink():
        raise BuildError("a prior fail-closed construction tree exists")

    classification_root = workspace / CLASSIFICATION_RELATIVE
    extraction_root = workspace / EXTRACTION_RELATIVE
    sealed_artifacts = {
        "classification_seal.private.json": (
            classification_root / "classification_seal.private.json", CLASSIFICATION_SEAL_SHA256,
        ),
        "independent_audit.private.json": (
            classification_root / "independent_audit.private.json", CLASSIFICATION_AUDIT_SHA256,
        ),
        "classification_completion.private.json": (
            classification_root / "classification_completion.private.json", CLASSIFICATION_COMPLETION_SHA256,
        ),
        "classification_review.private.json": (
            classification_root / "classification_review.private.json", CLASSIFICATION_REVIEW_SHA256,
        ),
        "prerequisite_status.private.json": (
            classification_root / "prerequisite_status.private.json", PREREQUISITE_STATUS_SHA256,
        ),
    }
    for name, (path, digest) in sealed_artifacts.items():
        require_hash(path, digest, name)
    completion = read_json(sealed_artifacts["classification_completion.private.json"][0])
    if (
        completion.get("kind") != "private_offline_classification_completion_receipt"
        or completion.get("status") != "completed_independently_approved"
        or completion.get("independent_audit_result") != "approved"
        or completion.get("execution_packet_prepared") is not False
        or completion.get("launch_authorized") is not False
        or completion.get("counts") != {
            "classified_exact_identities": 396, "outcome_unresolved": 396,
            "pan_candidates": 221, "preserved_passes": 1240,
            "source_local_repair_candidates": 396, "ssd_candidates": 144,
            "uga_candidates": 31, "verified_exclusions": 546,
        }
    ):
        raise BuildError("sealed classification completion semantics changed")

    source_inputs: dict[str, dict[str, object]] = {}
    global_urls: list[str] = []
    for code, spec in SOURCE_SPECS.items():
        lower = code.lower()
        classified = classification_root / lower / "classified_unresolved.private.ndjson"
        summary = classification_root / lower / "classification_summary.private.json"
        preserved = extraction_root / lower / "preserved_passes.private.ndjson"
        exclusions = extraction_root / lower / "verified_exclusions.private.ndjson"
        for path, digest, field in (
            (classified, spec["classified_sha256"], "classified ledger"),
            (summary, spec["summary_sha256"], "classification summary"),
            (preserved, spec["preserved_sha256"], "preserved passes"),
            (exclusions, spec["exclusion_sha256"], "verified exclusions"),
        ):
            require_hash(path, str(digest), f"{code} {field}")
        classified_facts = identity_facts(classified, str(spec["source_id"]))
        preserved_facts = identity_facts(preserved, str(spec["source_id"]))
        exclusion_facts = identity_facts(exclusions, str(spec["source_id"]))
        if (
            classified_facts["count"] != spec["classified_count"]
            or preserved_facts["count"] != spec["preserved_count"]
            or exclusion_facts["count"] != spec["exclusion_count"]
        ):
            raise BuildError(f"{code} sealed identity counts changed")
        partitions = [set(classified_facts["urls"]), set(preserved_facts["urls"]),
                      set(exclusion_facts["urls"])]
        if partitions[0] & partitions[1] or partitions[0] & partitions[2] or partitions[1] & partitions[2]:
            raise BuildError(f"{code} sealed identity partitions overlap")
        summary_value = read_json(summary)
        if summary_value.get("source_id") != spec["source_id"] or summary_value.get("classified_count") != spec["classified_count"]:
            raise BuildError(f"{code} classification summary changed")
        source_inputs[code] = {
            "classified": classified, "summary": summary, "preserved": preserved,
            "exclusions": exclusions, "classified_rows": read_jsonl(classified),
            "classified_facts": classified_facts, "preserved_facts": preserved_facts,
            "exclusion_facts": exclusion_facts,
        }
        global_urls.extend(str(value) for value in classified_facts["urls"])
    if len(global_urls) != 396 or len(set(global_urls)) != 396 or target_url_sha256(global_urls) != GLOBAL_EXACT_URL_SHA256:
        raise BuildError("sealed 396-identity union changed")

    build_root.parent.mkdir(parents=True, exist_ok=True)
    mkdir_private(build_root)
    for relative in (
        "source_material.private", "source_material.private/private_inputs",
        "source_material.private/private_handoff", "source_material.private/private_gate",
        "deployments", "runtime.private", "runtime.private/attempts",
        "runtime.private/locks", "runtime.private/logs", "runtime.private/outputs",
        "runtime.private/receipts", BINDING_DIRECTORY,
    ):
        path = build_root / relative
        ensure_private_directory(path.parent, build_root)
        mkdir_private(path)

    generated: dict[str, tuple[Path, int]] = {}
    for name, (source, expected) in receipts.items():
        destination = build_root / "source_material.private/private_gate" / name
        copy_exact(source, destination, 0o600)
        require_hash(destination, expected, name)
        generated[f"private_gate/{name}"] = (destination, 0o600)

    lane_material: dict[str, dict[str, object]] = {}
    for lane in LANE_SPECS:
        code = str(lane["source_code"])
        source = source_inputs[code]
        selected = select_rows(source["classified_rows"], str(lane["selector"]))
        if len(selected) != lane["expected_count"]:
            raise BuildError(f"{lane['lane_id']} sealed worklist count changed")
        urls = [str(row.get("url", "")).strip() for row in selected]
        if (
            any(not value for value in urls) or len(urls) != len(set(urls))
            or target_url_sha256(urls) != lane["exact_url_sha256"]
        ):
            raise BuildError(f"{lane['lane_id']} exact identity pin changed")
        worklist_name = f"{lane['lane_id']}.private.txt"
        handoff_name = f"{lane['lane_id']}.handoff.private.json"
        worklist = build_root / "source_material.private/private_inputs" / worklist_name
        write_bytes(worklist, "".join(f"{url}\n" for url in urls).encode("utf-8"))
        worklist_facts = {
            "count": len(urls), "sha256": sha256_path(worklist),
            "url_set_sha256": target_url_sha256(urls),
        }
        spec = SOURCE_SPECS[code]
        classified_facts = source["classified_facts"]
        preserved_facts = source["preserved_facts"]
        exclusion_facts = source["exclusion_facts"]
        handoff = {
            "schema_version": 1, "kind": "sealed_classification_exact_handoff",
            "status": "sealed", "sequence_id": SEQUENCE_ID, "lane_id": lane["lane_id"],
            "source_id": spec["source_id"], "source_name": spec["domain"],
            "repair_class": lane["repair_class"], "derivation_reason": lane["derivation_reason"],
            "worklist": worklist_facts,
            "classification_seal_sha256": CLASSIFICATION_SEAL_SHA256,
            "independent_audit_sha256": CLASSIFICATION_AUDIT_SHA256,
            "source_summary_sha256": spec["summary_sha256"],
            "classified_source_ledger": {
                key: classified_facts[key] for key in ("count", "sha256", "url_set_sha256")
            },
            "preserved_passes": {
                key: preserved_facts[key] for key in ("count", "sha256", "url_set_sha256")
            },
            "verified_exclusions": {
                key: exclusion_facts[key] for key in ("count", "sha256", "url_set_sha256")
            },
            "production_writes": False, "rediscovery": False,
        }
        handoff_path = build_root / "source_material.private/private_handoff" / handoff_name
        write_json(handoff_path, handoff)
        generated[f"private_inputs/{worklist_name}"] = (worklist, 0o600)
        generated[f"private_handoff/{handoff_name}"] = (handoff_path, 0o600)
        lane_material[str(lane["lane_id"])] = {
            "urls": urls, "worklist_name": worklist_name, "worklist_facts": worklist_facts,
            "handoff_name": handoff_name, "handoff_sha256": sha256_path(handoff_path),
        }

    redirect_url = str(lane_material["ssd-eyeradio-redirect-exact-1"]["urls"][0])
    redirect_identity_hash = sha256_bytes(redirect_url.encode("utf-8"))
    if redirect_identity_hash not in code_sources["automation/adapters/SSD/eyeradio.py"].read_text(encoding="utf-8"):
        raise BuildError("SSD redirect identity is absent from the published adapter allowlist")

    mapping: dict[str, tuple[Path, int]] = {
        relative: (source, 0o644) for relative, source in code_sources.items()
    }
    mapping.update(generated)
    for name, (source, _digest) in sealed_artifacts.items():
        mapping[f"private_handoff/{name}"] = (source, 0o600)
    for code, source in source_inputs.items():
        lower = code.lower()
        mapping[f"private_handoff/{lower}/classification_summary.private.json"] = (source["summary"], 0o600)
        mapping[f"private_handoff/{lower}/classified_source_ledger.private.ndjson"] = (source["classified"], 0o600)
        mapping[f"private_handoff/{lower}/preserved_passes.private.ndjson"] = (source["preserved"], 0o600)
        mapping[f"private_handoff/{lower}/verified_exclusions.private.ndjson"] = (source["exclusions"], 0o600)

    execution_files = [
        {"path": relative, "sha256": sha256_path(source)}
        for relative, (source, _mode) in sorted(mapping.items())
        if relative.startswith("automation/")
    ]
    for name in receipts:
        source = generated[f"private_gate/{name}"][0]
        execution_files.append({"path": f"private_gate/{name}", "sha256": sha256_path(source)})

    lanes: list[dict[str, object]] = []
    redirect_sets: list[dict[str, object]] = []
    for lane in LANE_SPECS:
        code = str(lane["source_code"])
        lower = code.lower()
        spec = SOURCE_SPECS[code]
        material = lane_material[str(lane["lane_id"])]
        value = {
            "lane_id": lane["lane_id"], "kind": "exact_urls", "source_id": spec["source_id"],
            "adapter": {"path": spec["adapter"], "sha256": sha256_path(workspace / str(spec["adapter"]))},
            "start": spec["start"], "end": spec["end"], "workers": 1,
            "timeout_seconds": LANE_TIMEOUT_SECONDS,
            "exact_input": {
                "path": f"private_inputs/{material['worklist_name']}",
                "sha256": material["worklist_facts"]["sha256"],
            },
            "exact_input_count": lane["expected_count"],
            "sealed_classification_parent": {
                "handoff_manifest": {
                    "path": f"private_handoff/{material['handoff_name']}",
                    "sha256": material["handoff_sha256"],
                },
                "worklist": {
                    "path": f"private_inputs/{material['worklist_name']}",
                    "sha256": material["worklist_facts"]["sha256"],
                },
                "classification_seal": {"path": "private_handoff/classification_seal.private.json", "sha256": CLASSIFICATION_SEAL_SHA256},
                "independent_audit": {"path": "private_handoff/independent_audit.private.json", "sha256": CLASSIFICATION_AUDIT_SHA256},
                "source_summary": {"path": f"private_handoff/{lower}/classification_summary.private.json", "sha256": spec["summary_sha256"]},
                "classified_source_ledger": {"path": f"private_handoff/{lower}/classified_source_ledger.private.ndjson", "sha256": spec["classified_sha256"]},
                "preserved_passes": {"path": f"private_handoff/{lower}/preserved_passes.private.ndjson", "sha256": spec["preserved_sha256"]},
                "verified_exclusions": {"path": f"private_handoff/{lower}/verified_exclusions.private.ndjson", "sha256": spec["exclusion_sha256"]},
            },
            "derivation_reason": lane["derivation_reason"], "repair_class": lane["repair_class"],
            "rediscovery": False, "production_writes": False,
            "acceptance": {
                "manifest_status": "observed", "exceptions_max": 0,
                "failure_class_max": {"unreviewed_redirect": 0},
                "targeted_issue_codes_zero": lane["targeted_issue_codes_zero"],
                "allowed_unrelated_issue_codes": ["date_out_of_range"],
            },
        }
        lanes.append(value)
        if lane["repair_class"] == "redirect_exact":
            redirect_sets.append({"source_id": spec["source_id"], "count": lane["expected_count"],
                                  "sha256": material["worklist_facts"]["sha256"]})

    plan = {
        "schema_version": 1, "mode": "unattended_exact_observation_sequence",
        "sequence_id": SEQUENCE_ID, "production_writes": False,
        "continue_after_isolated_lane_failure": True,
        "capacity": {
            "network_ceiling": 1, "browser_ceiling": 0,
            "log_max_bytes": LOG_MAX_BYTES, "max_wall_seconds": MAX_WALL_SECONDS,
        },
        "execution_files": execution_files,
        "provenance": {
            "repository": "lin61302/scraping", "branch": "main",
            "code_git_commit": args.runtime_commit,
            "runtime_publication_receipt_sha256": args.runtime_publication_receipt_sha256,
            "capacity_guard_publication_commit": args.guard_commit,
            "capacity_guard_publication_receipt_sha256": args.guard_publication_receipt_sha256,
            "runtime_code_proof_sha256": args.runtime_code_proof_sha256,
            "builder_commit": args.builder_commit,
            "builder_publication_receipt_sha256": args.builder_publication_receipt_sha256,
            "test_record_sha256": args.test_record_sha256,
            "runtime_independent_review_record_sha256": args.runtime_independent_review_record_sha256,
            "builder_independent_review_record_sha256": args.builder_independent_review_record_sha256,
        },
        "recovery_lineage": {
            "predecessor": PREDECESSOR_PINS, "predecessor_opened_or_modified": False,
            "consumed_predecessor_failure": CONSUMED_PREDECESSOR_FAILURE_PINS,
            "consumed_predecessor_packet_opened_or_modified": False,
            "consumed_predecessor_runtime_read": False,
            "consumed_predecessor_retry_resume_or_relaunch": False,
            "sealed_offline_classification_sha256": CLASSIFICATION_SEAL_SHA256,
            "sealed_offline_classification_audit_sha256": CLASSIFICATION_AUDIT_SHA256,
            "candidate_count": 396, "preserved_pass_count": 1240,
            "verified_exclusion_count": 546, "rediscovery": False,
            "closed_runtime_read": False,
            "excluded_sources": ["SLB", "ECU", "PER", "COD", "LBR", "MWI"],
        },
        "phases": [{
            "phase_id": "phase-a-sealed-source-local-exact-repairs", "max_active_lanes": 1,
            "production_writes": False, "requires_reviewed_publication": True,
            "publication_gate": {
                "status": "reviewed_published", "repository": "lin61302/scraping",
                "branch": "main", "commit": args.runtime_commit,
                "review_receipt": {"path": "private_gate/runtime_independent_review.private.json", "sha256": args.runtime_independent_review_record_sha256},
                "test_receipt": {"path": "private_gate/focused_test_record.private.json", "sha256": args.test_record_sha256},
                "redirect_exact_sets": redirect_sets,
            },
            "lanes": lanes,
        }],
    }
    plan_source = build_root / "source_material.private/exact_observation_sequence.private.json"
    write_json(plan_source, plan)
    plan_sha = sha256_path(plan_source)
    mapping["exact_observation_sequence.private.json"] = (plan_source, 0o600)

    staging = build_root / "deployments/.staging"
    mkdir_private(staging)
    deployment_rows: list[dict[str, object]] = []
    for relative, (source, mode) in sorted(mapping.items()):
        destination = staging / relative
        ensure_private_directory(destination.parent, staging)
        copy_exact(source, destination, mode)
        if source == build_root or build_root in source.parents:
            future_source = target_root / source.relative_to(build_root)
        else:
            future_source = source
        try:
            source_relative = future_source.relative_to(workspace).as_posix()
        except ValueError as exc:
            raise BuildError("deployment source escaped the workspace") from exc
        deployment_rows.append({
            "source_path": source_relative, "deployment_relative_path": relative,
            "deployed_path": str(target_root / "deployments" / "PENDING_DIGEST" / relative),
            "sha256": sha256_path(destination), "bytes": destination.stat().st_size,
            "mode": f"{stat.S_IMODE(destination.stat().st_mode):04o}",
            "private": mode & 0o077 == 0,
        })
    deployment_sha = deployment_digest(deployment_rows)
    for row in deployment_rows:
        relative = str(row["deployment_relative_path"])
        row["deployed_path"] = str(target_root / "deployments" / deployment_sha / relative)
    deployment_build = build_root / "deployments" / deployment_sha
    os.replace(staging, deployment_build)

    target_deployment = target_root / "deployments" / deployment_sha
    target_runtime = target_root / "runtime.private"
    plan_final = target_deployment / "exact_observation_sequence.private.json"
    manifest_final = target_root / "local_deployment_manifest.private.json"
    controller_hash = runtime_pin_values["controller_sha256"]
    launcher_hash = runtime_pin_values["launcher_sha256"]
    guard_hash = runtime_pin_values["capacity_guard_sha256"]
    manifest = {
        "schema_version": 1, "kind": "private_local_exact_sequence_fallback_deployment",
        "mode": "content_addressed_local_exact_sequence_deployment", "sequence_id": SEQUENCE_ID,
        "host_class": "local_darwin", "transport": "local", "production_writes": False,
        "repository": "lin61302/scraping", "branch": "main",
        "code_git_commit": args.runtime_commit, "network_ceiling": 1, "browser_ceiling": 0,
        "deployment_sha256": deployment_sha, "controller_sha256": controller_hash,
        "plan_sha256": plan_sha, "deployment_root_local": str(target_deployment),
        "automation_root_local": str(target_deployment / "automation"),
        "controller_local": str(target_deployment / "automation/ops/unattended_exact_observation_sequence.py"),
        "plan_local": str(plan_final), "python_local": str(python_path),
        "output_root_local": str(target_runtime / "outputs"),
        "receipt_root_local": str(target_runtime / "receipts"),
        "log_root_local": str(target_runtime / "logs"),
        "lock_root_local": str(target_runtime / "locks"),
        "attempt_root_local": str(target_runtime / "attempts"),
        "host_lock_local": str(target_runtime / "locks/controller.lock"),
        "attempt_marker_local": str(target_runtime / "attempts/attempt_receipt.json"),
        "launch_receipt_local": str(target_runtime / "attempts/launch_receipt.json"),
        "controller_log_local": str(target_runtime / "logs/controller.log"),
        "allowed_parent_roots_local": [str(classification_root)], "files": deployment_rows,
    }
    manifest_path = build_root / "local_deployment_manifest.private.json"
    write_json(manifest_path, manifest)
    manifest_sha = sha256_path(manifest_path)

    identity_path = build_root / "exact_identity_proof.private.json"
    write_json(identity_path, {
        "schema_version": 1, "kind": "private_exact_identity_proof", "sequence_id": SEQUENCE_ID,
        "global_count": 396, "global_url_set_sha256": GLOBAL_EXACT_URL_SHA256, "unique": True,
        "sources": {
            code: {"classified_count": spec["classified_count"],
                   "preserved_pass_count": spec["preserved_count"],
                   "verified_exclusion_count": spec["exclusion_count"]}
            for code, spec in SOURCE_SPECS.items()
        },
        "lanes": {str(lane["lane_id"]): {"count": lane["expected_count"],
                                           "sha256": lane["exact_url_sha256"]}
                  for lane in LANE_SPECS},
        "rediscovery": False,
    })
    predecessor_path = build_root / "predecessor_preservation.private.json"
    write_json(predecessor_path, {
        "schema_version": 1, "kind": "private_predecessor_preservation_proof",
        "sequence_id": SEQUENCE_ID, "predecessor": PREDECESSOR_PINS,
        "predecessor_packet_opened": False, "predecessor_runtime_read": False,
        "predecessor_modified": False, "predecessor_resume_supported": False,
    })
    consumed_predecessor_path = build_root / "consumed_predecessor_failure_preservation.private.json"
    write_json(consumed_predecessor_path, {
        "schema_version": 1,
        "kind": "private_consumed_predecessor_failure_preservation_proof",
        "sequence_id": SEQUENCE_ID,
        "consumed_predecessor": CONSUMED_PREDECESSOR_FAILURE_PINS,
        "sole_initial_attempt_consumed": True,
        "permanently_closed": True,
        "retry_supported": False,
        "resume_supported": False,
        "reuse_supported": False,
        "relaunch_supported": False,
        "consumed_predecessor_packet_opened": False,
        "consumed_predecessor_runtime_read": False,
        "consumed_predecessor_modified": False,
        "source_or_network_work_proven": False,
        "production_writes": False,
    })
    parent_path = build_root / "sealed_parent_proof.private.json"
    write_json(parent_path, {
        "schema_version": 1, "kind": "private_sealed_classification_parent_proof",
        "sequence_id": SEQUENCE_ID, "classification_seal_sha256": CLASSIFICATION_SEAL_SHA256,
        "independent_audit_sha256": CLASSIFICATION_AUDIT_SHA256,
        "classification_completion_sha256": CLASSIFICATION_COMPLETION_SHA256,
        "global_candidate_count": 396, "preserved_pass_count": 1240,
        "verified_exclusion_count": 546, "closed_runtime_read": False,
        "sources": {
            code: {"source_id": spec["source_id"], "classified_count": spec["classified_count"],
                   "classified_sha256": spec["classified_sha256"],
                   "preserved_count": spec["preserved_count"],
                   "preserved_sha256": spec["preserved_sha256"],
                   "exclusion_count": spec["exclusion_count"],
                   "exclusion_sha256": spec["exclusion_sha256"]}
            for code, spec in SOURCE_SPECS.items()
        },
    })

    count_sources = []
    for code, spec in SOURCE_SPECS.items():
        scope = {
            "source_id": spec["source_id"], "publication_date_field": "date_publish",
            "start": spec["start"], "end": spec["end"],
            "candidate_count": spec["classified_count"],
            "preserved_pass_count": spec["preserved_count"],
            "verified_exclusion_count": spec["exclusion_count"],
            "monthly_buckets": spec["monthly_buckets"],
        }
        count_sources.append({
            **scope, "scope_sha256": sha256_bytes(canonical_bytes(scope)),
            "monthly_buckets": [{"month": month, "scraped_promoted": None, "tcg_complete": None}
                                for month in spec["monthly_buckets"]],
            "scraped_promoted_status": "pending_guarded_production_promotion_terminal_receipt",
            "tcg_complete_status": "pending_tcg_terminal_receipt",
            "missing_history_must_remain_pending_not_zero": True,
        })
    count_path = build_root / "source_monthly_count_tcg_handoff.private.json"
    write_json(count_path, {
        "schema_version": 1, "kind": "private_source_monthly_scraped_promoted_tcg_count_handoff",
        "sequence_id": SEQUENCE_ID, "generated_at_utc": args.created_at_utc,
        "publication_date_field": "date_publish", "series_order": ["scraped_promoted", "tcg_complete"],
        "same_scope_and_bucket_contract": True, "missing_history_must_remain_pending_not_zero": True,
        "zero_buckets_must_be_explicit_after_evidence_exists": True,
        "raw_identities_or_urls_public": False, "sources": count_sources,
        "tcg_handoff": {
            "exact_source_scope_hash_required": True, "scraping_terminal_receipt_required": True,
            "production_promotion_receipt_required": True, "tcg_terminal_receipt_required": True,
            "public_plot_and_safe_data_publish_after_automatic_checks": True,
            "routine_user_checkpoint_required": False,
        },
    })

    publication_path = build_root / "publication_pins.private.json"
    write_json(publication_path, {
        "schema_version": 1, "kind": "private_single_worker_recovery_publication_pins",
        "sequence_id": SEQUENCE_ID, "repository": "lin61302/scraping", "branch": "main",
        "runtime_commit": args.runtime_commit, "capacity_guard_publication_commit": args.guard_commit,
        "builder_commit": args.builder_commit,
        "capacity_guard_sha256": guard_hash, "controller_sha256": controller_hash,
        "launcher_sha256": launcher_hash, "runtime_code": local_hashes,
        "receipts": {name: digest for name, (_path, digest) in receipts.items()},
        "production_writes": False,
    })

    deployment_list_path = build_root / "deployment_files.private.ndjson"
    write_bytes(deployment_list_path, b"".join(canonical_bytes(row) for row in deployment_rows))
    digest_rows_path = build_root / "deployment_digest_rows.private.tsv"
    lines = ["relative_path\tsha256\tbytes\tmode\n"]
    for row in sorted(deployment_rows, key=lambda item: str(item["deployment_relative_path"])):
        lines.append(f"{row['deployment_relative_path']}\t{row['sha256']}\t{row['bytes']}\t{row['mode']}\n")
    write_bytes(digest_rows_path, "".join(lines).encode("utf-8"))
    deployment_sha_path = build_root / "deployment_sha256.private.txt"
    write_bytes(deployment_sha_path, (deployment_sha + "\n").encode("ascii"))

    deployment_directories = sorted([deployment_build] + [path for path in deployment_build.rglob("*") if path.is_dir()])
    deployment_modes_path = build_root / "deployment_directory_modes.private.json"
    write_json(deployment_modes_path, {
        "schema_version": 1, "kind": "private_deployment_directory_mode_proof",
        "sequence_id": SEQUENCE_ID, "deployment_sha256": deployment_sha,
        "all_mode_0700": all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in deployment_directories),
        "directories": [{"relative_path": "." if path == deployment_build else path.relative_to(deployment_build).as_posix(),
                         "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}"}
                        for path in deployment_directories],
    })

    launcher = target_deployment / "automation/ops/launch_local_exact_sequence_once.py"
    launch_argv = exact_no_bytecode_prefix(python_path, launcher) + [
        "--plan", str(plan_final), "--plan-sha256", plan_sha,
        "--deployment-manifest", str(manifest_final),
        "--deployment-manifest-sha256", manifest_sha,
        "--deployment-sha256", deployment_sha, "--controller-sha256", controller_hash,
        "--launcher-sha256", launcher_hash, "--capacity-guard-sha256", guard_hash,
        "--workspace-root", str(workspace), "--deployment-root", str(target_deployment),
        "--automation-root", str(target_deployment / "automation"),
        "--output-root", str(target_runtime / "outputs"),
        "--receipt-root", str(target_runtime / "receipts"),
        "--host-lock-path", str(target_runtime / "locks/controller.lock"),
        "--allowed-parent-root", str(classification_root),
        "--operation-root", str(target_runtime / "attempts"),
        "--controller-log", str(target_runtime / "logs/controller.log"),
        "--python", str(python_path),
    ]
    preflight_argv = launch_argv + ["--preflight-only"]

    binding_dir = build_root / BINDING_DIRECTORY
    binding_path = binding_dir / "launcher_binding.private.json"
    write_json(binding_path, {
        "schema_version": 1, "kind": "private_local_exact_launcher_binding",
        "binding_id": "pan-ssd-uga-recovery2-launcher-binding-20261107",
        "sequence_id": SEQUENCE_ID, "created_at_utc": args.created_at_utc,
        "repository": "lin61302/scraping", "branch": "main", "production_writes": False,
        "execution_authorized": False, "execute_now": False, "resume_supported": False,
        "startup_inspection_seconds": 4, "network_ceiling": 1, "browser_ceiling": 0,
        "launch_argv": launch_argv, "launch_argv_sha256": argv_sha256(launch_argv),
        "preflight_argv": preflight_argv, "preflight_argv_sha256": argv_sha256(preflight_argv),
        "paths": {
            "workspace_root": str(workspace), "deployment_root": str(target_deployment),
            "automation_root": str(target_deployment / "automation"), "plan": str(plan_final),
            "deployment_manifest": str(manifest_final),
            "runtime_roots": {name: str(target_runtime / name)
                              for name in ("attempts", "locks", "logs", "outputs", "receipts")},
            "allowed_parent_roots": [str(classification_root)],
        },
        "pins": {
            "plan_sha256": plan_sha, "deployment_manifest_sha256": manifest_sha,
            "deployment_sha256": deployment_sha, "controller_sha256": controller_hash,
            "launcher_sha256": launcher_hash, "capacity_guard_sha256": guard_hash,
            "identity_proof_sha256": sha256_path(identity_path),
            "sealed_parent_proof_sha256": sha256_path(parent_path),
            "predecessor_preservation_sha256": sha256_path(predecessor_path),
            "consumed_predecessor_failure_preservation_sha256": sha256_path(consumed_predecessor_path),
            "source_count_tcg_handoff_sha256": sha256_path(count_path),
            "publication_pins_sha256": sha256_path(publication_path),
            "deployment_directory_modes_sha256": sha256_path(deployment_modes_path),
        },
    })
    readiness_path = binding_dir / "launcher_readiness.private.json"
    write_json(readiness_path, {
        "schema_version": 1, "kind": "private_local_exact_launcher_readiness",
        "sequence_id": SEQUENCE_ID, "created_at_utc": args.created_at_utc,
        "status": "OFFLINE_PREPARED_NOT_AUTHORIZED_NOT_ATTEMPTED",
        "production_writes": False, "execution_authorized": False, "execute_now": False,
        "binding_sha256": sha256_path(binding_path),
        "static_scope": {"phase_count": 1, "lane_count": 4, "identity_count": 396,
                         "preserved_pass_count": 1240, "verified_exclusion_count": 546,
                         "network_worker_ceiling": 1, "browser_worker_ceiling": 0},
        "gates": {
            "runtime_and_builder_published": True, "independent_code_review_pinned": True,
            "sealed_classification_parent_only": True, "predecessor_packet_opened": False,
            "consumed_predecessor_packet_opened": False,
            "consumed_predecessor_runtime_read": False,
            "closed_runtime_read": False, "rediscovery": False, "capacity_guard_run": False,
            "process_inspection_run": False, "preflight_run": False,
            "authorization_created": False, "launcher_run": False,
        },
    })
    binding_integrity_path = binding_dir / "binding_integrity.private.json"
    write_json(binding_integrity_path, {
        "schema_version": 1, "kind": "private_local_exact_launcher_binding_integrity",
        "sequence_id": SEQUENCE_ID, "binding_sha256": sha256_path(binding_path),
        "readiness_sha256": sha256_path(readiness_path), "plan_sha256": plan_sha,
        "deployment_manifest_sha256": manifest_sha, "deployment_sha256": deployment_sha,
        "preflight_argv_sha256": argv_sha256(preflight_argv),
        "launch_argv_sha256": argv_sha256(launch_argv),
        "exact_no_bytecode_prefix": launch_argv[:5], "network_ceiling": 1,
        "browser_ceiling": 0, "production_writes": False,
        "execution_authorized": False, "execute_now": False,
    })
    recipe_path = build_root / "local_invocation_recipe.private.json"
    write_json(recipe_path, {
        "schema_version": 1, "kind": "private_non_executable_local_exact_followup_recipe",
        "sequence_id": SEQUENCE_ID, "created_at_utc": args.created_at_utc,
        "production_writes": False, "execution_authorized": False, "execute_now": False,
        "binding_sha256": sha256_path(binding_path), "readiness_sha256": sha256_path(readiness_path),
        "binding_integrity_sha256": sha256_path(binding_integrity_path),
        "preflight_argv_sha256": argv_sha256(preflight_argv),
        "launch_argv_sha256": argv_sha256(launch_argv), "argv": None,
        "resume_contract": "Initial attempt only; interruption closes for separate agent review.",
    })

    packet_directories = sorted([build_root] + [path for path in build_root.rglob("*") if path.is_dir()])
    packet_modes_path = build_root / "packet_directory_modes.private.json"
    write_json(packet_modes_path, {
        "schema_version": 1, "kind": "private_packet_directory_mode_proof",
        "sequence_id": SEQUENCE_ID,
        "all_mode_0700": all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in packet_directories),
        "runtime_roots": {
            name: {"relative_path": f"runtime.private/{name}",
                   "mode": f"{stat.S_IMODE((build_root / 'runtime.private' / name).stat().st_mode):04o}",
                   "empty": not any((build_root / "runtime.private" / name).iterdir())}
            for name in ("attempts", "locks", "logs", "outputs", "receipts")
        },
    })
    construction_path = build_root / "construction_record.private.json"
    write_json(construction_path, {
        "schema_version": 1, "kind": "private_single_worker_recovery_packet_construction_record",
        "sequence_id": SEQUENCE_ID, "created_at_utc": args.created_at_utc,
        "status": "OFFLINE_PREPARED_NOT_AUTHORIZED_NOT_ATTEMPTED",
        "production_writes": False, "source_or_network_work": False,
        "closed_runtime_read": False, "predecessor_packet_opened_or_modified": False,
        "capacity_guard_run": False, "process_inspection_run": False,
        "preflight_run": False, "authorization_created": False, "launcher_run": False,
        "pins": {
            "plan_sha256": plan_sha, "deployment_manifest_sha256": manifest_sha,
            "deployment_sha256": deployment_sha, "identity_proof_sha256": sha256_path(identity_path),
            "sealed_parent_proof_sha256": sha256_path(parent_path),
            "predecessor_preservation_sha256": sha256_path(predecessor_path),
            "consumed_predecessor_failure_preservation_sha256": sha256_path(consumed_predecessor_path),
            "source_count_tcg_handoff_sha256": sha256_path(count_path),
            "publication_pins_sha256": sha256_path(publication_path),
            "binding_sha256": sha256_path(binding_path), "readiness_sha256": sha256_path(readiness_path),
            "binding_integrity_sha256": sha256_path(binding_integrity_path),
            "preflight_argv_sha256": argv_sha256(preflight_argv),
            "launch_argv_sha256": argv_sha256(launch_argv),
        },
        "counts": {"phase_count": 1, "lane_count": 4, "identity_count": 396,
                   "preserved_pass_count": 1240, "verified_exclusion_count": 546,
                   "deployment_file_count": len(deployment_rows), "runtime_root_count": 5},
    })

    core_artifacts = [
        manifest_path, identity_path, parent_path, predecessor_path,
        consumed_predecessor_path, count_path,
        publication_path, deployment_list_path, digest_rows_path, deployment_sha_path,
        deployment_modes_path, packet_modes_path, binding_path, readiness_path,
        binding_integrity_path, recipe_path, construction_path,
    ]
    for artifact in core_artifacts:
        add_sidecar(artifact)
    bundle_path = build_root / "bundle_integrity.private.json"
    write_json(bundle_path, {
        "schema_version": 1, "kind": "private_single_worker_recovery_bundle_integrity",
        "sequence_id": SEQUENCE_ID, "created_at_utc": args.created_at_utc,
        "status": "PASS_OFFLINE_PREPARED_NOT_AUTHORIZED_NOT_ATTEMPTED",
        "production_writes": False,
        "deployment": {"sha256": deployment_sha, "file_count": len(deployment_rows),
                       "directory_count": len(deployment_directories)},
        "scope": {"phase_count": 1, "lane_count": 4, "identity_count": 396,
                  "preserved_pass_count": 1240, "verified_exclusion_count": 546},
        "artifacts": [{"relative_path": path.relative_to(build_root).as_posix(),
                       "sha256": sha256_path(path), "bytes": path.stat().st_size,
                       "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}"}
                      for path in core_artifacts],
        "checks": {
            "exact_396_identity_union": True, "sealed_parent_subsets": True,
            "preserved_passes_and_exclusions_pinned": True,
            "runtime_roots_unique_empty_0700": True,
            "network_ceiling_1_browser_ceiling_0": True,
            "macos_capacity_guard_pinned": True, "log_cap_50_mib": True,
            "no_rediscovery": True, "no_predecessor_or_closed_runtime_read": True,
            "consumed_predecessor_failure_preserved_without_runtime_read": True,
            "no_guard_process_network_preflight_authorization_or_launch": True,
            "source_monthly_count_tcg_handoff_pinned": True,
        },
    })
    add_sidecar(bundle_path)
    construction = read_json(construction_path)
    full_path = build_root / "full_integrity_report.private.json"
    write_json(full_path, {
        "schema_version": 1, "kind": "private_single_worker_recovery_full_integrity_report",
        "sequence_id": SEQUENCE_ID, "created_at_utc": args.created_at_utc,
        "status": "PASS_OFFLINE_PREPARED_NOT_AUTHORIZED_NOT_ATTEMPTED",
        "production_writes": False, "execution_authorized": False, "execute_now": False,
        "pins": {
            **dict(construction["pins"]), "bundle_integrity_sha256": sha256_path(bundle_path),
            "construction_record_sha256": sha256_path(construction_path),
            "classification_seal_sha256": CLASSIFICATION_SEAL_SHA256,
            "classification_audit_sha256": CLASSIFICATION_AUDIT_SHA256,
            "classification_review_sha256": CLASSIFICATION_REVIEW_SHA256,
            "prerequisite_status_sha256": PREREQUISITE_STATUS_SHA256,
            "classification_completion_sha256": CLASSIFICATION_COMPLETION_SHA256,
            "runtime_commit": args.runtime_commit, "builder_commit": args.builder_commit,
        },
        "counts": construction["counts"], "manual_user_verification_required": False,
        "independent_agent_packet_audit_required_before_guard": True,
        "next_step": "independent static audit; then one capacity guard at an eligible window",
    })
    add_sidecar(full_path)

    for name in ("attempts", "locks", "logs", "outputs", "receipts"):
        runtime_root = build_root / "runtime.private" / name
        if stat.S_IMODE(runtime_root.stat().st_mode) != 0o700 or any(runtime_root.iterdir()):
            raise BuildError("runtime root is not empty and private")
    for directory in [build_root] + [path for path in build_root.rglob("*") if path.is_dir()]:
        if directory.is_symlink() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise BuildError("packet contains an unsafe directory")
    for path in deployment_build.rglob("*"):
        if path.is_symlink() or (path.is_file() and path.suffix in {".pyc", ".pyo"}) or (path.is_dir() and path.name == "__pycache__"):
            raise BuildError("deployment contains a link or bytecode artifact")
    if deployment_digest(deployment_rows) != deployment_sha:
        raise BuildError("deployment digest drifted")
    if launch_argv[:5] != exact_no_bytecode_prefix(python_path, launcher):
        raise BuildError("no-bytecode launch prefix drifted")
    if plan["capacity"]["network_ceiling"] != 1 or plan["phases"][0]["max_active_lanes"] != 1:
        raise BuildError("packet network ceiling drifted")
    if any(lane["workers"] != 1 for lane in lanes):
        raise BuildError("lane worker ceiling drifted")

    os.replace(build_root, target_root)
    print(json.dumps({
        "sequence_id": SEQUENCE_ID, "packet_root": str(target_root),
        "plan_sha256": plan_sha, "deployment_manifest_sha256": manifest_sha,
        "deployment_sha256": deployment_sha,
        "preflight_argv_sha256": argv_sha256(preflight_argv),
        "launch_argv_sha256": argv_sha256(launch_argv),
        "full_integrity_report_sha256": sha256_path(target_root / full_path.name),
        "status": "OFFLINE_PREPARED_NOT_AUTHORIZED_NOT_ATTEMPTED",
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
