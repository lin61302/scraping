import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from automation.ops import publish_github_provenance as publisher


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPOSITORY_ROOT / "automation/config/github_publish_policy.json"


def run_git(cwd, *arguments):
    completed = subprocess.run(
        ["git"] + list(arguments),
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


class PolicyAndRemoteTests(unittest.TestCase):
    def setUp(self):
        self.policy = publisher.load_json(POLICY_PATH)

    def test_policy_is_exact_target(self):
        publisher.validate_policy(self.policy)
        self.assertEqual(self.policy["expected_github_login"], "lin61302")
        self.assertEqual(self.policy["repository"]["upstream"], "origin/main")

    def test_supported_remote_forms_are_exact(self):
        valid = [
            "https://github.com/lin61302/scraping.git",
            "git@github.com:lin61302/scraping.git",
            "ssh://git@github.com/lin61302/scraping.git",
        ]
        for value in valid:
            self.assertEqual(publisher.normalize_github_remote(value), ("lin61302", "scraping"))

    def test_credentialed_and_wrong_remotes_are_rejected(self):
        rejected = [
            "https://" + "token@github.com/lin61302/scraping.git",
            "https://github.com/someone/scraping.git",
            "https://example.com/lin61302/scraping.git",
            "file:///tmp/repository.git",
        ]
        for value in rejected:
            with self.assertRaises(publisher.PublishError):
                owner, name = publisher.normalize_github_remote(value)
                if (owner, name) != ("lin61302", "scraping"):
                    raise publisher.PublishError("unexpected_remote", "wrong repository")


class PrivacyAndManifestTests(unittest.TestCase):
    def setUp(self):
        self.policy = publisher.load_json(POLICY_PATH)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.repo = self.root / "repo"
        self.external = self.root / "external"
        self.source.mkdir()
        self.repo.mkdir()
        self.external.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self, content, records=None, manifest_id="unit-test"):
        script = self.source / "adapter.py"
        script.write_bytes(content)
        entry = {
            "source": "adapter.py",
            "destination": "JAM/adapter.py",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "mode": "0644",
        }
        manifest = {
            "schema_version": 1,
            "manifest_id": manifest_id,
            "created_at": "2026-10-18T14:15:00Z",
            "files": [entry],
            "run_records": records or [],
        }
        path = self.external / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path, manifest

    def test_exact_manifest_materializes_idempotently(self):
        content = b"#!/usr/bin/env python3\nprint('safe')\n"
        manifest_path, _ = self.write_manifest(content)
        prepared = publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)
        self.assertEqual(publisher.materialize(prepared, self.repo), ["JAM/adapter.py"])
        self.assertEqual(publisher.materialize(prepared, self.repo), [])
        self.assertEqual((self.repo / "JAM/adapter.py").read_bytes(), content)

    def test_hash_mismatch_and_traversal_are_rejected(self):
        manifest_path, manifest = self.write_manifest(b"print('safe')\n")
        manifest["files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(publisher.PublishError, "sha256"):
            publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)
        manifest["files"][0]["sha256"] = hashlib.sha256(b"print('safe')\n").hexdigest()
        manifest["files"][0]["destination"] = "../escape.py"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(publisher.PublishError):
            publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)

    def test_existing_root_script_layout_is_allowed_but_root_json_is_not(self):
        content = b"print('safe')\n"
        manifest_path, manifest = self.write_manifest(content)
        manifest["files"][0]["destination"] = "getnewurls_everyday.py"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        prepared = publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)
        self.assertEqual(prepared.files[0].destination_text, "getnewurls_everyday.py")
        manifest["files"][0]["destination"] = "unreviewed.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(publisher.PublishError):
            publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)

    def test_secret_local_path_binary_and_infrastructure_gates(self):
        documentation_ip = ".".join(["203", "0", "113", "7"])
        host_label = "machine" + "-" + "17"
        host_suffix = "_on_" + "17"
        local_service_colon = "local" + "host" + ":" + "8080"
        local_service_underscore = "local" + "host" + "_" + "9000"
        rejected = [
            b"token = 'this-is-a-real-looking-token'\n",
            ("path = '/" + "Users/example/private/file'\n").encode("utf-8"),
            ("server = '%s'\n" % documentation_ip).encode("utf-8"),
            ("host = '%s'\n" % host_label).encode("utf-8"),
            ("artifact = 'worker%s'\n" % host_suffix).encode("utf-8"),
            ("endpoint = '%s'\n" % local_service_colon).encode("utf-8"),
            ("endpoint = '%s'\n" % local_service_underscore).encode("utf-8"),
            b"binary\x00content",
        ]
        for index, content in enumerate(rejected):
            manifest_path, _ = self.write_manifest(content, manifest_id="privacy-%d" % index)
            with self.assertRaises(publisher.PublishError):
                publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)

    def test_browser_user_agent_versions_are_not_treated_as_ip_addresses(self):
        dotted_version = ".".join(["126", "0", "0", "0"])
        products = ["Chrome", "Chromium", "Firefox", "Version", "Safari", "AppleWebKit", "Edg", "OPR"]
        for product in products:
            content = ("user_agent = '%s/%s'\n" % (product, dotted_version)).encode("utf-8")
            publisher._validate_text_privacy(
                content, "browser user-agent", self.policy["limits"]["max_file_bytes"]
            )

    def test_standalone_and_url_ip_addresses_remain_rejected(self):
        public_address = ".".join(["203", "0", "113", "7"])
        loopback_address = ".".join(["127", "0", "0", "1"])
        address_url = "https" + "://" + public_address + "/article"
        loopback_url = "http" + "://" + loopback_address + ":8080/service"
        for content in (public_address, address_url, loopback_url):
            with self.assertRaisesRegex(publisher.PublishError, "IPv4"):
                publisher._validate_text_privacy(
                    content.encode("utf-8"), "address", self.policy["limits"]["max_file_bytes"]
                )

    def test_run_records_are_rebuilt_from_allowlisted_fields(self):
        content = b"print('safe')\n"
        digest = hashlib.sha256(content).hexdigest()
        records = [
            {
                "event": "launch",
                "run_id": "run-20261018",
                "occurred_at": "2026-10-18T14:15:00Z",
                "source_ids": ["JAM-NCT"],
                "scripts": [{"destination": "JAM/adapter.py", "sha256": digest}],
                "production_writes": False,
                "git_commit": "1" * 40,
                "operation_manifest_sha256": "2" * 64,
                "acceptance_window": {"from": "2026-07-01", "through": "2026-09-30"},
                "evidence_sha256": ["3" * 64],
            },
            {
                "event": "terminal",
                "run_id": "run-20261018",
                "occurred_at": "2026-10-18T15:15:00Z",
                "source_ids": ["JAM-NCT"],
                "scripts": [{"destination": "JAM/adapter.py", "sha256": digest}],
                "production_writes": False,
                "git_commit": "1" * 40,
                "operation_manifest_sha256": "2" * 64,
                "acceptance_window": {"from": "2026-07-01", "through": "2026-09-30"},
                "evidence_sha256": ["3" * 64, "4" * 64],
                "outcome": "succeeded",
                "exit_code": 0,
                "counts": {"accepted": 12, "failed": 0},
            },
        ]
        manifest_path, _ = self.write_manifest(content, records=records)
        prepared = publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)
        changed = publisher.materialize(prepared, self.repo)
        self.assertEqual(len(changed), 3)
        record_path = self.repo / "automation/run_records/2026/10/18/run-20261018.terminal.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertNotIn("host", record)
        self.assertEqual(record["counts"], {"accepted": 12, "failed": 0})
        self.assertEqual(record["git_commit"], "1" * 40)
        self.assertEqual(record["operation_manifest_sha256"], "2" * 64)
        self.assertEqual(record["acceptance_window"]["through"], "2026-09-30")
        self.assertEqual(record["evidence_sha256"], ["3" * 64, "4" * 64])

    def test_unknown_run_record_fields_and_raw_counts_are_rejected(self):
        content = b"print('safe')\n"
        digest = hashlib.sha256(content).hexdigest()
        record = {
            "event": "terminal",
            "run_id": "run-1",
            "occurred_at": "2026-10-18T15:15:00Z",
            "source_ids": ["JAM-NCT"],
            "scripts": [{"destination": "JAM/adapter.py", "sha256": digest}],
            "production_writes": False,
            "git_commit": "1" * 40,
            "operation_manifest_sha256": "2" * 64,
            "acceptance_window": {"from": "2026-07-01", "through": "2026-09-30"},
            "evidence_sha256": ["3" * 64],
            "outcome": "failed",
            "exit_code": 1,
            "counts": {"article_urls": 1},
            "raw_log": "must never be accepted",
        }
        manifest_path, _ = self.write_manifest(content, records=[record])
        with self.assertRaises(publisher.PublishError):
            publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)

    def test_run_record_provenance_fields_are_required_and_bounded(self):
        content = b"print('safe')\n"
        digest = hashlib.sha256(content).hexdigest()
        record = {
            "event": "launch",
            "run_id": "run-provenance",
            "occurred_at": "2026-10-18T15:15:00Z",
            "source_ids": ["JAM-NCT"],
            "scripts": [{"destination": "JAM/adapter.py", "sha256": digest}],
            "production_writes": False,
            "git_commit": "1" * 40,
            "operation_manifest_sha256": "2" * 64,
            "acceptance_window": {"from": "2026-10-01", "through": "2026-09-30"},
            "evidence_sha256": ["3" * 64],
        }
        manifest_path, _ = self.write_manifest(content, records=[record])
        with self.assertRaisesRegex(publisher.PublishError, "reversed"):
            publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)
        record["acceptance_window"] = {"from": "2026-07-01", "through": "2026-09-30"}
        record["evidence_sha256"] = []
        manifest_path, _ = self.write_manifest(content, records=[record])
        with self.assertRaisesRegex(publisher.PublishError, "bounded nonempty"):
            publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)

    def test_publisher_sources_pass_their_own_privacy_gate(self):
        paths = [
            REPOSITORY_ROOT / "automation/ops/publish_github_provenance.py",
            POLICY_PATH,
            REPOSITORY_ROOT / "automation/tests/test_publish_github_provenance.py",
            REPOSITORY_ROOT / "docs/GITHUB_PROVENANCE.md",
        ]
        for path in paths:
            publisher._validate_text_privacy(
                path.read_bytes(), path.name, self.policy["limits"]["max_file_bytes"]
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_source_and_destination_symlinks_are_rejected(self):
        real = self.source / "real.py"
        real.write_text("print('safe')\n", encoding="utf-8")
        linked = self.source / "adapter.py"
        linked.symlink_to(real.name)
        content = real.read_bytes()
        manifest = {
            "schema_version": 1,
            "manifest_id": "symlink-source",
            "created_at": "2026-10-18T14:15:00Z",
            "files": [
                {
                    "source": "adapter.py",
                    "destination": "JAM/adapter.py",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "mode": "0644",
                }
            ],
            "run_records": [],
        }
        manifest_path = self.external / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(publisher.PublishError, "symlink"):
            publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)

        linked.unlink()
        linked.write_bytes(content)
        (self.repo / "JAM").mkdir()
        (self.repo / "target.py").write_bytes(content)
        (self.repo / "JAM/adapter.py").symlink_to("../target.py")
        with self.assertRaisesRegex(publisher.PublishError, "symlink"):
            publisher.prepare_manifest(manifest_path, self.source, self.repo, self.policy)


