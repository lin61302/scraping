#!/usr/bin/env python3
"""Publish reviewed scraper code and sanitized run provenance to GitHub.

This command is deliberately narrow.  It accepts an exact, hash-pinned local
manifest, copies only allowlisted text files into this repository, creates
sanitized launch/terminal records, commits explicit paths, pushes directly to
``origin/main``, and verifies the remote object id.  It never force pushes.

The initial installation of this helper must be reviewed, committed, and
pushed manually.  The helper refuses to run from a dirty repository, so it
cannot bootstrap its own uncommitted implementation.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit


HELPER_VERSION = "1.0.0"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COUNTRY_TOP_LEVEL_RE = re.compile(r"^[A-Z]{3}$")
WINDOWS_HOME_RE = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+\\")
POSIX_HOME_RE = re.compile(r"/(?:Users|home)/[^/\s]+/")
IPV4_CANDIDATE_RE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
BROWSER_VERSION_PRODUCTS = (
    "Chrome",
    "Chromium",
    "Firefox",
    "Version",
    "Safari",
    "AppleWebKit",
    "Edg",
    "OPR",
)
URL_SPAN_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+")


class PublishError(RuntimeError):
    """A fail-closed publishing error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    """Small subprocess seam so unit tests can remain network-free."""

    def run(
        self,
        args: Sequence[str],
        cwd: Path,
        check: bool = True,
        timeout: int = 120,
    ) -> CommandResult:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GH_PROMPT_DISABLED": "1",
                "LC_ALL": "C",
            }
        )
        try:
            completed = subprocess.run(
                list(args),
                cwd=str(cwd),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublishError("command_execution_failed", "Command execution failed: %s" % exc) from exc
        # Git porcelain output uses a leading space as the index status.  Keep
        # leading whitespace intact and remove only line terminators.
        result = CommandResult(
            completed.returncode,
            completed.stdout.rstrip("\r\n"),
            completed.stderr.rstrip("\r\n"),
        )
        if check and result.returncode != 0:
            detail = result.stderr or result.stdout or "no command output"
            raise PublishError("command_failed", "%s failed: %s" % (args[0], detail))
        return result


@dataclasses.dataclass(frozen=True)
class FilePlan:
    source: Path
    destination: Path
    destination_text: str
    sha256: str
    size_bytes: int
    mode: int
    content: bytes


@dataclasses.dataclass(frozen=True)
class RunRecordPlan:
    destination: Path
    destination_text: str
    content: bytes


@dataclasses.dataclass(frozen=True)
class PreparedManifest:
    manifest_id: str
    manifest_sha256: str
    files: Tuple[FilePlan, ...]
    run_records: Tuple[RunRecordPlan, ...]

    @property
    def all_destinations(self) -> Tuple[Path, ...]:
        return tuple(item.destination for item in self.files) + tuple(item.destination for item in self.run_records)


@dataclasses.dataclass(frozen=True)
class RepositoryState:
    base_sha: str
    remote_sha: str


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublishError("duplicate_json_key", "Duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path: Path) -> Mapping[str, Any]:
    try:
        content = path.read_bytes()
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except PublishError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublishError("invalid_json", "Cannot read JSON file %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict):
        raise PublishError("invalid_json_root", "JSON root must be an object: %s" % path)
    return value


def load_json_bytes(content: bytes, context: str) -> Mapping[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except PublishError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublishError("invalid_json", "Cannot parse JSON for %s" % context) from exc
    if not isinstance(value, dict):
        raise PublishError("invalid_json_root", "%s JSON root must be an object" % context)
    return value


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_exact_keys(value: Mapping[str, Any], required: Set[str], optional: Set[str], context: str) -> None:
    actual = set(value.keys())
    missing = required - actual
    extra = actual - required - optional
    if missing:
        raise PublishError("manifest_missing_key", "%s is missing keys: %s" % (context, ", ".join(sorted(missing))))
    if extra:
        raise PublishError("manifest_unknown_key", "%s has unknown keys: %s" % (context, ", ".join(sorted(extra))))


def _require_safe_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise PublishError("unsafe_identifier", "%s must be a safe identifier" % context)
    return value


def _require_hash(value: Any, context: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise PublishError("invalid_sha256", "%s must be a lowercase SHA-256" % context)
    return value


def _require_utc_timestamp(value: Any, context: str) -> str:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        raise PublishError("invalid_timestamp", "%s must use YYYY-MM-DDTHH:MM:SSZ" % context)
    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise PublishError("invalid_timestamp", "%s is not a valid UTC timestamp" % context) from exc
    return value


def _safe_relative_path(value: Any, context: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PublishError("unsafe_path", "%s must be a nonempty POSIX relative path" % context)
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise PublishError("unsafe_path", "%s contains an unsafe path" % context)
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in path.parts):
        raise PublishError("unsafe_path", "%s contains unsupported characters" % context)
    return path


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_no_symlink_components(path: Path, stop: Path, context: str) -> None:
    stop = stop.resolve()
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise PublishError("symlink_rejected", "%s traverses a symlink" % context)
        if current == stop:
            return
        if current.parent == current:
            raise PublishError("unsafe_path", "%s escaped its root" % context)
        current = current.parent


def validate_policy(policy: Mapping[str, Any]) -> None:
    _require_exact_keys(
        policy,
        {
            "schema_version",
            "expected_github_login",
            "repository",
            "limits",
            "allowed",
            "privacy",
            "validation_commands",
        },
        set(),
        "policy",
    )
    if policy["schema_version"] != 1:
        raise PublishError("unsupported_policy", "Policy schema_version must be 1")
    if policy["expected_github_login"] != "lin61302":
        raise PublishError("unsafe_policy", "Expected GitHub login must be lin61302")
    repository = policy["repository"]
    if not isinstance(repository, dict):
        raise PublishError("unsafe_policy", "repository policy must be an object")
    _require_exact_keys(repository, {"remote", "owner", "name", "branch", "upstream"}, set(), "policy.repository")
    expected_repository = {
        "remote": "origin",
        "owner": "lin61302",
        "name": "scraping",
        "branch": "main",
        "upstream": "origin/main",
    }
    if repository != expected_repository:
        raise PublishError("unsafe_policy", "Repository policy must exactly target lin61302/scraping main")
    limits = policy["limits"]
    if not isinstance(limits, dict):
        raise PublishError("unsafe_policy", "limits must be an object")
    _require_exact_keys(
        limits,
        {
            "max_file_bytes",
            "max_files",
            "max_run_records",
            "max_sources_per_record",
            "max_evidence_sha256_per_record",
        },
        set(),
        "policy.limits",
    )
    for key, value in limits.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PublishError("unsafe_policy", "Policy limit %s must be a positive integer" % key)
    allowed = policy["allowed"]
    if not isinstance(allowed, dict):
        raise PublishError("unsafe_policy", "allowed must be an object")
    _require_exact_keys(
        allowed,
        {"top_level_directories", "root_extensions", "extensions", "executable_extensions", "forbidden_components"},
        set(),
        "policy.allowed",
    )
    for key in (
        "top_level_directories",
        "root_extensions",
        "extensions",
        "executable_extensions",
        "forbidden_components",
    ):
        if not isinstance(allowed[key], list) or not all(isinstance(item, str) and item for item in allowed[key]):
            raise PublishError("unsafe_policy", "policy.allowed.%s must be a nonempty string list" % key)
    privacy = policy["privacy"]
    if not isinstance(privacy, dict):
        raise PublishError("unsafe_policy", "privacy must be an object")
    _require_exact_keys(privacy, {"allowed_count_keys"}, set(), "policy.privacy")
    if not isinstance(privacy["allowed_count_keys"], list) or not all(
        isinstance(item, str) and SAFE_ID_RE.fullmatch(item) for item in privacy["allowed_count_keys"]
    ):
        raise PublishError("unsafe_policy", "allowed_count_keys must contain safe identifiers")
    commands = policy["validation_commands"]
    if not isinstance(commands, list) or not commands:
        raise PublishError("unsafe_policy", "validation_commands must be a nonempty list")
    for command in commands:
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise PublishError("unsafe_policy", "Each validation command must be a nonempty argument list")


def normalize_github_remote(url: str) -> Tuple[str, str]:
    """Return owner/repository for an uncredentialed GitHub remote."""

    if not url or any(character.isspace() for character in url):
        raise PublishError("unexpected_remote", "Origin URL is empty or contains whitespace")
    owner_repo: Optional[str] = None
    if url.startswith("git@github.com:"):
        owner_repo = url[len("git@github.com:") :]
    elif url.startswith("ssh://git@github.com/"):
        owner_repo = url[len("ssh://git@github.com/") :]
    elif url.startswith("https://"):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PublishError("unexpected_remote", "HTTPS origin must be an uncredentialed github.com URL")
        owner_repo = parsed.path.lstrip("/")
    else:
        raise PublishError("unexpected_remote", "Origin must use supported GitHub HTTPS or SSH syntax")
    if owner_repo.endswith(".git"):
        owner_repo = owner_repo[:-4]
    pieces = owner_repo.split("/") if owner_repo else []
    if len(pieces) != 2 or not all(SAFE_ID_RE.fullmatch(piece) for piece in pieces):
        raise PublishError("unexpected_remote", "Origin must name exactly one GitHub owner and repository")
    return pieces[0], pieces[1]


def _validate_destination(path: Path, policy: Mapping[str, Any], context: str) -> None:
    allowed = policy["allowed"]
    forbidden = {item.casefold() for item in allowed["forbidden_components"]}
    for part in path.parts:
        if part.casefold() in forbidden or part.startswith("."):
            raise PublishError("forbidden_destination", "%s contains a forbidden component" % context)
    top = path.parts[0]
    named_top_levels = set(allowed["top_level_directories"])
    suffix = path.suffix.lower()
    reviewed_systemd_unit = (
        path.parts[:3] == ("automation", "ops", "systemd")
        and suffix in {".service", ".timer"}
    )
    if suffix not in set(allowed["extensions"]) and not reviewed_systemd_unit:
        raise PublishError("forbidden_extension", "%s has a disallowed extension" % context)
    if len(path.parts) == 1:
        if suffix not in set(allowed["root_extensions"]):
            raise PublishError("forbidden_destination", "%s is not an allowlisted root code file" % context)
    elif top not in named_top_levels and not COUNTRY_TOP_LEVEL_RE.fullmatch(top):
        raise PublishError("forbidden_destination", "%s is outside an allowlisted code directory" % context)
    if suffix == ".json" and not (
        path.parts[:2] == ("automation", "config") or path.parts[:2] == ("automation", "run_records")
    ):
        raise PublishError("forbidden_destination", "JSON is limited to automation/config and automation/run_records")
    if suffix == ".md" and (len(path.parts) == 1 or top != "docs"):
        raise PublishError("forbidden_destination", "Markdown is limited to docs")


def _validate_text_privacy(content: bytes, context: str, max_bytes: int) -> str:
    if len(content) > max_bytes:
        raise PublishError("file_too_large", "%s exceeds the policy size limit" % context)
    if b"\x00" in content:
        raise PublishError("binary_rejected", "%s contains NUL bytes" % context)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublishError("binary_rejected", "%s is not valid UTF-8 text" % context) from exc
    secret_patterns = (
        ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
        ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
        ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
        (
            "literal_secret_assignment",
            re.compile(
                r"(?im)^\s*(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|secret|token)\s*=\s*['\"][^'\"${}\s][^'\"\r\n]{7,}['\"]"
            ),
        ),
        ("credentialed_mongodb_uri", re.compile(r"mongodb(?:\+srv)?://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE)),
        ("credentialed_url", re.compile(r"https?://[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)),
    )
    for name, pattern in secret_patterns:
        if pattern.search(text):
            raise PublishError("secret_detected", "%s contains a prohibited %s pattern" % (context, name))
    if POSIX_HOME_RE.search(text) or WINDOWS_HOME_RE.search(text):
        raise PublishError("local_path_detected", "%s contains an absolute user-home path" % context)
    machine_label_pattern = re.compile(r"\b" + "machine" + r"(?:[ _-]?" + "17" + r")\b", re.IGNORECASE)
    on_host_suffix_pattern = re.compile(re.escape("_on_") + "17" + r"\b", re.IGNORECASE)
    if machine_label_pattern.search(text) or on_host_suffix_pattern.search(text):
        raise PublishError("machine_identifier_detected", "%s contains a machine-specific identifier" % context)
    local_service_pattern = re.compile(
        r"\b" + ("local" + "host") + r"(?P<separator>[:_])(?P<port>[0-9]{1,5})\b",
        re.IGNORECASE,
    )
    for match in local_service_pattern.finditer(text):
        port = int(match.group("port"))
        if 1 <= port <= 65535:
            raise PublishError("local_service_detected", "%s contains a localhost service endpoint" % context)
    url_spans = tuple((match.start(), match.end()) for match in URL_SPAN_RE.finditer(text))
    for candidate_match in IPV4_CANDIDATE_RE.finditer(text):
        candidate = candidate_match.group(0)
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        in_url = any(start <= candidate_match.start() < end for start, end in url_spans)
        if in_url:
            raise PublishError("infrastructure_detected", "%s contains an IPv4 address in a URL" % context)
        browser_version = False
        for product in BROWSER_VERSION_PRODUCTS:
            prefix = product + "/"
            prefix_start = candidate_match.start() - len(prefix)
            if prefix_start < 0 or text[prefix_start : candidate_match.start()] != prefix:
                continue
            if prefix_start > 0 and (text[prefix_start - 1].isalnum() or text[prefix_start - 1] in "_-"):
                continue
            browser_version = True
            break
        if browser_version:
            continue
        if not (address.is_loopback or address.is_unspecified):
            raise PublishError("infrastructure_detected", "%s contains a non-loopback IPv4 address" % context)
    return text


def _validate_source_file(source: Path, source_root: Path, context: str) -> None:
    _assert_no_symlink_components(source, source_root, context)
    try:
        metadata = source.stat()
    except OSError as exc:
        raise PublishError("source_missing", "%s does not exist" % context) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise PublishError("source_not_regular", "%s is not a regular file" % context)


def _sanitize_run_record(
    raw: Mapping[str, Any],
    index: int,
    repo: Path,
    policy: Mapping[str, Any],
) -> RunRecordPlan:
    context = "manifest.run_records[%d]" % index
    common = {
        "event",
        "run_id",
        "occurred_at",
        "source_ids",
        "scripts",
        "production_writes",
        "git_commit",
        "operation_manifest_sha256",
        "acceptance_window",
        "evidence_sha256",
    }
    if raw.get("event") == "launch":
        _require_exact_keys(raw, common, set(), context)
    elif raw.get("event") == "terminal":
        _require_exact_keys(raw, common | {"outcome", "exit_code", "counts"}, set(), context)
    else:
        raise PublishError("invalid_run_event", "%s event must be launch or terminal" % context)
    event = raw["event"]
    run_id = _require_safe_id(raw["run_id"], context + ".run_id")
    occurred_at = _require_utc_timestamp(raw["occurred_at"], context + ".occurred_at")
    if not isinstance(raw["production_writes"], bool):
        raise PublishError("invalid_run_record", "%s.production_writes must be boolean" % context)
    git_commit = raw["git_commit"]
    if not isinstance(git_commit, str) or not GIT_COMMIT_RE.fullmatch(git_commit):
        raise PublishError("invalid_run_record", "%s.git_commit must be 40 lowercase hex characters" % context)
    operation_manifest_sha256 = _require_hash(
        raw["operation_manifest_sha256"], context + ".operation_manifest_sha256"
    )
    acceptance_window = raw["acceptance_window"]
    if not isinstance(acceptance_window, dict):
        raise PublishError("invalid_run_record", "%s.acceptance_window must be an object" % context)
    _require_exact_keys(acceptance_window, {"from", "through"}, set(), context + ".acceptance_window")
    dates: Dict[str, str] = {}
    for key in ("from", "through"):
        value = acceptance_window[key]
        if not isinstance(value, str) or not DATE_RE.fullmatch(value):
            raise PublishError("invalid_run_record", "%s.acceptance_window.%s must use YYYY-MM-DD" % (context, key))
        try:
            dt.date.fromisoformat(value)
        except ValueError as exc:
            raise PublishError("invalid_run_record", "%s has an invalid acceptance date" % context) from exc
        dates[key] = value
    if dates["from"] > dates["through"]:
        raise PublishError("invalid_run_record", "%s acceptance window is reversed" % context)
    evidence_sha256 = raw["evidence_sha256"]
    if (
        not isinstance(evidence_sha256, list)
        or not evidence_sha256
        or len(evidence_sha256) > policy["limits"]["max_evidence_sha256_per_record"]
    ):
        raise PublishError("invalid_run_record", "%s.evidence_sha256 must be a bounded nonempty list" % context)
    sanitized_evidence = [
        _require_hash(value, context + ".evidence_sha256") for value in evidence_sha256
    ]
    if len(set(sanitized_evidence)) != len(sanitized_evidence):
        raise PublishError("invalid_run_record", "%s.evidence_sha256 contains duplicates" % context)
    source_ids = raw["source_ids"]
    if not isinstance(source_ids, list) or not source_ids or len(source_ids) > policy["limits"]["max_sources_per_record"]:
        raise PublishError("invalid_run_record", "%s.source_ids must be a bounded nonempty list" % context)
    sanitized_sources = [_require_safe_id(item, context + ".source_ids") for item in source_ids]
    if len(set(sanitized_sources)) != len(sanitized_sources):
        raise PublishError("invalid_run_record", "%s.source_ids contains duplicates" % context)
    scripts = raw["scripts"]
    if not isinstance(scripts, list) or not scripts:
        raise PublishError("invalid_run_record", "%s.scripts must be a nonempty list" % context)
    sanitized_scripts: List[Dict[str, str]] = []
    seen_scripts: Set[str] = set()
    for script_index, script in enumerate(scripts):
        script_context = "%s.scripts[%d]" % (context, script_index)
        if not isinstance(script, dict):
            raise PublishError("invalid_run_record", "%s must be an object" % script_context)
        _require_exact_keys(script, {"destination", "sha256"}, set(), script_context)
        destination_path = _safe_relative_path(script["destination"], script_context + ".destination")
        _validate_destination(destination_path, policy, script_context + ".destination")
        destination = destination_path.as_posix()
        if destination in seen_scripts:
            raise PublishError("invalid_run_record", "%s contains duplicate script destinations" % context)
        seen_scripts.add(destination)
        sanitized_scripts.append({"destination": destination, "sha256": _require_hash(script["sha256"], script_context + ".sha256")})
    sanitized: Dict[str, Any] = {
        "schema_version": 1,
        "event": event,
        "run_id": run_id,
        "occurred_at": occurred_at,
        "source_ids": sanitized_sources,
        "scripts": sanitized_scripts,
        "production_writes": raw["production_writes"],
        "git_commit": git_commit,
        "operation_manifest_sha256": operation_manifest_sha256,
        "acceptance_window": dates,
        "evidence_sha256": sanitized_evidence,
    }
    if event == "terminal":
        if raw["outcome"] not in {"succeeded", "partial", "failed", "timeout", "cancelled"}:
            raise PublishError("invalid_run_record", "%s.outcome is not allowed" % context)
        if not isinstance(raw["exit_code"], int) or isinstance(raw["exit_code"], bool) or raw["exit_code"] < 0:
            raise PublishError("invalid_run_record", "%s.exit_code must be a nonnegative integer" % context)
        counts = raw["counts"]
        allowed_count_keys = set(policy["privacy"]["allowed_count_keys"])
        if not isinstance(counts, dict) or not set(counts).issubset(allowed_count_keys):
            raise PublishError("invalid_run_record", "%s.counts contains unapproved keys" % context)
        sanitized_counts: Dict[str, int] = {}
        for key, value in counts.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PublishError("invalid_run_record", "%s.counts.%s must be nonnegative" % (context, key))
            sanitized_counts[key] = value
        sanitized.update({"outcome": raw["outcome"], "exit_code": raw["exit_code"], "counts": sanitized_counts})
    day = occurred_at[:10]
    destination_text = "automation/run_records/%s/%s.%s.json" % (day.replace("-", "/"), run_id, event)
    destination_rel = _safe_relative_path(destination_text, context + ".generated_destination")
    _validate_destination(destination_rel, policy, context + ".generated_destination")
    content = canonical_json_bytes(sanitized)
    _validate_text_privacy(content, context, policy["limits"]["max_file_bytes"])
    return RunRecordPlan(repo / destination_rel, destination_rel.as_posix(), content)


def prepare_manifest(
    manifest_path: Path,
    source_root: Path,
    repo: Path,
    policy: Mapping[str, Any],
) -> PreparedManifest:
    validate_policy(policy)
    source_root = source_root.resolve()
    repo = repo.resolve()
    if not source_root.is_dir():
        raise PublishError("source_root_missing", "Source root is not a directory")
    manifest_path = manifest_path.resolve()
    if _inside(manifest_path, repo):
        raise PublishError("manifest_inside_repository", "The input manifest must remain outside the Git repository")
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise PublishError("manifest_unreadable", "Cannot read the input manifest") from exc
    manifest_sha = sha256_bytes(manifest_bytes)
    manifest = load_json_bytes(manifest_bytes, "manifest")
    _require_exact_keys(manifest, {"schema_version", "manifest_id", "created_at", "files", "run_records"}, set(), "manifest")
    if manifest["schema_version"] != 1:
        raise PublishError("unsupported_manifest", "Manifest schema_version must be 1")
    manifest_id = _require_safe_id(manifest["manifest_id"], "manifest.manifest_id")
    _require_utc_timestamp(manifest["created_at"], "manifest.created_at")
    files = manifest["files"]
    records = manifest["run_records"]
    if not isinstance(files, list) or not isinstance(records, list) or (not files and not records):
        raise PublishError("empty_manifest", "Manifest must contain files or run records")
    if len(files) > policy["limits"]["max_files"] or len(records) > policy["limits"]["max_run_records"]:
        raise PublishError("manifest_limit_exceeded", "Manifest exceeds a configured item limit")
    file_plans: List[FilePlan] = []
    destination_names: Set[str] = set()
    for index, item in enumerate(files):
        context = "manifest.files[%d]" % index
        if not isinstance(item, dict):
            raise PublishError("invalid_file_entry", "%s must be an object" % context)
        _require_exact_keys(item, {"source", "destination", "sha256", "size_bytes", "mode"}, set(), context)
        source_rel = _safe_relative_path(item["source"], context + ".source")
        destination_rel = _safe_relative_path(item["destination"], context + ".destination")
        _validate_destination(destination_rel, policy, context + ".destination")
        destination_text = destination_rel.as_posix()
        if destination_text in destination_names:
            raise PublishError("duplicate_destination", "Manifest destination is duplicated: %s" % destination_text)
        destination_names.add(destination_text)
        source = source_root / source_rel
        _validate_source_file(source, source_root, context + ".source")
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise PublishError("source_unreadable", "%s cannot be read" % (context + ".source")) from exc
        declared_size = item["size_bytes"]
        if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size < 0 or declared_size != len(content):
            raise PublishError("size_mismatch", "%s.size_bytes does not match the source" % context)
        declared_hash = _require_hash(item["sha256"], context + ".sha256")
        if sha256_bytes(content) != declared_hash:
            raise PublishError("hash_mismatch", "%s.sha256 does not match the source" % context)
        mode_text = item["mode"]
        if mode_text not in {"0644", "0755"}:
            raise PublishError("invalid_mode", "%s.mode must be 0644 or 0755" % context)
        if mode_text == "0755" and destination_rel.suffix.lower() not in set(policy["allowed"]["executable_extensions"]):
            raise PublishError("invalid_mode", "%s cannot be executable" % context)
        _validate_text_privacy(content, context + ".source", policy["limits"]["max_file_bytes"])
        destination = repo / destination_rel
        _assert_no_symlink_components(destination, repo, context + ".destination")
        file_plans.append(
            FilePlan(source, destination, destination_text, declared_hash, declared_size, int(mode_text, 8), content)
        )
    record_plans: List[RunRecordPlan] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise PublishError("invalid_run_record", "manifest.run_records[%d] must be an object" % index)
        plan = _sanitize_run_record(record, index, repo, policy)
        if plan.destination_text in destination_names:
            raise PublishError("duplicate_destination", "Generated run record destination is duplicated")
        destination_names.add(plan.destination_text)
        _assert_no_symlink_components(plan.destination, repo, "run record destination")
        record_plans.append(plan)
    return PreparedManifest(manifest_id, manifest_sha, tuple(file_plans), tuple(record_plans))


def _verify_record_script_hashes(prepared: PreparedManifest, repo: Path) -> None:
    for plan in prepared.run_records:
        record = json.loads(plan.content.decode("utf-8"))
        for script in record["scripts"]:
            destination = repo / script["destination"]
            if not destination.is_file() or destination.is_symlink():
                raise PublishError("record_script_missing", "A run record references a missing script")
            if sha256_file(destination) != script["sha256"]:
                raise PublishError("record_script_hash_mismatch", "A run record script hash does not match repository content")


def verify_materialized(prepared: PreparedManifest) -> None:
    """Confirm hooks or concurrent work did not alter the reviewed artifacts."""

    for plan in prepared.files:
        if not plan.destination.is_file() or plan.destination.is_symlink():
            raise PublishError("materialized_file_missing", "A planned file is missing or is a symlink")
        if plan.destination.read_bytes() != plan.content:
            raise PublishError("materialized_file_changed", "A planned file changed after validation")
        if stat.S_IMODE(plan.destination.stat().st_mode) != plan.mode:
            raise PublishError("materialized_mode_changed", "A planned file mode changed after validation")
    for plan in prepared.run_records:
        if not plan.destination.is_file() or plan.destination.is_symlink() or plan.destination.read_bytes() != plan.content:
            raise PublishError("materialized_record_changed", "An immutable run record changed after validation")


def _write_atomic(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temporary), mode)
        os.replace(str(temporary), str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def materialize(prepared: PreparedManifest, repo: Path) -> List[str]:
    changed: List[str] = []
    for plan in prepared.files:
        existing = plan.destination.read_bytes() if plan.destination.exists() and plan.destination.is_file() else None
        existing_mode = stat.S_IMODE(plan.destination.stat().st_mode) if existing is not None else None
        if existing != plan.content or existing_mode != plan.mode:
            _write_atomic(plan.destination, plan.content, plan.mode)
            changed.append(plan.destination_text)
    for plan in prepared.run_records:
        if plan.destination.exists():
            if not plan.destination.is_file() or plan.destination.is_symlink():
                raise PublishError("record_collision", "Run record destination is not a regular file")
            if plan.destination.read_bytes() != plan.content:
                raise PublishError("record_collision", "An immutable run record already exists with different content")
        else:
            _write_atomic(plan.destination, plan.content, 0o644)
            changed.append(plan.destination_text)
    _verify_record_script_hashes(prepared, repo)
    return sorted(changed)


def verify_github_auth(repo: Path, policy: Mapping[str, Any], runner: CommandRunner) -> None:
    runner.run(["gh", "auth", "status", "--hostname", "github.com"], cwd=repo, timeout=30)
    login = runner.run(["gh", "api", "user", "--jq", ".login"], cwd=repo, timeout=30).stdout.strip()
    if login != policy["expected_github_login"]:
        raise PublishError("wrong_github_login", "Authenticated GitHub login is not the required account")


def _git_output(repo: Path, runner: CommandRunner, arguments: Sequence[str]) -> str:
    return runner.run(["git"] + list(arguments), cwd=repo).stdout.strip()


def _require_clean_repository(repo: Path, runner: CommandRunner) -> None:
    status = _git_output(repo, runner, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status:
        raise PublishError("repository_dirty", "Repository must be clean before publishing")


def preflight_repository(repo: Path, policy: Mapping[str, Any], runner: CommandRunner) -> RepositoryState:
    repo = repo.resolve()
    root = Path(_git_output(repo, runner, ["rev-parse", "--show-toplevel"])).resolve()
    if root != repo:
        raise PublishError("wrong_repository_root", "--repo must name the exact Git repository root")
    _require_clean_repository(repo, runner)
    repository_policy = policy["repository"]
    branch = _git_output(repo, runner, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if branch != repository_policy["branch"]:
        raise PublishError("wrong_branch", "Publishing is permitted only from main")
    upstream = _git_output(repo, runner, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream != repository_policy["upstream"]:
        raise PublishError("wrong_upstream", "main must track origin/main")
    fetch_url = _git_output(repo, runner, ["remote", "get-url", repository_policy["remote"]])
    push_url = _git_output(repo, runner, ["remote", "get-url", "--push", repository_policy["remote"]])
    for url in (fetch_url, push_url):
        if normalize_github_remote(url) != (repository_policy["owner"], repository_policy["name"]):
            raise PublishError("unexpected_remote", "Origin does not exactly target lin61302/scraping")
    verify_github_auth(repo, policy, runner)
    runner.run(["git", "fetch", "--prune", "origin", "main"], cwd=repo, timeout=120)
    local_sha = _git_output(repo, runner, ["rev-parse", "HEAD"])
    remote_sha = _git_output(repo, runner, ["rev-parse", "origin/main"])
    if local_sha != remote_sha:
        local_is_ancestor = runner.run(
            ["git", "merge-base", "--is-ancestor", local_sha, remote_sha], cwd=repo, check=False
        ).returncode == 0
        remote_is_ancestor = runner.run(
            ["git", "merge-base", "--is-ancestor", remote_sha, local_sha], cwd=repo, check=False
        ).returncode == 0
        if local_is_ancestor:
            runner.run(["git", "merge", "--ff-only", "origin/main"], cwd=repo)
            local_sha = _git_output(repo, runner, ["rev-parse", "HEAD"])
            if local_sha != remote_sha:
                raise PublishError("fast_forward_failed", "Fast-forward did not reach origin/main")
        elif remote_is_ancestor:
            raise PublishError("unpushed_local_commits", "Local main is ahead of origin/main; manual review is required")
        else:
            raise PublishError("diverged_history", "Local main and origin/main have diverged")
    _require_clean_repository(repo, runner)
    return RepositoryState(local_sha, remote_sha)


def _status_paths(repo: Path, runner: CommandRunner) -> Set[str]:
    # `_git_output` trims leading whitespace for scalar Git values. Porcelain
    # status needs that whitespace because it is one of the two status bytes.
    output = runner.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo
    ).stdout.rstrip("\r\n")
    paths: Set[str] = set()
    if not output:
        return paths
    for line in output.splitlines():
        if len(line) < 4:
            raise PublishError("unparseable_git_status", "Cannot parse Git status")
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        if value.startswith('"'):
            raise PublishError("unsafe_git_path", "Quoted Git paths are not supported by this publisher")
        paths.add(value)
    return paths


def run_validation_commands(repo: Path, policy: Mapping[str, Any], runner: CommandRunner) -> None:
    for command in policy["validation_commands"]:
        runner.run(command, cwd=repo, timeout=300)


def stage_and_commit(
    repo: Path,
    changed_paths: Sequence[str],
    manifest_id: str,
    policy: Mapping[str, Any],
    runner: CommandRunner,
) -> str:
    expected = set(changed_paths)
    if not expected:
        return _git_output(repo, runner, ["rev-parse", "HEAD"])
    run_validation_commands(repo, policy, runner)
    actual = _status_paths(repo, runner)
    if actual != expected:
        raise PublishError("unexpected_worktree_change", "Worktree changes differ from the explicit publish plan")
    runner.run(["git", "add", "--"] + sorted(expected), cwd=repo)
    staged_output = runner.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRTUXB"], cwd=repo
    ).stdout
    staged = {line for line in staged_output.splitlines() if line}
    if staged != expected:
        raise PublishError("unexpected_staging", "Staged paths differ from the explicit publish plan")
    commit_message = "Record scraper provenance: %s" % manifest_id
    runner.run(["git", "-c", "commit.gpgsign=false", "commit", "-m", commit_message], cwd=repo)
    committed_output = runner.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], cwd=repo
    ).stdout
    committed = {line for line in committed_output.splitlines() if line}
    if committed != expected:
        raise PublishError("unexpected_commit_content", "Committed paths differ from the explicit publish plan")
    _require_clean_repository(repo, runner)
    return _git_output(repo, runner, ["rev-parse", "HEAD"])


def push_and_verify(repo: Path, commit_sha: str, runner: CommandRunner) -> str:
    runner.run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=repo, timeout=180)
    output = runner.run(
        ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"], cwd=repo, timeout=60
    ).stdout
    fields = output.split()
    if len(fields) != 2 or fields[1] != "refs/heads/main" or fields[0] != commit_sha:
        raise PublishError("remote_verification_failed", "origin/main does not resolve to the published commit")
    _require_clean_repository(repo, runner)
    return fields[0]


def _remove_empty_parents(path: Path, repo: Path) -> None:
    current = path.parent
    while current != repo:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def rollback_before_commit(repo: Path, base_sha: str, destinations: Iterable[Path], runner: CommandRunner) -> None:
    runner.run(["git", "reset", "--hard", base_sha], cwd=repo, check=False)
    for destination in destinations:
        relative = destination.relative_to(repo).as_posix()
        tracked = runner.run(["git", "cat-file", "-e", "%s:%s" % (base_sha, relative)], cwd=repo, check=False)
        if tracked.returncode != 0:
            with contextlib.suppress(FileNotFoundError):
                destination.unlink()
            _remove_empty_parents(destination, repo)


def _receipt_base(manifest_id: str, manifest_sha: str) -> str:
    return "%s-%s" % (manifest_id, manifest_sha[:12])


def write_success_receipt(
    receipt_dir: Path,
    repo: Path,
    prepared: PreparedManifest,
    outcome: str,
    changed_paths: Sequence[str],
    commit_sha: str,
    remote_sha: str,
) -> Path:
    receipt_dir = receipt_dir.resolve()
    if _inside(receipt_dir, repo.resolve()):
        raise PublishError("receipt_inside_repository", "Receipt directory must be outside the Git repository")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / (_receipt_base(prepared.manifest_id, prepared.manifest_sha256) + ".json")
    receipt: Dict[str, Any] = {
        "schema_version": 1,
        "helper_version": HELPER_VERSION,
        "manifest_id": prepared.manifest_id,
        "manifest_sha256": prepared.manifest_sha256,
        "outcome": outcome,
        "repository": "lin61302/scraping",
        "branch": "main",
        "changed_paths": list(changed_paths),
        "commit_sha": commit_sha,
        "remote_sha": remote_sha,
        "completed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    content = canonical_json_bytes(receipt)
    if receipt_path.exists():
        existing = load_json(receipt_path)
        if existing.get("manifest_sha256") != prepared.manifest_sha256:
            raise PublishError("receipt_collision", "Existing receipt belongs to a different manifest")
        return receipt_path
    _write_atomic(receipt_path, content, 0o600)
    return receipt_path


def write_failure_receipt(
    receipt_dir: Path,
    repo: Path,
    manifest_id: str,
    manifest_sha: str,
    error_code: str,
) -> Optional[Path]:
    receipt_dir = receipt_dir.resolve()
    if _inside(receipt_dir, repo.resolve()):
        return None
    with contextlib.suppress(OSError):
        receipt_dir.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = receipt_dir / ("%s-failed-%s.json" % (_receipt_base(manifest_id, manifest_sha), timestamp))
        content = canonical_json_bytes(
            {
                "schema_version": 1,
                "helper_version": HELPER_VERSION,
                "manifest_id": manifest_id,
                "manifest_sha256": manifest_sha,
                "outcome": "failed",
                "error_code": error_code,
                "completed_at": dt.datetime.now(dt.timezone.utc)
                .replace(microsecond=0)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
        _write_atomic(path, content, 0o600)
        return path
    return None


def publish(
    repo: Path,
    source_root: Path,
    manifest_path: Path,
    policy_path: Path,
    receipt_dir: Path,
    runner: Optional[CommandRunner] = None,
) -> Tuple[str, Path]:
    runner = runner or CommandRunner()
    repo = repo.resolve()
    policy = load_json(policy_path.resolve())
    validate_policy(policy)
    receipt_dir = receipt_dir.resolve()
    if _inside(receipt_dir, repo):
        raise PublishError("receipt_inside_repository", "Receipt directory must be outside the Git repository")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    lock_path = receipt_dir / ".github-provenance.lock"
    with lock_path.open("a+b") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublishError("publisher_locked", "Another provenance publisher is active") from exc
        state = preflight_repository(repo, policy, runner)
        prepared = prepare_manifest(manifest_path, source_root, repo, policy)
        push_started = False
        try:
            changed = materialize(prepared, repo)
            if not changed:
                commit_sha = _git_output(repo, runner, ["rev-parse", "HEAD"])
                remote_sha = _git_output(repo, runner, ["rev-parse", "origin/main"])
                if commit_sha != remote_sha:
                    raise PublishError("remote_verification_failed", "No-op repository is not synchronized with origin/main")
                receipt = write_success_receipt(
                    receipt_dir, repo, prepared, "noop", changed, commit_sha, remote_sha
                )
                return "noop", receipt
            commit_sha = stage_and_commit(repo, changed, prepared.manifest_id, policy, runner)
            verify_materialized(prepared)
            push_started = True
            remote_sha = push_and_verify(repo, commit_sha, runner)
            receipt = write_success_receipt(
                receipt_dir, repo, prepared, "published", changed, commit_sha, remote_sha
            )
            return "published", receipt
        except Exception:
            if not push_started:
                rollback_before_commit(repo, state.base_sha, prepared.all_destinations, runner)
            raise


def _default_repo() -> Path:
    return Path(__file__).resolve().parents[2]


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path, help="Hash-pinned JSON manifest outside the repository")
    parser.add_argument("--source-root", required=True, type=Path, help="Root used to resolve manifest source paths")
    parser.add_argument("--receipt-dir", required=True, type=Path, help="Local receipt directory outside the repository")
    parser.add_argument("--repo", type=Path, default=_default_repo(), help="Exact lin61302/scraping checkout root")
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="Policy file (default: automation/config/github_publish_policy.json in the repository)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    repo = arguments.repo.resolve()
    policy_path = arguments.policy or (repo / "automation/config/github_publish_policy.json")
    manifest_id = "unknown"
    manifest_sha = "0" * 64
    try:
        manifest_path = arguments.manifest.resolve()
        if manifest_path.is_file():
            manifest_sha = sha256_file(manifest_path)
            with contextlib.suppress(Exception):
                raw_manifest = load_json(manifest_path)
                manifest_id = _require_safe_id(raw_manifest.get("manifest_id"), "manifest.manifest_id")
        outcome, receipt = publish(
            repo=repo,
            source_root=arguments.source_root,
            manifest_path=manifest_path,
            policy_path=policy_path,
            receipt_dir=arguments.receipt_dir,
        )
        print(json.dumps({"outcome": outcome, "receipt": str(receipt)}, sort_keys=True))
        return 0
    except PublishError as exc:
        receipt = write_failure_receipt(arguments.receipt_dir, repo, manifest_id, manifest_sha, exc.code)
        result = {"outcome": "failed", "error_code": exc.code, "message": str(exc)}
        if receipt is not None:
            result["receipt"] = str(receipt)
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
