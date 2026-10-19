#!/usr/bin/env python3
"""Capture one private, bounded recovery packet from a configured host.

This operator is deliberately separate from every scraper and campaign
launcher. It opens exactly one SSH session to the explicitly configured
host, runs a read-only Python program, and stores the result locally with
private permissions.  It never starts source work, changes Git state, writes a
remote archive, or prints captured evidence.

The remote program collects incident, capacity, network, Git, and runtime
evidence.  Modified and untracked *code* is packed in memory on the remote
host, base64 framed inside the one response, and materialized as a private
local tarball.  Credentials and likely secret files are neither inventoried
nor archived.  Command output is scrubbed before it leaves the remote host.

Exit codes:

* 0: capture completed and all required evidence groups have usable evidence
* 1: the one SSH attempt or response validation failed
* 2: capture completed with evidence gaps (for example journal permissions)
* 3: local one-shot/preflight gate failed before SSH
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
from typing import Callable, Mapping, Sequence


SCHEMA_VERSION = 1
EXPECTED_GITHUB_LOGIN = "lin61302"
EXPECTED_GITHUB_REPOSITORY = "lin61302/scraping"
EXPECTED_BRANCH = "main"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_STDERR_BYTES = 2 * 1024 * 1024
SSH_TIMEOUT_SECONDS = 900
CAPTURE_ID = "host-read-only-recovery"

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIVATE_OUTPUT_ROOT = WORKSPACE_ROOT / "outputs" / "operations"
PUBLIC_GITHUB_ARCHIVE_ROOT = WORKSPACE_ROOT / "automation" / "github_script_archive"

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700

LOCAL_SECRET_PATTERNS = (
    ("authorization_basic", re.compile(rb"(?i)authorization\s*:\s*basic\s+[A-Za-z0-9+/=]+")),
    ("authorization_token", re.compile(rb"(?i)authorization\s*:\s*(?:bearer|token)\s+[^\s]+")),
    ("github_token", re.compile(rb"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b")),
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("openai_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("private_key", re.compile(rb"(?s)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----")),
    (
        "sensitive_assignment",
        re.compile(
            rb"(?i)(?:password|passwd|token|secret|api[_-]?key|aws_secret_access_key|"
            rb"mongo(?:db)?[_-]?uri|database[_-]?(?:url|uri)|redis[_-]?(?:url|uri)|"
            rb"dsn|connection[_-]?string)\s*[=:]\s*[^\s,;]+"
        ),
    ),
    (
        "sensitive_flag",
        re.compile(
            rb"(?i)--?(?:password|passwd|token|secret|api[_-]?key|"
            rb"mongo(?:db)?[_-]?uri|database[_-]?(?:url|uri)|redis[_-]?(?:url|uri)|"
            rb"dsn|connection[_-]?string)\s+[^\s]+"
        ),
    ),
    ("uri_userinfo", re.compile(rb"(?i)[a-z][a-z0-9+.-]*://[^/@\s]+@")),
    (
        "secret_query",
        re.compile(rb"(?i)[?&](?:access[_-]?token|token|api[_-]?key|key|password|passwd|sig|signature)=[^&\s]+"),
    ),
)

REQUIRED_EVIDENCE_GROUPS = (
    "boot_shutdown",
    "previous_boot_journal",
    "kernel_oom",
    "ssh_auth",
    "network",
    "disk_inode_filesystem",
    "memory_swap_load",
    "process_open_files",
    "logs_and_run_roots",
    "git_checkout",
    "private_code_archive",
    "runtime_environments",
    "github_provenance",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_since_utc(value: str) -> str:
    if not re.fullmatch(r"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        raise argparse.ArgumentTypeError("--since-utc must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def validate_ssh_user(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value):
        raise argparse.ArgumentTypeError("invalid SSH user")
    return value


def validate_ssh_host(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", value):
        raise argparse.ArgumentTypeError("invalid SSH host")
    if value.endswith(".") or ".." in value:
        raise argparse.ArgumentTypeError("invalid SSH host")
    return value


def validate_remote_repository(value: str) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9._/+@=-]{1,4094}", value):
        raise argparse.ArgumentTypeError(
            "remote repository must be an absolute shell-safe POSIX path"
        )
    if ".." in PurePosixPath(value).parts:
        raise argparse.ArgumentTypeError("remote repository cannot traverse parents")
    return value


def scrub_local_stderr(value: bytes) -> str:
    """Return bounded diagnostic text without credential-like substrings."""
    text = value[:MAX_STDERR_BYTES].decode("utf-8", errors="replace")
    patterns = (
        (r"(?i)(authorization\s*:\s*(?:bearer|token)\s+)[^\s]+", r"\1[REDACTED]"),
        (r"(?i)(authorization\s*:\s*basic\s+)[A-Za-z0-9+/=]+", r"\1[REDACTED]"),
        (
            r"(?i)((?:password|passwd|token|secret|api[_-]?key|aws_secret_access_key|"
            r"mongo(?:db)?[_-]?uri|database[_-]?(?:url|uri)|redis[_-]?(?:url|uri)|"
            r"dsn|connection[_-]?string)\s*[=:]\s*)[^\s]+",
            r"\1[REDACTED]",
        ),
        (
            r"(?i)((?:--?(?:password|passwd|token|secret|api[_-]?key|"
            r"mongo(?:db)?[_-]?uri|database[_-]?(?:url|uri)|redis[_-]?(?:url|uri)|"
            r"dsn|connection[_-]?string))\s+)[^\s]+",
            r"\1[REDACTED]",
        ),
        (r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b", "[REDACTED]"),
        (r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[REDACTED]"),
        (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED]"),
        (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "[REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{20,}\b", "[REDACTED]"),
        (r"(?s)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]"),
        (r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1[REDACTED]@"),
        (
            r"(?i)([?&](?:access[_-]?token|token|api[_-]?key|key|password|passwd|sig|signature)=)[^&\s]+",
            r"\1[REDACTED]",
        ),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def local_secret_kinds(value: bytes) -> list[str]:
    """Return stable secret classes without returning matched values."""
    return sorted(name for name, pattern in LOCAL_SECRET_PATTERNS if pattern.search(value))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_private_output_dir(
    output_dir: Path,
    *,
    private_root: Path = DEFAULT_PRIVATE_OUTPUT_ROOT,
) -> Path:
    """Pin evidence below the private operations root and outside Git trees."""
    requested = output_dir.expanduser()
    root = private_root.expanduser().resolve(strict=True)
    resolved = requested.resolve(strict=False)
    if resolved == root or not _is_relative_to(resolved, root):
        raise ValueError("output directory must be a child of the private operations root")
    public_root = PUBLIC_GITHUB_ARCHIVE_ROOT.resolve(strict=False)
    if resolved == public_root or _is_relative_to(resolved, public_root):
        raise ValueError("output directory cannot be inside the public GitHub archive")
    cursor = resolved
    while True:
        if (cursor / ".git").exists():
            raise ValueError("output directory cannot be inside a Git worktree")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    # A pre-existing symlink could redirect final evidence after validation.
    cursor = requested.absolute()
    while cursor != cursor.parent:
        if cursor.resolve(strict=False) == root:
            break
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("output directory path cannot contain a symlink")
        cursor = cursor.parent
    return resolved


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_overflow: bool = False
    stderr_overflow: bool = False
    timed_out: bool = False
    stdout_bytes_seen: int = 0
    stderr_bytes_seen: int = 0


def run_process_bounded(
    command: Sequence[str],
    *,
    input: bytes,
    stdout: object,
    stderr: object,
    timeout: int,
    check: bool,
    stdout_limit: int = MAX_RESPONSE_BYTES,
    stderr_limit: int = MAX_STDERR_BYTES,
) -> BoundedProcessResult:
    """Run a process while enforcing byte ceilings during production."""
    del stdout, stderr, check
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_data = bytearray()
    stderr_data = bytearray()
    state = {"stdout_overflow": False, "stderr_overflow": False}
    totals = {"stdout": 0, "stderr": 0}
    state_lock = threading.Lock()

    def drain(name: str, stream: object, target: bytearray, limit: int) -> None:
        while True:
            chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
            if not chunk:
                return
            with state_lock:
                totals[name] += len(chunk)
            remaining = max(0, limit - len(target))
            if remaining:
                target.extend(chunk[:remaining])
            if len(chunk) > remaining:
                with state_lock:
                    state[name + "_overflow"] = True
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return

    threads = [
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout, stdout_data, stdout_limit),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr, stderr_data, stderr_limit),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    assert process.stdin is not None
    try:
        process.stdin.write(input)
        process.stdin.close()
    except BrokenPipeError:
        pass
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        returncode = process.wait()
    for thread in threads:
        thread.join(timeout=10)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    return BoundedProcessResult(
        returncode=124 if timed_out else returncode,
        stdout=bytes(stdout_data),
        stderr=bytes(stderr_data),
        stdout_overflow=state["stdout_overflow"],
        stderr_overflow=state["stderr_overflow"],
        timed_out=timed_out,
        stdout_bytes_seen=totals["stdout"],
        stderr_bytes_seen=totals["stderr"],
    )


def atomic_private_write(path: Path, data: bytes) -> None:
    """Publish a complete private file atomically and exactly once."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    os.chmod(path.parent, PRIVATE_DIRECTORY_MODE)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.tmp."
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    os.chmod(path, PRIVATE_FILE_MODE)


