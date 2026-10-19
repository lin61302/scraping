from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest


AUTOMATION = Path(__file__).resolve().parents[1]
if str(AUTOMATION) not in sys.path:
    sys.path.insert(0, str(AUTOMATION))

from ops.capture_host_recovery_once import (  # noqa: E402
    CAPTURE_ID,
    MAX_ARCHIVE_BYTES,
    MAX_RESPONSE_BYTES,
    REMOTE_PROGRAM,
    REQUIRED_EVIDENCE_GROUPS,
    atomic_private_write,
    build_ssh_command,
    capture_once,
    local_secret_kinds,
    run_process_bounded,
    parser as build_parser,
    scrub_local_stderr,
    sha256_bytes,
    validate_private_output_dir,
    validate_remote_payload,
)


TEST_HOST = "recovery.example.test"
TEST_SSH_USER = "operator"
TEST_REMOTE_REPOSITORY = "/srv/recovery/repository"
TEST_SINCE_UTC = "2026-01-01T00:00:00Z"


def adversarial_fixtures() -> dict[str, str]:
    """Assemble scanner test values without embedding prohibited literals."""
    github_value = "github" + "_pat_" + "abcdefghijklmnopqrstuvwxyz"
    short_github_value = "gh" + "p_" + "abcdefghijklmnop"
    aws_value = "A" + "KIA" + "ABCDEFGHIJKLMNOP"
    slack_value = "xo" + "xb-" + "1234567890-abcdefghijklmnop"
    jwt_value = "ey" + "Jabcdefghijk.abcdefghijk.abcdefghijk"
    pem_begin = "-" * 5 + "BEGIN " + "RSA " + "PRIVATE KEY" + "-" * 5
    pem_end = "-" * 5 + "END " + "RSA " + "PRIVATE KEY" + "-" * 5
    credentialed_https = "https://" + "alice" + ":" + "secret" + "@example.test/repo"
    credentialed_mongodb = "mongodb://" + "bob" + ":" + "dbsecret" + "@db.example/test"
    return {
        "github": github_value,
        "github_short": short_github_value,
        "aws": aws_value,
        "slack": slack_value,
        "jwt": jwt_value,
        "pem": pem_begin + "\nprivatebytes\n" + pem_end,
        "https": credentialed_https,
        "mongodb": credentialed_mongodb,
        "basic": "dXNl" + "cjpwYXNz",
    }