class GitTransactionTests(unittest.TestCase):
    def setUp(self):
        self.policy = publisher.load_json(POLICY_PATH)
        self.policy = dict(self.policy)
        self.policy["validation_commands"] = [["python3", "-c", "print('offline validation')"]]
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.repo.mkdir()
        run_git(self.root, "init", "--bare", str(self.remote))
        run_git(self.repo, "init", "-b", "main")
        run_git(self.repo, "config", "user.name", "Unit Test")
        run_git(self.repo, "config", "user.email", "unit@example.invalid")
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        run_git(self.repo, "add", "seed.txt")
        run_git(self.repo, "commit", "-m", "seed")
        run_git(self.repo, "remote", "add", "origin", str(self.remote))
        run_git(self.repo, "push", "-u", "origin", "main")
        self.runner = publisher.CommandRunner()

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_staging_rejects_unexpected_files(self):
        (self.repo / "JAM").mkdir()
        (self.repo / "JAM/adapter.py").write_text("print('safe')\n", encoding="utf-8")
        (self.repo / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
        with self.assertRaisesRegex(publisher.PublishError, "explicit publish plan"):
            publisher.stage_and_commit(
                self.repo, ["JAM/adapter.py"], "unit", self.policy, self.runner
            )
        self.assertEqual(run_git(self.repo, "diff", "--cached", "--name-only"), "")

    def test_porcelain_leading_space_is_preserved(self):
        (self.repo / "seed.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual(publisher._status_paths(self.repo, self.runner), {"seed.txt"})

    def test_dirty_checkout_blocks_unattended_bootstrap(self):
        (self.repo / "unreviewed.py").write_text("print('unreviewed')\n", encoding="utf-8")
        with self.assertRaisesRegex(publisher.PublishError, "clean"):
            publisher._require_clean_repository(self.repo, self.runner)

    def test_direct_push_and_remote_sha_verification_without_network(self):
        (self.repo / "JAM").mkdir()
        (self.repo / "JAM/adapter.py").write_text("print('safe')\n", encoding="utf-8")
        commit = publisher.stage_and_commit(
            self.repo, ["JAM/adapter.py"], "unit", self.policy, self.runner
        )
        remote_sha = publisher.push_and_verify(self.repo, commit, self.runner)
        self.assertEqual(remote_sha, commit)
        self.assertEqual(run_git(self.repo, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