def atomic_private_json(path: Path, value: object) -> None:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_private_write(path, data)


def build_ssh_command(
    user: str,
    host: str,
    remote_repository: str,
    since_utc: str,
) -> list[str]:
    """Build the sole connection command from validated explicit inputs."""
    validate_ssh_user(user)
    validate_ssh_host(host)
    validate_remote_repository(remote_repository)
    validate_since_utc(since_utc)
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=30",
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=20",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=yes",
        f"{user}@{host}",
        "python3", "-",
        "--repository", remote_repository,
        "--since-utc", since_utc,
        "--expected-login", EXPECTED_GITHUB_LOGIN,
        "--expected-repository", EXPECTED_GITHUB_REPOSITORY,
        "--expected-branch", EXPECTED_BRANCH,
        "--max-archive-bytes", str(MAX_ARCHIVE_BYTES),
        "--max-response-bytes", str(MAX_RESPONSE_BYTES),
    ]


# This program executes on the configured host through stdin. It has no
# filesystem write primitive.  The only archive is a BytesIO object returned
# through stdout.  Keep it compatible with Python 3.8.
REMOTE_PROGRAM = r'''#!/usr/bin/env python3
import argparse
import base64
import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import selectors
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
MAX_COMMAND_BYTES = 2 * 1024 * 1024
MAX_UNTRACKED_FILE_BYTES = 8 * 1024 * 1024
MAX_SCAN_ENTRIES = 250000
MAX_SCAN_SECONDS = 30

CODE_SUFFIXES = frozenset({
    ".py", ".pyi", ".sh", ".bash", ".zsh", ".fish",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala",
    ".rb", ".php", ".pl", ".pm", ".r", ".sql",
})
CODE_BASENAMES = frozenset({
    "Dockerfile", "Makefile", "Justfile", "Rakefile", "Gemfile",
})
DENIED_PARTS = (
    ".env", "credential", "secret", "token", "password", "passwd",
    "cookie", "session", "private_key", "id_rsa", "id_ed25519",
    "oauth", "auth.json", "keychain", "netrc", "pem", "p12", "pfx",
)
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)((?:password|passwd|token|secret|api[_-]?key|authorization|aws_secret_access_key|"
    r"mongo(?:db)?[_-]?uri|database[_-]?(?:url|uri)|redis[_-]?(?:url|uri)|"
    r"dsn|connection[_-]?string)\s*[=:]\s*)([^\s,;]+)"
)
SENSITIVE_FLAG = re.compile(
    r"(?i)((?:--?(?:password|passwd|token|secret|api[_-]?key|"
    r"mongo(?:db)?[_-]?uri|database[_-]?(?:url|uri)|redis[_-]?(?:url|uri)|"
    r"dsn|connection[_-]?string))\s+)([^\s]+)"
)
TOKEN_PATTERN = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b"
)
BASIC_AUTH = re.compile(r"(?i)(authorization\s*:\s*basic\s+)[A-Za-z0-9+/=]+")
AUTH_TOKEN = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|token)\s+)[^\s]+")
AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
JWT_TOKEN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.S,
)
URI_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", re.I)
URI_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:access[_-]?token|token|api[_-]?key|key|password|passwd|sig|signature)=)[^&\s]+"
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--since-utc", required=True)
    parser.add_argument("--expected-login", required=True)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument("--expected-branch", required=True)
    parser.add_argument("--max-archive-bytes", required=True, type=int)
    parser.add_argument("--max-response-bytes", required=True, type=int)
    return parser.parse_args()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    digest = hashlib.sha256()
    total = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    return total, digest.hexdigest()


def scrub(text):
    text = TOKEN_PATTERN.sub("[REDACTED]", text)
    text = BASIC_AUTH.sub(r"\1[REDACTED]", text)
    text = AUTH_TOKEN.sub(r"\1[REDACTED]", text)
    text = AWS_ACCESS_KEY.sub("[REDACTED]", text)
    text = SLACK_TOKEN.sub("[REDACTED]", text)
    text = JWT_TOKEN.sub("[REDACTED]", text)
    text = OPENAI_KEY.sub("[REDACTED]", text)
    text = PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", text)
    text = URI_USERINFO.sub(r"\1[REDACTED]@", text)
    text = SENSITIVE_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    text = SENSITIVE_FLAG.sub(r"\1[REDACTED]", text)
    text = URI_SECRET_QUERY.sub(r"\1[REDACTED]", text)
    return text


def secret_kinds(data):
    text = data.decode("utf-8", errors="replace")
    checks = (
        ("authorization_basic", BASIC_AUTH),
        ("authorization_token", AUTH_TOKEN),
        ("github_token", TOKEN_PATTERN),
        ("aws_access_key", AWS_ACCESS_KEY),
        ("slack_token", SLACK_TOKEN),
        ("jwt", JWT_TOKEN),
        ("openai_key", OPENAI_KEY),
        ("private_key", PRIVATE_KEY_BLOCK),
        ("sensitive_assignment", SENSITIVE_ASSIGNMENT),
        ("sensitive_flag", SENSITIVE_FLAG),
        ("uri_userinfo", URI_USERINFO),
        ("secret_query", URI_SECRET_QUERY),
    )
    return sorted(name for name, pattern in checks if pattern.search(text))


def path_hash(value):
    return sha(value.encode("utf-8", errors="surrogateescape"))


def excluded_path(value, reason, secret_types=None):
    row = {"path_sha256": path_hash(value), "reason": reason}
    if secret_types:
        row["secret_types"] = sorted(set(secret_types))
    return row


def sanitized_url(raw):
    raw = raw.strip()
    if not raw:
        return raw
    if raw.startswith("git@github.com:"):
        return raw
    scp = re.match(r"^([^@\s]+)@([^:\s]+):(.*)$", raw)
    if scp:
        return "[REDACTED]@%s:%s" % (scp.group(2), scp.group(3))
    try:
        parts = urlsplit(raw)
    except Exception:
        return scrub(raw)
    if parts.scheme and parts.netloc:
        host = parts.hostname or ""
        if parts.port:
            host = "%s:%s" % (host, parts.port)
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    return scrub(raw)


def bounded_process(argv, cwd, env, timeout, stdout_limit, stderr_limit):
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, ("stdout", stdout_limit))
    selector.register(process.stderr, selectors.EVENT_READ, ("stderr", stderr_limit))
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    overflow = {"stdout": False, "stderr": False}
    timed_out = False
    deadline = time.monotonic() + timeout
    while selector.get_map():
        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
        events = selector.select(0.2)
        if not events and process.poll() is not None:
            events = [(key, selectors.EVENT_READ) for key in list(selector.get_map().values())]
        for key, _ in events:
            name, limit = key.data
            try:
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
            except OSError:
                chunk = b""
            if not chunk:
                try:
                    selector.unregister(key.fileobj)
                except Exception:
                    pass
                continue
            totals[name] += len(chunk)
            remaining = max(0, limit - len(buffers[name]))
            if remaining:
                buffers[name].extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow[name] = True
                process.kill()
        if timed_out and process.poll() is not None and not events:
            break
    selector.close()
    try:
        returncode = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait()
    process.stdout.close()
    process.stderr.close()
    return {
        "returncode": 124 if timed_out else returncode,
        "stdout": bytes(buffers["stdout"]),
        "stderr": bytes(buffers["stderr"]),
        "stdout_bytes": totals["stdout"],
        "stderr_bytes": totals["stderr"],
        "stdout_truncated": overflow["stdout"],
        "stderr_truncated": overflow["stderr"],
        "timed_out": timed_out,
    }


def command(name, argv, timeout=45, limit=MAX_COMMAND_BYTES, cwd=None, env=None):
    started = time.monotonic()
    run_env = os.environ.copy()
    run_env.update({
        "LC_ALL": "C", "LANG": "C", "PAGER": "cat", "SYSTEMD_PAGER": "cat",
        "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UpdateHostKeys=no",
        "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    })
    if env:
        run_env.update(env)
    try:
        result = bounded_process(
            argv, cwd, run_env, timeout, limit, min(limit, 512 * 1024),
        )
        stdout_raw = result["stdout"]
        stderr_raw = result["stderr"]
        stdout = scrub(stdout_raw.decode("utf-8", errors="replace"))
        stderr = scrub(stderr_raw.decode("utf-8", errors="replace"))
        return {
            "name": name,
            "available": True,
            "argv": argv,
            "returncode": result["returncode"],
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_bytes": result["stdout_bytes"],
            "stderr_bytes": result["stderr_bytes"],
            "stdout_truncated": result["stdout_truncated"],
            "stderr_truncated": result["stderr_truncated"],
            "timed_out": result["timed_out"],
        }
    except FileNotFoundError:
        return {
            "name": name, "available": False, "argv": argv,
            "returncode": None, "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": "", "stderr": "command_not_found",
            "stdout_bytes": 0, "stderr_bytes": 0,
            "stdout_truncated": False, "stderr_truncated": False,
        }


def group_ok(rows):
    return any(
        row.get("available") and row.get("returncode") == 0 and row.get("stdout", "").strip()
        for row in rows
    )


def command_ok(row, require_output=True):
    if not row.get("available") or row.get("returncode") != 0:
        return False
    return bool(row.get("stdout", "").strip()) if require_output else True


def resolve_git_root(candidate):
    row = command(
        "git_top_level",
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        timeout=30,
        limit=64 * 1024,
    )
    if not command_ok(row):
        return None, row
    first = row.get("stdout", "").splitlines()[0].strip()
    root = Path(first)
    if not root.is_absolute() or not root.is_dir():
        row["resolution_error"] = "resolved_top_level_not_absolute_directory"
        return None, row
    return root, row


def read_text(path, limit=1024 * 1024):
    try:
        target = Path(path)
        expected_bytes = target.stat().st_size
        with target.open("rb") as handle:
            data = handle.read(limit + 1)
    except Exception as exc:
        return {"path": path, "ok": False, "error": scrub("%s: %s" % (type(exc).__name__, exc))}
    return {
        "path": path, "ok": True, "bytes": expected_bytes,
        "captured_bytes": min(len(data), limit),
        "captured_sha256": sha(data[:limit]),
        "truncated": expected_bytes > limit or len(data) > limit,
        "text": scrub(data[:limit].decode("utf-8", errors="replace")),
    }


def normalized_load_snapshot():
    try:
        with open("/proc/loadavg", "r") as handle:
            values = [float(value) for value in handle.read(4096).split()[:3]]
        cpus = os.cpu_count() or 1
        return {
            "ok": True,
            "cpu_count": cpus,
            "load_1m": values[0],
            "load_5m": values[1],
            "load_15m": values[2],
            "normalized_1m": round(values[0] / cpus, 6),
            "normalized_5m": round(values[1] / cpus, 6),
            "normalized_15m": round(values[2] / cpus, 6),
        }
    except Exception as exc:
        return {"ok": False, "error": scrub("%s: %s" % (type(exc).__name__, exc))}


def code_path(raw):
    try:
        value = PurePosixPath(raw)
    except Exception:
        return False
    if value.is_absolute() or ".." in value.parts or not value.parts:
        return False
    folded = "/".join(value.parts).casefold()
    if any(part in folded for part in DENIED_PARTS):
        return False
    return value.name in CODE_BASENAMES or value.suffix.casefold() in CODE_SUFFIXES


def git_bytes(repo, argv, limit):
    result = bounded_process(
        ["git", "-C", str(repo)] + argv,
        None,
        dict(
            os.environ,
            LC_ALL="C", LANG="C", GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UpdateHostKeys=no",
        ),
        60,
        limit,
        512 * 1024,
    )
    if result["returncode"] != 0:
        raise RuntimeError(
            "git command failed:returncode=%s:stderr_sha256=%s"
            % (result["returncode"], sha(result["stderr"]))
        )
    if result["stdout_truncated"]:
        raise RuntimeError("git output exceeded byte cap")
    return result["stdout"]


def tar_add_bytes(archive, name, data, mode=0o600):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def framed_failed_archive(reason, max_bytes):
    manifest = {
        "schema_version": 1,
        "created_at_utc": now(),
        "code_only": True,
        "remote_files_written": False,
        "status": "failed",
        "errors": [scrub(reason)],
        "tracked_paths": [],
        "untracked_members": [],
        "excluded_path_hashes": [],
        "tracked_patch_bytes": 0,
        "tracked_patch_sha256": sha(b""),
        "content_secret_gate": True,
    }
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            tar_add_bytes(
                archive,
                "archive_manifest.json",
                (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
            )
            tar_add_bytes(archive, "tracked_changes.patch", b"")
    payload = buffer.getvalue()
    if len(payload) > max_bytes:
        raise RuntimeError("failed archive framing exceeded byte cap")
    return {
        "status": "failed",
        "tracked_paths": [],
        "untracked_paths": [],
        "excluded_path_hashes": [],
        "errors": [scrub(reason)],
        "archive_bytes": len(payload),
        "archive_sha256": sha(payload),
        "archive_base64": base64.b64encode(payload).decode("ascii"),
    }


def build_private_code_archive(repo, max_bytes):
    result = {
        "status": "not_started", "tracked_paths": [], "untracked_paths": [],
        "excluded_path_hashes": [], "errors": [],
    }
    try:
        tracked_raw = git_bytes(repo, ["diff", "--name-only", "-z", "HEAD", "--"], 8 * 1024 * 1024)
        untracked_raw = git_bytes(repo, ["ls-files", "--others", "--exclude-standard", "-z"], 8 * 1024 * 1024)
        tracked = sorted({p.decode("utf-8", errors="surrogateescape") for p in tracked_raw.split(b"\0") if p})
        untracked = sorted({p.decode("utf-8", errors="surrogateescape") for p in untracked_raw.split(b"\0") if p})
        allowed_tracked = []
        allowed_untracked = []
        for relative in tracked:
            if code_path(relative):
                allowed_tracked.append(relative)
            else:
                result["excluded_path_hashes"].append(excluded_path(relative, "path_policy"))
        for relative in untracked:
            if code_path(relative):
                allowed_untracked.append(relative)
            else:
                result["excluded_path_hashes"].append(excluded_path(relative, "path_policy"))

        patches = []
        archived_tracked = []
        total_input = 0
        for relative in allowed_tracked:
            candidate_patch = git_bytes(
                repo,
                ["diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "HEAD", "--", relative],
                min(max_bytes, MAX_UNTRACKED_FILE_BYTES),
            )
            kinds = secret_kinds(candidate_patch)
            if b"GIT binary patch" in candidate_patch or b"\0" in candidate_patch:
                result["excluded_path_hashes"].append(excluded_path(relative, "binary_patch"))
                continue
            if kinds:
                result["excluded_path_hashes"].append(excluded_path(relative, "content_secret_gate", kinds))
                continue
            if total_input + len(candidate_patch) > max_bytes:
                result["excluded_path_hashes"].append(excluded_path(relative, "archive_input_cap"))
                result["errors"].append("archive_input_cap_exceeded")
                continue
            patches.append(candidate_patch)
            archived_tracked.append(relative)
            total_input += len(candidate_patch)
        patch = b"".join(patches)
        members = []
        untracked_content = []
        repository_real = os.path.realpath(str(repo))
        for relative in allowed_untracked:
            candidate = repo / relative
            candidate_real = os.path.realpath(str(candidate))
            if os.path.commonpath([repository_real, candidate_real]) != repository_real:
                result["excluded_path_hashes"].append(excluded_path(relative, "escaped_repository"))
                continue
            try:
                stat_result = candidate.lstat()
                if not candidate.is_file() or candidate.is_symlink():
                    result["excluded_path_hashes"].append(excluded_path(relative, "not_regular_file"))
                    continue
                if stat_result.st_size > MAX_UNTRACKED_FILE_BYTES:
                    result["excluded_path_hashes"].append(excluded_path(relative, "file_size_cap"))
                    continue
                with candidate.open("rb") as handle:
                    data = handle.read(MAX_UNTRACKED_FILE_BYTES + 1)
            except Exception as exc:
                result["excluded_path_hashes"].append(excluded_path(relative, "read_failed:" + type(exc).__name__))
                continue
            kinds = secret_kinds(data)
            if kinds:
                result["excluded_path_hashes"].append(excluded_path(relative, "content_secret_gate", kinds))
                continue
            total_input += len(data)
            if total_input > max_bytes:
                result["errors"].append("archive_input_cap_exceeded")
                result["excluded_path_hashes"].append(excluded_path(relative, "archive_input_cap"))
                total_input -= len(data)
                continue
            untracked_content.append((relative, data))
            members.append({"path": relative, "bytes": len(data), "sha256": sha(data)})

        exclusions = sorted(
            result["excluded_path_hashes"],
            key=lambda row: (row["path_sha256"], row["reason"]),
        )
        result["excluded_path_hashes"] = exclusions
        manifest = {
            "schema_version": 1,
            "created_at_utc": now(),
            "repository_commit": git_bytes(repo, ["rev-parse", "HEAD"], 1024).decode().strip(),
            "status": "complete" if not result["errors"] else "complete_with_gaps",
            "tracked_paths": archived_tracked,
            "tracked_patch_bytes": len(patch),
            "tracked_patch_sha256": sha(patch),
            "untracked_members": members,
            "excluded_path_hashes": exclusions,
            "errors": result["errors"],
            "code_only": True,
            "content_secret_gate": True,
            "remote_files_written": False,
        }
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                tar_add_bytes(archive, "archive_manifest.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
                tar_add_bytes(archive, "tracked_changes.patch", patch)
                for relative, data in untracked_content:
                    tar_add_bytes(archive, "untracked/" + relative, data)
        payload = buffer.getvalue()
        if len(payload) > max_bytes:
            raise RuntimeError("compressed archive exceeded byte cap")
        result.update({
            "status": "complete" if not result["errors"] else "complete_with_gaps",
            "tracked_paths": archived_tracked,
            "untracked_paths": [path for path, _ in untracked_content],
            "archive_bytes": len(payload),
            "archive_sha256": sha(payload),
            "archive_base64": base64.b64encode(payload).decode("ascii"),
        })
    except Exception as exc:
        return framed_failed_archive(
            "%s: %s" % (type(exc).__name__, scrub(str(exc))), max_bytes,
        )
    return result


def scan_largest_files(roots):
    started = time.monotonic()
    entries = 0
    files = []
    errors = []
    for root in roots:
        if not root.exists() or root.is_symlink():
            continue
        for base, dirs, names in os.walk(str(root), followlinks=False):
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
            for name in names:
                entries += 1
                if entries > MAX_SCAN_ENTRIES or time.monotonic() - started > MAX_SCAN_SECONDS:
                    return {
                        "entries_seen": entries, "truncated": True,
                        "largest_files": sorted(files, reverse=True)[:100], "errors": errors,
                    }
                path = os.path.join(base, name)
                try:
                    if os.path.islink(path):
                        continue
                    size = os.path.getsize(path)
                    files.append((size, path))
                    if len(files) > 1000:
                        files = sorted(files, reverse=True)[:200]
                except Exception as exc:
                    if len(errors) < 100:
                        errors.append("%s:%s" % (path, type(exc).__name__))
    return {
        "entries_seen": entries, "truncated": False,
        "largest_files": [
            {"bytes": size, "path": path} for size, path in sorted(files, reverse=True)[:100]
        ],
        "errors": errors,
    }


def network_interfaces():
    rows = []
    root = Path("/sys/class/net")
    if not root.is_dir():
        return rows
    for interface in sorted(root.iterdir(), key=lambda item: item.name):
        row = {"name": interface.name}
        for relative in (
            "operstate", "carrier", "carrier_changes", "carrier_up_count",
            "carrier_down_count", "dormant", "flags", "mtu", "address",
            "type", "speed", "duplex", "link_mode", "ifalias",
            "device/vendor", "device/device", "device/subsystem_vendor",
            "device/subsystem_device", "device/modalias", "device/uevent",
            "device/power/control", "device/power/runtime_status",
            "device/power/runtime_active_time", "device/power/runtime_suspended_time",
            "device/power/autosuspend_delay_ms",
        ):
            item = read_text(str(interface / relative), 4096)
            if item.get("ok"):
                row[relative.replace("/", "_")] = item["text"].strip()
        try:
            row["device"] = os.path.realpath(str(interface / "device"))
            row["driver"] = os.path.realpath(str(interface / "device/driver"))
        except Exception:
            pass
        rows.append(row)
    return rows


def process_snapshot():
    rows = []
    matching = []
    browser_terms = ("chrome", "chromium", "firefox", "webkit", "playwright", "selenium", "geckodriver")
    campaign_terms = ("scrape", "observe", "country", "adapter", "chromedriver")
    proc = Path("/proc")
    if not proc.is_dir():
        return {"processes": rows, "matching": matching, "file_nr": None, "self_nofile": list(resource.getrlimit(resource.RLIMIT_NOFILE))}
    for entry in sorted(proc.iterdir(), key=lambda item: int(item.name) if item.name.isdigit() else 10**12):
        if not entry.name.isdigit():
            continue
        try:
            status = {}
            for line in (entry / "status").read_text(errors="replace").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    if key in {"Name", "State", "PPid", "Uid", "VmRSS", "Threads"}:
                        status[key] = value.strip()
            raw_cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            cmd = scrub(raw_cmd)
            row = {
                "pid": int(entry.name), "ppid": int(status.get("PPid", "0")),
                "name": status.get("Name", ""), "state": status.get("State", ""),
                "uid": status.get("Uid", "").split()[0] if status.get("Uid") else "",
                "rss": status.get("VmRSS"), "threads": status.get("Threads"),
            }
            rows.append(row)
            folded = (row["name"] + " " + cmd).casefold()
            if any(term in folded for term in browser_terms + campaign_terms):
                try:
                    fd_count = sum(1 for _ in (entry / "fd").iterdir())
                except Exception:
                    fd_count = None
                matching.append(dict(row, command=cmd, open_file_descriptors=fd_count,
                                     orphan_candidate=row["ppid"] in (0, 1) and any(term in folded for term in browser_terms)))
        except Exception:
            continue
    return {
        "processes": rows,
        "matching": matching,
        "file_nr": read_text("/proc/sys/fs/file-nr", 4096),
        "self_nofile": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
    }


def runtime_inventory(repo):
    prefixes = set()
    home = Path.home()
    candidates = [
        home / "anaconda3",
        home / "miniconda3",
        Path(sys.prefix),
    ]
    for base in (home / "anaconda3" / "envs", home / "miniconda3" / "envs"):
        if base.is_dir():
            candidates.extend(item for item in base.iterdir() if item.is_dir())
    env_list = Path.home() / ".conda/environments.txt"
    if env_list.is_file():
        try:
            with env_list.open("r", errors="replace") as handle:
                candidates.extend(Path(line.strip()) for line in handle.read(2 * 1024 * 1024).splitlines() if line.strip())
        except Exception:
            pass
    for prefix in candidates:
        if prefix.is_dir():
            prefixes.add(prefix)
    environments = []
    for prefix in sorted(prefixes, key=str)[:40]:
        packages = []
        conda_meta = prefix / "conda-meta"
        if conda_meta.is_dir():
            for item in sorted(conda_meta.glob("*.json")):
                try:
                    value = json.loads(item.read_text())
                    packages.append({
                        "name": value.get("name"),
                        "version": value.get("version"),
                        "build": value.get("build"),
                        "channel": sanitized_url(str(value.get("channel") or "")),
                    })
                except Exception:
                    continue
        python_packages = []
        metadata_files = []
        for library in sorted((prefix / "lib").glob("python*")):
            metadata_files.extend(sorted((library / "site-packages").glob("*.dist-info/METADATA")))
            metadata_files.extend(sorted((library / "site-packages").glob("*.egg-info/PKG-INFO")))
            metadata_files.extend(sorted((library / "dist-packages").glob("*.dist-info/METADATA")))
            metadata_files.extend(sorted((library / "dist-packages").glob("*.egg-info/PKG-INFO")))
        for metadata in metadata_files[:20000]:
            try:
                name = None
                version = None
                with metadata.open("r", errors="replace") as handle:
                    metadata_text = handle.read(256 * 1024)
                for line in metadata_text.splitlines():
                    if line.startswith("Name: ") and name is None:
                        name = line[6:].strip()
                    elif line.startswith("Version: ") and version is None:
                        version = line[9:].strip()
                    if name is not None and version is not None:
                        break
                if name or version:
                    python_packages.append({"name": name, "version": version})
            except Exception:
                continue
        unique_python_packages = []
        seen_python_packages = set()
        for package in sorted(python_packages, key=lambda row: ((row.get("name") or "").casefold(), row.get("version") or "")):
            key = (package.get("name"), package.get("version"))
            if key not in seen_python_packages:
                seen_python_packages.add(key)
                unique_python_packages.append(package)
        python = prefix / "bin/python"
        python_version = command("python_version", [str(python), "--version"], timeout=10, limit=8192) if python.is_file() else None
        environments.append({
            "prefix": str(prefix),
            "python": python_version,
            "conda_packages": packages,
            "conda_package_count": len(packages),
            "python_packages": unique_python_packages,
            "python_package_count": len(unique_python_packages),
        })
    locks = []
    for name in (
        "requirements.txt", "requirements-dev.txt", "environment.yml", "environment.yaml",
        "pyproject.toml", "poetry.lock", "Pipfile", "Pipfile.lock",
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    ):
        path = repo / name
        if path.is_file() and not path.is_symlink():
            size, digest = sha_file(path)
            locks.append({"path": name, "bytes": size, "sha256": digest})
    binaries = []
    for name in ("google-chrome", "chromium", "chromium-browser", "chromedriver", "firefox", "geckodriver", "node", "npm"):
        executable = shutil.which(name)
        if executable:
            binaries.append(command(name + "_version", [executable, "--version"], timeout=10, limit=8192))
    return {"environments": environments, "lockfiles": locks, "binaries": binaries}


def parse_github_repository(raw):
    value = raw.strip()
    scp = re.match(r"^(?:[^@\s]+)@([^:\s]+):([^?#]+)$", value)
    if scp:
        host = scp.group(1).casefold()
        path = scp.group(2)
    else:
        try:
            parts = urlsplit(value)
        except Exception:
            return None
        host = (parts.hostname or "").casefold()
        path = parts.path.lstrip("/")
    if host != "github.com":
        return None
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        return None
    return path


def git_status_summary(repo):
    groups = {}
    for name, argv in (
        ("tracked", ["diff", "--name-only", "-z", "HEAD", "--"]),
        ("staged", ["diff", "--cached", "--name-only", "-z", "HEAD", "--"]),
        ("untracked", ["ls-files", "--others", "--exclude-standard", "-z"]),
    ):
        raw = git_bytes(repo, argv, 8 * 1024 * 1024)
        values = sorted({item for item in raw.split(b"\0") if item})
        groups[name] = {
            "count": len(values),
            "path_set_sha256": sha(b"\0".join(values)),
            "code_path_count": sum(
                1 for item in values
                if code_path(item.decode("utf-8", errors="surrogateescape"))
            ),
        }
    return {
        "name": "git_status",
        "available": True,
        "returncode": 0,
        "stdout": json.dumps(groups, sort_keys=True, separators=(",", ":")),
        "stderr": "",
        "path_names_emitted": False,
    }


def git_snapshot(repo, expected_login, expected_repository, expected_branch):
    rows = {}
    try:
        rows["status"] = git_status_summary(repo)
    except Exception as exc:
        rows["status"] = {
            "name": "git_status", "available": True, "returncode": 1,
            "stdout": "", "stderr": scrub("%s: %s" % (type(exc).__name__, exc)),
            "path_names_emitted": False,
        }
    rows["commit"] = command("git_commit", ["git", "rev-parse", "HEAD"], cwd=str(repo), limit=8192)
    rows["branch"] = command("git_branch", ["git", "symbolic-ref", "--short", "HEAD"], cwd=str(repo), limit=8192)
    rows["upstream"] = command("git_upstream", ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd=str(repo), limit=8192)
    origin = command("git_origin", ["git", "remote", "get-url", "origin"], cwd=str(repo), limit=65536)
    if origin.get("stdout"):
        origin["stdout"] = sanitized_url(origin["stdout"])
    rows["origin"] = origin
    rows["remote_main"] = command("git_remote_main", ["git", "ls-remote", "origin", "refs/heads/main"], cwd=str(repo), timeout=60, limit=65536)

    gh = shutil.which("gh")
    if gh:
        rows["github_login"] = command("github_login", [gh, "api", "user", "--jq", ".login"], timeout=45, limit=8192)
        rows["github_repository"] = command(
            "github_repository",
            [gh, "api", "repos/" + expected_repository, "--jq", "{full_name:.full_name,default_branch:.default_branch,push:.permissions.push}"],
            timeout=45, limit=65536,
        )
    else:
        rows["github_login"] = {"name": "github_login", "available": False, "returncode": None, "stdout": "", "stderr": "gh_not_found"}
        rows["github_repository"] = {"name": "github_repository", "available": False, "returncode": None, "stdout": "", "stderr": "gh_not_found"}

    login = rows["github_login"].get("stdout", "").strip()
    branch = rows["branch"].get("stdout", "").strip()
    origin_value = rows["origin"].get("stdout", "").strip()
    parsed_origin = parse_github_repository(origin_value)
    origin_expected = (
        parsed_origin is not None
        and parsed_origin.casefold() == expected_repository.casefold()
    )
    identity_exact = login == expected_login and branch == expected_branch and origin_expected
    if identity_exact:
        rows["direct_push_dry_run"] = command(
            "direct_push_dry_run",
            [
                "git", "-c", "core.hooksPath=/dev/null",
                "-c", "credential.helper=", "push", "--porcelain",
                "--dry-run", "--no-verify", "origin",
                "HEAD:refs/heads/" + expected_branch,
            ],
            cwd=str(repo), timeout=90, limit=65536,
        )
    else:
        rows["direct_push_dry_run"] = {
            "name": "direct_push_dry_run", "available": True, "returncode": None,
            "stdout": "", "stderr": "skipped_identity_or_checkout_mismatch",
        }
    rows["assessment"] = {
        "expected_login": expected_login,
        "expected_repository": expected_repository,
        "expected_branch": expected_branch,
        "login_exact": login == expected_login,
        "origin_exact": origin_expected,
        "parsed_origin_repository": parsed_origin,
        "branch_exact": branch == expected_branch,
        "dry_run_ok": rows["direct_push_dry_run"].get("returncode") == 0,
        "remote_mutation_attempted": False,
    }
    return rows


def main():
    args = parse_args()
    operation_root = Path(args.repository)
    git_root, git_root_resolution = resolve_git_root(operation_root)
    since_journal = args.since_utc.replace("T", " ").replace("Z", " UTC")
    evidence = {}

    evidence["boot_shutdown"] = [
        command("last_boot_shutdown", ["last", "-x", "-F", "-n", "200"]),
        command("last_boot", ["who", "-b"]),
        command("journal_boots", ["journalctl", "--list-boots", "--no-pager"]),
    ]
    evidence["previous_boot_journal"] = [
        command("previous_boot_journal", ["journalctl", "-b", "-1", "--no-pager", "-o", "short-iso", "-n", "12000"], timeout=120),
    ]
    evidence["kernel_oom"] = [
        command("previous_kernel", ["journalctl", "-k", "-b", "-1", "--no-pager", "-o", "short-iso", "-n", "8000"], timeout=120),
        command("current_kernel", ["journalctl", "-k", "-b", "0", "--no-pager", "-o", "short-iso", "-n", "4000"], timeout=90),
        command("previous_kernel_network_power", ["journalctl", "-k", "-b", "-1", "--grep", "network|ether|link|carrier|e1000|igc|iwl|r8169|suspend|resume|sleep|hibernate|PM:|ACPI|OOM|out of memory|killed process", "--no-pager", "-o", "short-iso", "-n", "8000"], timeout=120),
        command("current_kernel_network_power", ["journalctl", "-k", "-b", "0", "--grep", "network|ether|link|carrier|e1000|igc|iwl|r8169|suspend|resume|sleep|hibernate|PM:|ACPI|OOM|out of memory|killed process", "--no-pager", "-o", "short-iso", "-n", "8000"], timeout=120),
        command("dmesg_warnings", ["dmesg", "--ctime", "--level=emerg,alert,crit,err,warn"], timeout=45),
    ]
    evidence["ssh_auth"] = [
        command("ssh_service", ["systemctl", "status", "ssh", "sshd", "--no-pager"], timeout=30),
        command("ssh_journal", ["journalctl", "-u", "ssh", "-u", "sshd", "--since", since_journal, "--no-pager", "-o", "short-iso", "-n", "12000"], timeout=120),
    ]
    evidence["network"] = [
        command("ip_link", ["ip", "-details", "-statistics", "link", "show"]),
        command("ip_address", ["ip", "address", "show"]),
        command("ip_route", ["ip", "route", "show", "table", "all"]),
        command("ip_rule", ["ip", "rule", "show"]),
        command("ip_neighbour", ["ip", "neighbour", "show"]),
        command("networkctl", ["networkctl", "status", "--all", "--no-pager"], timeout=45),
        command("nmcli_device", ["nmcli", "-t", "-f", "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION", "device", "show"], timeout=30),
        command("nmcli_general", ["nmcli", "-t", "-f", "STATE,CONNECTIVITY,NETWORKING,WIFI-HW,WIFI,WWAN-HW,WWAN", "general", "status"], timeout=30),
        command("nmcli_networking", ["nmcli", "networking"], timeout=30),
        command("nmcli_radio", ["nmcli", "radio", "all"], timeout=30),
        command("nmcli_connections_autoconnect", ["nmcli", "-t", "-f", "connection.id,connection.uuid,connection.type,connection.interface-name,connection.autoconnect,connection.autoconnect-priority", "connection", "show"], timeout=30),
        command("rfkill", ["rfkill", "list"]),
        command("resolvectl", ["resolvectl", "status"], timeout=30),
        command("network_services", ["systemctl", "show", "NetworkManager.service", "systemd-networkd.service", "systemd-resolved.service", "-p", "Id", "-p", "LoadState", "-p", "ActiveState", "-p", "SubState", "-p", "Result", "-p", "NRestarts", "-p", "ExecMainStatus", "-p", "ExecMainStartTimestamp", "-p", "ActiveEnterTimestamp", "-p", "InactiveExitTimestamp", "--no-pager"], timeout=30),
        command("network_enabled", ["systemctl", "is-enabled", "NetworkManager.service", "systemd-networkd.service", "systemd-resolved.service"], timeout=30),
        command("network_journal_since", ["journalctl", "-u", "NetworkManager", "-u", "systemd-networkd", "-u", "systemd-resolved", "--since", since_journal, "--no-pager", "-o", "short-iso", "-n", "12000"], timeout=120),
        command("network_journal_previous_boot", ["journalctl", "-b", "-1", "-u", "NetworkManager", "-u", "systemd-networkd", "-u", "systemd-resolved", "--no-pager", "-o", "short-iso", "-n", "12000"], timeout=120),
        command("network_journal_current_boot", ["journalctl", "-b", "0", "-u", "NetworkManager", "-u", "systemd-networkd", "-u", "systemd-resolved", "--no-pager", "-o", "short-iso", "-n", "12000"], timeout=120),
        command("network_suspend_since", ["journalctl", "--since", since_journal, "--grep", "suspend|resume|sleep|hibernate|NetworkManager|networkd|link.*down|link.*up|carrier|rfkill", "--no-pager", "-o", "short-iso", "-n", "12000"], timeout=120),
        command("networkmanager_state", ["cat", "/var/lib/NetworkManager/NetworkManager.state"], timeout=20, limit=64 * 1024),
        command("pci_drivers", ["lspci", "-nnk"], timeout=30, limit=1024 * 1024),
        command("power_profile", ["powerprofilesctl", "get"], timeout=20, limit=64 * 1024),
        command("power_profiles", ["powerprofilesctl", "list"], timeout=20, limit=256 * 1024),
        command("tlp_status", ["tlp-stat", "-s"], timeout=30, limit=256 * 1024),
        command("tlp_pcie", ["tlp-stat", "-e"], timeout=30, limit=512 * 1024),
        command("tlp_service", ["systemctl", "status", "tlp.service", "--no-pager"], timeout=30, limit=256 * 1024),
        command("ssh_listener_22", ["ss", "-lntp", "sport", "=", ":22"], timeout=20, limit=256 * 1024),
    ]
    interfaces = network_interfaces()
    interface_tools = []
    ethtool = shutil.which("ethtool")
    if ethtool:
        for row in interfaces:
            if row.get("name") != "lo":
                interface_tools.append(command("ethtool_" + row["name"], [ethtool, row["name"]], timeout=20, limit=256 * 1024))
                interface_tools.append(command("ethtool_driver_" + row["name"], [ethtool, "-i", row["name"]], timeout=20, limit=256 * 1024))
                interface_tools.append(command("ethtool_stats_" + row["name"], [ethtool, "-S", row["name"]], timeout=30, limit=512 * 1024))
                interface_tools.append(command("ethtool_eee_" + row["name"], [ethtool, "--show-eee", row["name"]], timeout=20, limit=256 * 1024))
    default_route = command("default_gateway", ["ip", "-4", "route", "show", "default"], timeout=20, limit=64 * 1024)
    gateway_test = {
        "name": "gateway_ping", "available": False, "returncode": None,
        "stdout": "", "stderr": "default_gateway_unavailable",
    }
    if command_ok(default_route):
        match = re.search(r"(?:^|\s)via\s+([0-9.]+)(?:\s|$)", default_route.get("stdout", ""))
        if match:
            gateway_test = command(
                "gateway_ping", ["ping", "-n", "-c", "1", "-W", "2", match.group(1)],
                timeout=5, limit=64 * 1024,
            )
    evidence["network_interfaces"] = {
        "sysfs": interfaces,
        "tools": interface_tools,
        "default_route": default_route,
        "bounded_gateway_test": gateway_test,
        "gateway_packets_requested": 1,
        "power_state": read_text("/sys/power/state", 64 * 1024),
        "memory_sleep_state": read_text("/sys/power/mem_sleep", 64 * 1024),
    }

    evidence["disk_inode_filesystem"] = [
        command("df_space", ["df", "-hPT"]),
        command("df_inodes", ["df", "-iPT"]),
        command("findmnt", ["findmnt", "--real", "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"]),
        command("lsblk", ["lsblk", "-e", "7", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,ROTA,RO,MODEL"]),
        command("smart_scan", ["smartctl", "--scan-open"], timeout=30),
        command("nvme_list", ["nvme", "list"], timeout=30),
    ]
    evidence["memory_swap_load"] = [
        command("free", ["free", "-b"]),
        command("vmstat", ["vmstat", "-s"]),
        command("swapon", ["swapon", "--show", "--bytes"]),
        command("uptime", ["uptime"]),
        command("nproc", ["nproc", "--all"]),
    ]
    evidence["proc_meminfo"] = read_text("/proc/meminfo", 1024 * 1024)
    evidence["proc_loadavg"] = read_text("/proc/loadavg", 4096)
    evidence["normalized_load"] = normalized_load_snapshot()
    evidence["process_open_files"] = process_snapshot()

    roots = [
        operation_root / "logs", operation_root / "runs",
        operation_root / "var", operation_root / "reports",
    ]
    evidence["logs_and_run_roots"] = {
        "largest_files": scan_largest_files(roots),
        "root_sizes": [
            command("du_" + root.name, ["du", "-x", "-B1", "-d", "2", str(root)], timeout=90)
            for root in roots if root.exists()
        ],
    }
    if git_root is not None:
        evidence["git_checkout"] = git_snapshot(
            git_root, args.expected_login, args.expected_repository, args.expected_branch,
        )
        evidence["git_checkout"]["top_level_resolution"] = git_root_resolution
        evidence["git_checkout"]["resolved_top_level"] = str(git_root)
        private_archive = build_private_code_archive(git_root, args.max_archive_bytes)
        runtime_lock_root = git_root
    else:
        evidence["git_checkout"] = {
            "top_level_resolution": git_root_resolution,
            "resolved_top_level": None,
            "assessment": {
                "expected_login": args.expected_login,
                "expected_repository": args.expected_repository,
                "expected_branch": args.expected_branch,
                "login_exact": False,
                "origin_exact": False,
                "branch_exact": False,
                "dry_run_ok": False,
                "remote_mutation_attempted": False,
                "error": "git_top_level_unavailable",
            },
        }
        private_archive = framed_failed_archive(
            "git_top_level_unavailable", args.max_archive_bytes,
        )
        runtime_lock_root = operation_root
    evidence["runtime_environments"] = runtime_inventory(runtime_lock_root)

    status = {}
    status["boot_shutdown"] = (
        command_ok(evidence["boot_shutdown"][0])
        and command_ok(evidence["boot_shutdown"][2])
    )
    status["previous_boot_journal"] = command_ok(evidence["previous_boot_journal"][0])
    status["kernel_oom"] = group_ok(evidence["kernel_oom"])
    status["ssh_auth"] = command_ok(evidence["ssh_auth"][1])
    network_named = {row.get("name"): row for row in evidence["network"]}
    status["network"] = (
        command_ok(network_named.get("network_journal_since", {}))
        and command_ok(network_named.get("network_services", {}), require_output=False)
        and command_ok(default_route)
        and gateway_test.get("available") is True
        and gateway_test.get("returncode") is not None
        and bool(interfaces)
    )
    status["disk_inode_filesystem"] = all(
        command_ok(row) for row in evidence["disk_inode_filesystem"][:4]
    )
    status["memory_swap_load"] = (
        command_ok(evidence["memory_swap_load"][0])
        and command_ok(evidence["memory_swap_load"][2], require_output=False)
        and command_ok(evidence["memory_swap_load"][3])
        and evidence["proc_meminfo"].get("ok", False)
        and evidence["proc_loadavg"].get("ok", False)
        and evidence["normalized_load"].get("ok", False)
    )
    process_evidence = evidence["process_open_files"]
    status["process_open_files"] = (
        bool(process_evidence.get("processes"))
        and bool(process_evidence.get("file_nr", {}).get("ok"))
        and len(process_evidence.get("self_nofile", [])) == 2
    )
    root_evidence = evidence["logs_and_run_roots"]
    status["logs_and_run_roots"] = (
        bool(root_evidence.get("root_sizes"))
        and any(command_ok(row) for row in root_evidence.get("root_sizes", []))
        and isinstance(root_evidence.get("largest_files"), dict)
    )
    git_assessment = evidence["git_checkout"].get("assessment", {})
    status["git_checkout"] = (
        git_root is not None
        and evidence["git_checkout"].get("status", {}).get("returncode") == 0
        and evidence["git_checkout"].get("commit", {}).get("returncode") == 0
    )
    status["private_code_archive"] = private_archive.get("status") == "complete"
    status["runtime_environments"] = bool(evidence["runtime_environments"].get("environments"))
    status["github_provenance"] = all(
        git_assessment.get(key) is True
        for key in ("login_exact", "origin_exact", "branch_exact", "dry_run_ok")
    )
    missing = sorted(key for key, ok in status.items() if not ok)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "capture_id": "host-read-only-recovery",
        "captured_at_utc": now(),
        "read_only": True,
        "remote_files_written": False,
        "scrapers_started": False,
        "campaigns_started": False,
        "production_writes": False,
        "required_group_status": status,
        "missing_required_groups": missing,
        "status": "captured" if not missing else "captured_with_gaps",
        "evidence": evidence,
        "private_code_archive": private_archive,
    }
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(rendered.encode("utf-8")) > args.max_response_bytes:
        raise RuntimeError("serialized response exceeded byte cap")
    print(rendered)


if __name__ == "__main__":
    main()
'''