def tiny_archive(*, status: str = "complete") -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            manifest = {
                "schema_version": 1,
                "status": status,
                "code_only": True,
                "content_secret_gate": True,
                "remote_files_written": False,
                "tracked_paths": [],
                "tracked_patch_bytes": 0,
                "tracked_patch_sha256": hashlib.sha256(b"").hexdigest(),
                "untracked_members": [],
                "excluded_path_hashes": [],
                "errors": [],
            }
            for name, data in (
                ("archive_manifest.json", (json.dumps(manifest) + "\n").encode()),
                ("tracked_changes.patch", b""),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def valid_payload(*, missing: tuple[str, ...] = ()) -> dict[str, object]:
    archive = tiny_archive()
    groups = {name: name not in missing for name in REQUIRED_EVIDENCE_GROUPS}
    return {
        "schema_version": 1,
        "capture_id": CAPTURE_ID,
        "read_only": True,
        "remote_files_written": False,
        "scrapers_started": False,
        "campaigns_started": False,
        "production_writes": False,
        "required_group_status": groups,
        "missing_required_groups": list(missing),
        "status": "captured" if not missing else "captured_with_gaps",
        "evidence": {},
        "private_code_archive": {
            "status": "complete",
            "tracked_paths": [],
            "untracked_paths": [],
            "excluded_path_hashes": [],
            "errors": [],
            "archive_bytes": len(archive),
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "archive_base64": base64.b64encode(archive).decode("ascii"),
        },
    }


class CaptureHostRecoveryTests(unittest.TestCase):
    @staticmethod
    def remote_namespace() -> dict[str, object]:
        namespace: dict[str, object] = {"__name__": "host_recovery_test"}
        exec(compile(REMOTE_PROGRAM, "<host-recovery>", "exec"), namespace)
        return namespace

    @staticmethod
    def connection_kwargs() -> dict[str, str]:
        return {
            "host": TEST_HOST,
            "ssh_user": TEST_SSH_USER,
            "remote_repository": TEST_REMOTE_REPOSITORY,
            "since_utc": TEST_SINCE_UTC,
        }

    def test_command_pins_one_host_one_attempt_and_read_only_program(self):
        command = build_ssh_command(
            TEST_SSH_USER, TEST_HOST, TEST_REMOTE_REPOSITORY, TEST_SINCE_UTC,
        )
        self.assertEqual(command[0], "ssh")
        self.assertEqual(command.count("ssh"), 1)
        self.assertIn("ConnectionAttempts=1", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn(TEST_SSH_USER + "@" + TEST_HOST, command)
        self.assertIn(TEST_REMOTE_REPOSITORY, command)
        self.assertIn("--max-archive-bytes", command)
        self.assertEqual(command[-2:], ["--max-response-bytes", str(MAX_RESPONSE_BYTES)])
        compile(REMOTE_PROGRAM, "<host-recovery>", "exec")
        self.assertIn('"remote_files_written": False', REMOTE_PROGRAM)
        self.assertIn('"scrapers_started": False', REMOTE_PROGRAM)
        self.assertIn('"production_writes": False', REMOTE_PROGRAM)

    def test_connection_coordinates_have_no_defaults_and_are_required(self):
        argument_parser = build_parser()
        actions = {option: action for action in argument_parser._actions for option in action.option_strings}
        for option in ("--host", "--ssh-user", "--remote-repository", "--since-utc"):
            self.assertTrue(actions[option].required, option)
            self.assertIsNone(actions[option].default, option)
        public_text = (
            (AUTOMATION / "ops" / "capture_host_recovery_once.py").read_text()
            + Path(__file__).read_text()
        )
        for private_marker in (
            "/" + "home" + "/",
            "130" + ".91",
            "ml" + "p2",
        ):
            self.assertNotIn(private_marker, public_text)
        self.assertIsNone(re.search(r"machine[\s_-]*17", public_text, re.I))

    def test_remote_program_covers_every_required_evidence_group(self):
        required_tokens = {
            "boot_shutdown": "last_boot_shutdown",
            "previous_boot_journal": "previous_boot_journal",
            "kernel_oom": "previous_kernel",
            "ssh_auth": "ssh_journal",
            "network": "network_journal",
            "disk_inode_filesystem": "df_inodes",
            "memory_swap_load": "swapon",
            "process_open_files": "open_file_descriptors",
            "logs_and_run_roots": "largest_files",
            "git_checkout": "git_status",
            "private_code_archive": "build_private_code_archive",
            "runtime_environments": "runtime_inventory",
            "github_provenance": "direct_push_dry_run",
        }
        self.assertEqual(set(required_tokens), set(REQUIRED_EVIDENCE_GROUPS))
        for group, token in required_tokens.items():
            with self.subTest(group=group):
                self.assertIn(token, REMOTE_PROGRAM)
        for token in (
            "NetworkManager", "NetworkManager.state", "systemd-networkd",
            "nmcli_connections_autoconnect", "nmcli_radio", "ip_rule",
            "ip_neighbour", "resolvectl", "network_services",
            "carrier_down_count", "speed", "duplex", "runtime_suspended_time",
            "ethtool_driver_", "ethtool_stats_", "ethtool_eee_", "pci_drivers",
            "network_journal_previous_boot", "network_journal_current_boot",
            "network_suspend_since", "previous_kernel_network_power",
            "powerprofilesctl", "tlp-stat", "ssh_listener_22",
            "bounded_gateway_test", "gateway_packets_requested",
            "device/power/control",
        ):
            self.assertIn(token, REMOTE_PROGRAM)
        self.assertIn('"python_packages"', REMOTE_PROGRAM)
        self.assertIn('"conda_packages"', REMOTE_PROGRAM)

    def test_remote_program_has_no_remote_filesystem_mutation_or_real_push(self):
        forbidden = (
            ".write_text(", ".write_bytes(", "Path.mkdir(", "os.mkdir(",
            "os.makedirs(", "os.remove(", "os.unlink(", "shutil.rmtree(",
            '"git", "checkout"', '"git", "reset"', '"git", "clean"',
            '"git", "pull"', '"git", "fetch"',
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, REMOTE_PROGRAM)
        self.assertIn('"core.hooksPath=/dev/null"', REMOTE_PROGRAM)
        self.assertIn('"--dry-run", "--no-verify"', REMOTE_PROGRAM)
        self.assertNotIn('"git", "push", "origin"', REMOTE_PROGRAM)
        self.assertNotIn('"sudo"', REMOTE_PROGRAM)
        self.assertIn('GIT_OPTIONAL_LOCKS="0"', REMOTE_PROGRAM)
        self.assertIn('"--no-textconv"', REMOTE_PROGRAM)

    def test_scrubber_removes_tokens_passwords_and_url_userinfo(self):
        fixtures = adversarial_fixtures()
        raw = " ".join((
            "to" + "ken=" + fixtures["github"],
            "pass" + "word=" + "hunter2",
            fixtures["https"] + "?access_" + "token=querysecret",
            "--mongo" + "db-uri " + fixtures["mongodb"],
            fixtures["github_short"],
            "Authorization: Basic " + fixtures["basic"],
            "AWS_" + "SECRET_ACCESS_KEY=" + "verysecret",
            fixtures["aws"], fixtures["slack"], fixtures["jwt"], fixtures["pem"],
        )).encode()
        value = scrub_local_stderr(raw)
        for secret in (
            "hunter2", "alice:secret", "querysecret", "bob:dbsecret",
            fixtures["github"], fixtures["github_short"], fixtures["basic"], "verysecret",
            fixtures["aws"], fixtures["slack"], fixtures["jwt"],
            "privatebytes",
        ):
            self.assertNotIn(secret, value)
        self.assertGreaterEqual(value.count("[REDACTED]"), 3)

    def test_remote_code_archive_filter_allows_code_and_rejects_secret_paths(self):
        code_path = self.remote_namespace()["code_path"]
        self.assertTrue(code_path("automation/ops/collector.py"))  # type: ignore[operator]
        self.assertTrue(code_path("scripts/run.sh"))  # type: ignore[operator]
        self.assertFalse(code_path("runs/articles.jsonl"))  # type: ignore[operator]
        self.assertFalse(code_path("automation/secrets/token.py"))  # type: ignore[operator]
        self.assertFalse(code_path("../escape.py"))  # type: ignore[operator]
        self.assertFalse(code_path("/absolute/code.py"))  # type: ignore[operator]

    def test_remote_scrubber_redacts_runtime_command_secrets(self):
        scrub = self.remote_namespace()["scrub"]
        fixtures = adversarial_fixtures()
        raw = " ".join((
            "--to" + "ken " + fixtures["github"],
            "MONGO" + "DB_URI=" + fixtures["https"].replace("https://", "mongodb://") + "/db",
            "--database-url " + "postgres://" + "bob:dbpass@example.test/db?to" + "ken=hidden",
            "Authorization: Basic " + fixtures["basic"],
            "AWS_" + "SECRET_ACCESS_KEY=" + "verysecret",
            fixtures["aws"], fixtures["slack"], fixtures["jwt"], fixtures["pem"],
        ))
        value = scrub(raw)  # type: ignore[operator]
        self.assertNotIn(fixtures["github"], value)
        self.assertNotIn("alice:secret", value)
        self.assertNotIn("bob:dbpass", value)
        self.assertNotIn("hidden", value)
        self.assertNotIn(fixtures["basic"], value)
        self.assertNotIn("verysecret", value)
        self.assertNotIn(fixtures["aws"], value)
        self.assertNotIn(fixtures["slack"], value)
        self.assertNotIn(fixtures["jwt"], value)
        self.assertNotIn("privatebytes", value)
        self.assertIn("[REDACTED]", value)

    def test_local_secret_gate_classifies_adversarial_values(self):
        fixtures = adversarial_fixtures()
        value = " ".join((
            "Authorization: Basic " + fixtures["basic"],
            "AWS_" + "SECRET_ACCESS_KEY=" + "verysecret",
            fixtures["slack"], fixtures["jwt"], fixtures["pem"],
        )).encode()
        kinds = set(local_secret_kinds(value))
        self.assertTrue(
            {"authorization_basic", "sensitive_assignment", "slack_token", "jwt", "private_key"}
            .issubset(kinds)
        )

    def test_sanitized_url_drops_userinfo_query_and_fragment(self):
        sanitized_url = self.remote_namespace()["sanitized_url"]
        credentialed = "https://" + "alice" + ":" + "secret" + "@github.com/lin61302/scraping.git"
        value = sanitized_url(  # type: ignore[operator]
            credentialed + "?to" + "ken=query#fragment"
        )
        self.assertEqual(value, "https://github.com/lin61302/scraping.git")
        scp = sanitized_url("oauth-secret@github.com:lin61302/scraping.git")  # type: ignore[operator]
        self.assertEqual(scp, "[REDACTED]@github.com:lin61302/scraping.git")

    def test_git_top_level_is_resolved_from_nested_path(self):
        namespace = self.remote_namespace()
        resolve_git_root = namespace["resolve_git_root"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            nested = root / "deep" / "subdir"
            nested.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            resolved, evidence = resolve_git_root(nested)  # type: ignore[operator]
            self.assertEqual(resolved.resolve(), root.resolve())
            self.assertEqual(evidence["returncode"], 0)
            self.assertIn("rev-parse", evidence["argv"])

    def test_github_origin_parser_requires_exact_host_and_repository(self):
        parser = self.remote_namespace()["parse_github_repository"]
        self.assertEqual(parser("git@github.com:lin61302/scraping.git"), "lin61302/scraping")  # type: ignore[operator]
        self.assertEqual(parser("https://github.com/lin61302/scraping.git"), "lin61302/scraping")  # type: ignore[operator]
        for spoof in (
            "https://evilgithub.com/lin61302/scraping.git",
            "git@github.com:lin61302/scraping/extra.git",
        ):
            with self.subTest(spoof=spoof):
                self.assertIsNone(parser(spoof))  # type: ignore[operator]
        self.assertNotEqual(
            parser("https://github.com/lin61302/scraping-evil.git"),  # type: ignore[operator]
            "lin61302/scraping",
        )

    def test_remote_archive_excludes_secret_content_and_hashes_its_path(self):
        namespace = self.remote_namespace()
        build_archive = namespace["build_private_code_archive"]
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "safe.py").write_text("print('old')\n")
            (repo / "secret.py").write_text("print('old')\n")
            subprocess.run(["git", "-C", str(repo), "add", "safe.py", "secret.py"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
            (repo / "safe.py").write_text("print('safe new')\n")
            fixtures = adversarial_fixtures()
            tracked_secret = fixtures["github"]
            assignment_name = "TO" + "KEN"
            (repo / "secret.py").write_text("%s = '%s'\n" % (assignment_name, tracked_secret))
            aws_secret = fixtures["aws"]
            (repo / "ordinary.py").write_text("value = '%s'\n" % aws_secret)
            (repo / "secrets").mkdir()
            denied_name = "secrets/token.py"
            (repo / denied_name).write_text("print('denied')\n")

            result = build_archive(repo, MAX_ARCHIVE_BYTES)  # type: ignore[operator]
            self.assertEqual(result["status"], "complete")
            archive_bytes = base64.b64decode(result["archive_base64"])
            self.assertNotIn(tracked_secret.encode(), archive_bytes)
            self.assertNotIn(aws_secret.encode(), archive_bytes)
            self.assertNotIn(denied_name.encode(), archive_bytes)
            self.assertEqual(result["tracked_paths"], ["safe.py"])
            self.assertEqual(result["untracked_paths"], [])
            hashes = {row["path_sha256"] for row in result["excluded_path_hashes"]}
            for path in ("secret.py", "ordinary.py", denied_name):
                self.assertIn(hashlib.sha256(path.encode()).hexdigest(), hashes)
            validate_outer = {
                key: result[key]
                for key in (
                    "status", "tracked_paths", "untracked_paths",
                    "excluded_path_hashes", "errors",
                )
            }
            from ops.capture_host_recovery_once import _validate_private_archive
            _validate_private_archive(archive_bytes, validate_outer)

    def test_payload_validation_rejects_mutation_or_archive_mismatch(self):
        payload = valid_payload()
        self.assertEqual(validate_remote_payload(payload)["status"], "captured")
        mutated = dict(payload, remote_files_written=True)
        with self.assertRaisesRegex(ValueError, "remote invariant"):
            validate_remote_payload(mutated)
        broken = json.loads(json.dumps(payload))
        broken["private_code_archive"]["archive_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "hash or byte count"):
            validate_remote_payload(broken)
        inconsistent = json.loads(json.dumps(payload))
        inconsistent["required_group_status"]["network"] = False
        with self.assertRaisesRegex(ValueError, "missing evidence groups"):
            validate_remote_payload(inconsistent)
        inconsistent_status = json.loads(json.dumps(payload))
        inconsistent_status["status"] = "captured_with_gaps"
        with self.assertRaisesRegex(ValueError, "status is inconsistent"):
            validate_remote_payload(inconsistent_status)

    def test_payload_validation_cross_checks_archive_manifest(self):
        payload = valid_payload()
        mismatched = json.loads(json.dumps(payload))
        mismatched["private_code_archive"]["tracked_paths"] = ["spoof.py"]
        with self.assertRaisesRegex(ValueError, "tracked paths mismatch"):
            validate_remote_payload(mismatched)

        raw = base64.b64decode(payload["private_code_archive"]["archive_base64"])
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as source:
            manifest = json.loads(source.extractfile("archive_manifest.json").read())
        manifest["tracked_patch_sha256"] = "0" * 64
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as target:
                for name, data in (
                    ("archive_manifest.json", (json.dumps(manifest) + "\n").encode()),
                    ("tracked_changes.patch", b""),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    target.addfile(info, io.BytesIO(data))
        corrupted = buffer.getvalue()
        mismatched_hash = json.loads(json.dumps(payload))
        mismatched_hash["private_code_archive"]["archive_base64"] = base64.b64encode(corrupted).decode()
        mismatched_hash["private_code_archive"]["archive_bytes"] = len(corrupted)
        mismatched_hash["private_code_archive"]["archive_sha256"] = hashlib.sha256(corrupted).hexdigest()
        with self.assertRaisesRegex(ValueError, "patch hash mismatch"):
            validate_remote_payload(mismatched_hash)

    def test_capture_uses_one_runner_call_and_writes_private_files(self):
        calls: list[tuple[list[str], dict[str, object]]] = []
        payload = valid_payload()

        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b"")

        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            output = private_root / "capture"
            result = capture_once(
                output_dir=output, runner=runner, private_root=private_root,
                **self.connection_kwargs(),
            )
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][0], "ssh")
            self.assertEqual(calls[0][1]["input"], REMOTE_PROGRAM.encode())
            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o700")
            for name in (
                "attempt.private.json",
                "ssh_stderr.private.txt",
                "connection_receipt.private.json",
                "recovery_evidence.private.json",
                "modified_untracked_code.private.tar.gz",
            ):
                path = output / name
                self.assertTrue(path.is_file(), name)
                self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            evidence_text = (output / "recovery_evidence.private.json").read_text()
            self.assertNotIn("archive_base64", evidence_text)
            archive = (output / "modified_untracked_code.private.tar.gz").read_bytes()
            self.assertEqual(sha256_bytes(archive), payload["private_code_archive"]["archive_sha256"])
            self.assertFalse(any(path.name.startswith(".") and ".tmp." in path.name for path in output.iterdir()))

    def test_output_directory_must_be_under_private_root_and_outside_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            private_root = base / "private"
            private_root.mkdir()
            accepted = validate_private_output_dir(
                private_root / "capture", private_root=private_root,
            )
            self.assertEqual(accepted, (private_root / "capture").resolve())
            with self.assertRaisesRegex(ValueError, "private operations root"):
                validate_private_output_dir(base / "outside", private_root=private_root)
            git_root = private_root / "nested"
            git_root.mkdir()
            (git_root / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "Git worktree"):
                validate_private_output_dir(git_root / "capture", private_root=private_root)

    def test_atomic_private_write_is_exclusive_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "packet.private"
            atomic_private_write(path, b"first")
            with self.assertRaises(FileExistsError):
                atomic_private_write(path, b"second")
            self.assertEqual(path.read_bytes(), b"first")
            self.assertFalse(any(".tmp." in item.name for item in path.parent.iterdir()))

    def test_bounded_runner_stops_oversized_stdout(self):
        result = run_process_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"],
            input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, check=False, stdout_limit=1024, stderr_limit=1024,
        )
        self.assertTrue(result.stdout_overflow)
        self.assertLessEqual(len(result.stdout), 1024)
        self.assertGreater(result.stdout_bytes_seen, 1024)

    def test_existing_attempt_fails_before_second_runner_call(self):
        calls = 0

        def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            nonlocal calls
            calls += 1
            payload = valid_payload()
            return subprocess.CompletedProcess(args[0], 0, json.dumps(payload).encode(), b"")

        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            output = private_root / "capture"
            first = capture_once(
                output_dir=output, runner=runner, private_root=private_root,
                **self.connection_kwargs(),
            )
            self.assertEqual(first.exit_code, 0)
            with self.assertRaises(FileExistsError):
                capture_once(
                    output_dir=output, runner=runner, private_root=private_root,
                    **self.connection_kwargs(),
                )
            self.assertEqual(calls, 1)

    def test_missing_group_is_preserved_as_nonzero_terminal_capture(self):
        payload = valid_payload(missing=("previous_boot_journal",))

        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b"")

        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            result = capture_once(
                output_dir=private_root / "capture", runner=runner,
                private_root=private_root,
                **self.connection_kwargs(),
            )
            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.status, "captured_with_gaps")
            self.assertEqual(result.receipt["missing_required_groups"], ["previous_boot_journal"])

    def test_incident_start_is_validated_before_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(argparse.ArgumentTypeError):
                capture_once(
                    output_dir=Path(temporary) / "capture",
                    host=TEST_HOST,
                    ssh_user=TEST_SSH_USER,
                    remote_repository=TEST_REMOTE_REPOSITORY,
                    since_utc="not-a-timestamp",
                    runner=lambda *args, **kwargs: None,  # type: ignore[arg-type]
                    private_root=Path(temporary),
                )


if __name__ == "__main__":
    unittest.main()