@dataclass(frozen=True)
class CaptureResult:
    exit_code: int
    status: str
    output_dir: Path
    receipt: Mapping[str, object]


Runner = Callable[..., object]


def _validate_member_name(name: str) -> None:
    value = PurePosixPath(name)
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise ValueError("private archive contains an unsafe member name")


def _validate_private_archive(decoded: bytes, outer: Mapping[str, object]) -> None:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(decoded), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("private code archive is not a valid gzip tar") from exc
    extracted: dict[str, bytes] = {}
    total = 0
    with archive:
        for member in archive.getmembers():
            _validate_member_name(member.name)
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError("private archive contains a non-regular member")
            if member.name in extracted:
                raise ValueError("private archive contains duplicate members")
            total += member.size
            if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError("private archive uncompressed size exceeds cap")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError("private archive member cannot be read")
            data = handle.read(member.size + 1)
            if len(data) != member.size:
                raise ValueError("private archive member size mismatch")
            extracted[member.name] = data

    required = {"archive_manifest.json", "tracked_changes.patch"}
    if not required.issubset(extracted):
        raise ValueError("private archive required members are missing")
    if local_secret_kinds(extracted["archive_manifest.json"]):
        raise ValueError("private archive manifest failed local secret gate")
    try:
        manifest = json.loads(extracted["archive_manifest.json"].decode("utf-8"))
    except Exception as exc:
        raise ValueError("private archive manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("private archive manifest schema mismatch")
    if manifest.get("code_only") is not True or manifest.get("remote_files_written") is not False:
        raise ValueError("private archive manifest invariant failed")
    if manifest.get("content_secret_gate") is not True:
        raise ValueError("private archive content secret gate is absent")

    tracked_paths = manifest.get("tracked_paths")
    untracked_members = manifest.get("untracked_members")
    exclusions = manifest.get("excluded_path_hashes")
    errors = manifest.get("errors")
    if not isinstance(tracked_paths, list) or not all(isinstance(item, str) for item in tracked_paths):
        raise ValueError("private archive tracked path list is invalid")
    if not isinstance(untracked_members, list) or not all(isinstance(item, dict) for item in untracked_members):
        raise ValueError("private archive untracked manifest is invalid")
    if not isinstance(exclusions, list) or not all(isinstance(item, dict) for item in exclusions):
        raise ValueError("private archive exclusion ledger is invalid")
    if not isinstance(errors, list) or not all(isinstance(item, str) for item in errors):
        raise ValueError("private archive error ledger is invalid")
    for row in exclusions:
        if set(row) - {"path_sha256", "reason", "secret_types"}:
            raise ValueError("private archive exclusion ledger contains a path field")
        if not isinstance(row.get("path_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["path_sha256"]):
            raise ValueError("private archive exclusion hash is invalid")
        if not isinstance(row.get("reason"), str):
            raise ValueError("private archive exclusion reason is invalid")

    patch = extracted["tracked_changes.patch"]
    if manifest.get("tracked_patch_bytes") != len(patch):
        raise ValueError("private archive patch byte count mismatch")
    if manifest.get("tracked_patch_sha256") != sha256_bytes(patch):
        raise ValueError("private archive patch hash mismatch")
    if local_secret_kinds(patch):
        raise ValueError("private archive patch failed local secret gate")

    expected_members = set(required)
    outer_untracked: list[str] = []
    for row in untracked_members:
        path = row.get("path")
        if not isinstance(path, str):
            raise ValueError("private archive untracked path is invalid")
        _validate_member_name(path)
        member_name = "untracked/" + path
        expected_members.add(member_name)
        data = extracted.get(member_name)
        if data is None:
            raise ValueError("private archive untracked member is missing")
        if row.get("bytes") != len(data) or row.get("sha256") != sha256_bytes(data):
            raise ValueError("private archive untracked member hash mismatch")
        if local_secret_kinds(data):
            raise ValueError("private archive untracked member failed local secret gate")
        outer_untracked.append(path)
    if set(extracted) != expected_members:
        raise ValueError("private archive member set does not match the manifest")

    comparisons = (
        (outer.get("status"), manifest.get("status"), "status"),
        (outer.get("tracked_paths"), tracked_paths, "tracked paths"),
        (outer.get("untracked_paths"), outer_untracked, "untracked paths"),
        (outer.get("excluded_path_hashes"), exclusions, "exclusion ledger"),
        (outer.get("errors"), errors, "error ledger"),
    )
    for observed, expected, label in comparisons:
        if observed != expected:
            raise ValueError(f"private archive {label} mismatch")


def validate_remote_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("remote response is not an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("remote schema version mismatch")
    if payload.get("capture_id") != CAPTURE_ID:
        raise ValueError("remote capture id mismatch")
    invariants = {
        "read_only": True,
        "remote_files_written": False,
        "scrapers_started": False,
        "campaigns_started": False,
        "production_writes": False,
    }
    for key, expected in invariants.items():
        if payload.get(key) is not expected:
            raise ValueError(f"remote invariant failed: {key}")
    groups = payload.get("required_group_status")
    if not isinstance(groups, dict) or set(groups) != set(REQUIRED_EVIDENCE_GROUPS):
        raise ValueError("remote required evidence group set mismatch")
    if any(type(value) is not bool for value in groups.values()):
        raise ValueError("remote evidence group status is not boolean")
    expected_missing = sorted(name for name, value in groups.items() if value is False)
    missing = payload.get("missing_required_groups")
    if missing != expected_missing:
        raise ValueError("remote missing evidence groups are inconsistent")
    remote_status = payload.get("status")
    expected_status = "captured" if not expected_missing else "captured_with_gaps"
    if remote_status != expected_status:
        raise ValueError("remote capture status is inconsistent")
    archive = payload.get("private_code_archive")
    if not isinstance(archive, dict):
        raise ValueError("private code archive metadata is missing")
    archive_b64 = archive.get("archive_base64")
    archive_hash = archive.get("archive_sha256")
    archive_bytes = archive.get("archive_bytes")
    if not isinstance(archive_b64, str) or not isinstance(archive_hash, str) or not isinstance(archive_bytes, int):
        raise ValueError("private code archive framing is incomplete")
    if archive_bytes < 0 or archive_bytes > MAX_ARCHIVE_BYTES:
        raise ValueError("private code archive size is outside the cap")
    try:
        decoded = base64.b64decode(archive_b64, validate=True)
    except Exception as exc:
        raise ValueError("private code archive base64 is invalid") from exc
    if len(decoded) != archive_bytes or sha256_bytes(decoded) != archive_hash:
        raise ValueError("private code archive hash or byte count mismatch")
    _validate_private_archive(decoded, archive)
    return payload


def materialize_payload(output_dir: Path, payload: dict[str, object]) -> dict[str, object]:
    archive = dict(payload["private_code_archive"])  # type: ignore[arg-type]
    archive_data = base64.b64decode(str(archive.pop("archive_base64")), validate=True)
    archive_path = output_dir / "modified_untracked_code.private.tar.gz"
    atomic_private_write(archive_path, archive_data)
    archive["local_filename"] = archive_path.name
    archive["private_permissions"] = "0600"
    clean_payload = dict(payload)
    clean_payload["private_code_archive"] = archive
    atomic_private_json(output_dir / "recovery_evidence.private.json", clean_payload)
    return clean_payload


def capture_once(
    *,
    output_dir: Path,
    host: str,
    ssh_user: str,
    remote_repository: str,
    since_utc: str,
    runner: Runner = run_process_bounded,
    private_root: Path = DEFAULT_PRIVATE_OUTPUT_ROOT,
) -> CaptureResult:
    """Perform the single SSH attempt and materialize private local evidence."""
    validate_since_utc(since_utc)
    validate_ssh_host(host)
    validate_ssh_user(ssh_user)
    validate_remote_repository(remote_repository)
    output_dir = validate_private_output_dir(output_dir, private_root=private_root)
    output_dir.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    os.chmod(output_dir, PRIVATE_DIRECTORY_MODE)
    marker_path = output_dir / "attempt.private.json"
    marker = {
        "schema_version": SCHEMA_VERSION,
        "capture_id": CAPTURE_ID,
        "started_at_utc": utc_now(),
        "target": host,
        "ssh_user": ssh_user,
        "ssh_sessions_allowed": 1,
        "retry_allowed": False,
        "read_only": True,
        "remote_program_sha256": sha256_bytes(REMOTE_PROGRAM.encode("utf-8")),
    }
    atomic_private_json(marker_path, marker)

    command = build_ssh_command(
        ssh_user, host, remote_repository, since_utc,
    )
    started_at = utc_now()
    try:
        completed = runner(
            command,
            input=REMOTE_PROGRAM.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SSH_TIMEOUT_SECONDS,
            check=False,
        )
        completed_stdout = getattr(completed, "stdout", b"")
        completed_stderr = getattr(completed, "stderr", b"")
        stdout = completed_stdout if isinstance(completed_stdout, bytes) else str(completed_stdout or "").encode()
        stderr = completed_stderr if isinstance(completed_stderr, bytes) else str(completed_stderr or "").encode()
        returncode = int(getattr(completed, "returncode"))
        timed_out = bool(getattr(completed, "timed_out", False))
        stdout_overflow = bool(getattr(completed, "stdout_overflow", False))
        stderr_overflow = bool(getattr(completed, "stderr_overflow", False))
        stdout_bytes_seen = int(getattr(completed, "stdout_bytes_seen", len(stdout)))
        stderr_bytes_seen = int(getattr(completed, "stderr_bytes_seen", len(stderr)))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        returncode = 124
        timed_out = True
        stdout_overflow = len(stdout) > MAX_RESPONSE_BYTES
        stderr_overflow = len(stderr) > MAX_STDERR_BYTES
        stdout_bytes_seen = len(stdout)
        stderr_bytes_seen = len(stderr)

    stderr_clean = scrub_local_stderr(stderr).encode("utf-8")
    atomic_private_write(output_dir / "ssh_stderr.private.txt", stderr_clean)
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "capture_id": CAPTURE_ID,
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "ssh_sessions": 1,
        "retry_attempted": False,
        "target": host,
        "read_only": True,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_bytes": stdout_bytes_seen,
        "stdout_captured_bytes": len(stdout),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_bytes": stderr_bytes_seen,
        "stderr_captured_bytes": len(stderr_clean),
        "stderr_sha256": sha256_bytes(stderr_clean),
        "stdout_overflow": stdout_overflow,
        "stderr_overflow": stderr_overflow,
        "remote_program_sha256": marker["remote_program_sha256"],
    }

    if stdout_overflow or len(stdout) > MAX_RESPONSE_BYTES:
        receipt.update({"status": "failed", "error": "response_size_cap_exceeded"})
        atomic_private_json(output_dir / "connection_receipt.private.json", receipt)
        return CaptureResult(1, "failed", output_dir, receipt)
    if stderr_overflow:
        receipt.update({"status": "failed", "error": "stderr_size_cap_exceeded"})
        atomic_private_json(output_dir / "connection_receipt.private.json", receipt)
        return CaptureResult(1, "failed", output_dir, receipt)
    if returncode != 0:
        receipt.update({"status": "failed", "error": "ssh_or_remote_program_failed"})
        atomic_private_json(output_dir / "connection_receipt.private.json", receipt)
        return CaptureResult(1, "failed", output_dir, receipt)
    try:
        payload = validate_remote_payload(json.loads(stdout.decode("utf-8")))
        clean_payload = materialize_payload(output_dir, payload)
    except Exception as exc:
        receipt.update({
            "status": "failed",
            "error": "response_validation_failed",
            "error_type": type(exc).__name__,
            "error_message": scrub_local_stderr(str(exc).encode()),
        })
        atomic_private_json(output_dir / "connection_receipt.private.json", receipt)
        return CaptureResult(1, "failed", output_dir, receipt)

    remote_status = str(clean_payload.get("status"))
    missing = clean_payload.get("missing_required_groups")
    receipt.update({
        "status": remote_status,
        "missing_required_groups": missing if isinstance(missing, list) else [],
        "evidence_sha256": sha256_file(output_dir / "recovery_evidence.private.json"),
        "archive_sha256": sha256_file(output_dir / "modified_untracked_code.private.tar.gz"),
        "private_permissions": "0700 directory; 0600 files",
    })
    atomic_private_json(output_dir / "connection_receipt.private.json", receipt)
    exit_code = 0 if remote_status == "captured" else 2
    return CaptureResult(exit_code, remote_status, output_dir, receipt)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--output-dir", type=Path, required=True)
    value.add_argument("--host", type=validate_ssh_host, required=True)
    value.add_argument("--ssh-user", type=validate_ssh_user, required=True)
    value.add_argument(
        "--remote-repository", type=validate_remote_repository, required=True,
    )
    value.add_argument("--since-utc", type=validate_since_utc, required=True)
    value.add_argument(
        "--print-receipt",
        action="store_true",
        help="print only the sanitized connection receipt; private evidence is never printed",
    )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = capture_once(
            output_dir=args.output_dir,
            host=args.host,
            ssh_user=args.ssh_user,
            remote_repository=args.remote_repository,
            since_utc=args.since_utc,
        )
    except (FileExistsError, ValueError, OSError) as exc:
        print(f"preflight_failed:{type(exc).__name__}", file=sys.stderr)
        return 3
    if args.print_receipt:
        print(json.dumps(result.receipt, indent=2, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
