from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import launch_exact_sequence_once as launcher
from ops import unattended_exact_observation_sequence as sequence


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.remote_root = "/srv/scraping/deployments/exact-fixture"
        self.files = []
        for relative in (
            "automation/ops/unattended_exact_observation_sequence.py",
            "automation/ops/run_pure_discovery_diagnostic.py",
            "automation/ops/host_capacity_guard.py",
            "automation/scripts/parallel_observe_pure.py",
            "plan.json",
            "private/exact.txt",
        ):
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture %s\n" % relative, encoding="utf-8")
            private = relative.startswith("private/") or relative == "plan.json"
            self.files.append({
                "local_path": relative,
                "remote_path": "%s/%s" % (self.remote_root, relative),
                "sha256": sha(path),
                "bytes": path.stat().st_size,
                "private": private,
                "mode": "0600" if private else "0644",
            })
        controller = next(row for row in self.files if row["local_path"].endswith(
            "unattended_exact_observation_sequence.py"
        ))
        plan = next(row for row in self.files if row["local_path"] == "plan.json")
        private_input = next(
            row for row in self.files if row["local_path"] == "private/exact.txt"
        )
        plan_path = self.workspace / "plan.json"
        plan_path.write_text(json.dumps({
            "schema_version": 1,
            "mode": "unattended_exact_observation_sequence",
            "production_writes": False,
            "phases": [{
                "phase_id": "exact",
                "lanes": [{
                    "lane_id": "repair",
                    "kind": "exact_urls",
                    "production_writes": False,
                    "exact_input": {
                        "path": "private/exact.txt",
                        "sha256": private_input["sha256"],
                    },
                }],
            }],
        }, sort_keys=True) + "\n", encoding="utf-8")
        plan["sha256"] = sha(plan_path)
        plan["bytes"] = plan_path.stat().st_size
        self.payload = {
            "schema_version": 1,
            "mode": "exact_sequence_single_use_deployment",
            "sequence_id": "exact-fixture", "production_writes": False,
            "host": "127.0.0.1", "ssh_connections": 1, "ssh_retries": 0,
            "startup_inspection_seconds": 4,
            "network_ceiling": 2, "browser_ceiling": 0,
            "repository": "lin61302/scraping", "branch": "main",
            "code_git_commit": "1" * 40,
            "deployment_root_remote": self.remote_root,
            "files": [{key: row[key] for key in (
                "local_path", "remote_path", "sha256", "private", "mode",
            )} for row in self.files],
            "controller_remote": controller["remote_path"],
            "controller_sha256": controller["sha256"],
            "plan_remote": plan["remote_path"], "plan_sha256": plan["sha256"],
            "automation_root_remote": self.remote_root + "/automation",
            "python_remote": "/opt/conda/bin/python",
            "output_root_remote": "/srv/scraping/outputs/exact-fixture",
            "receipt_root_remote": "/srv/scraping/receipts",
            "host_lock_remote": "/srv/scraping/locks/exact-sequence.lock",
            "allowed_parent_roots_remote": ["/srv/scraping/observations"],
            "remote_attempt_marker": "/srv/scraping/receipts/exact-fixture/remote-attempt.json",
            "remote_resume_marker": "/srv/scraping/receipts/exact-fixture/remote-resume-attempt.json",
            "resume_launch_receipt_remote": "/srv/scraping/receipts/exact-fixture/resume-launch.json",
            "deployment_receipt_remote": "/srv/scraping/receipts/exact-fixture/deployment.json",
            "controller_log_remote": "/srv/scraping/receipts/exact-fixture/controller.log",
            "resume_controller_log_remote": "/srv/scraping/receipts/exact-fixture/resume-controller.log",
            "startup_receipt_remote": "/srv/scraping/receipts/exact-fixture/startup_receipt.json",
            "terminal_receipt_remote": "/srv/scraping/receipts/exact-fixture/terminal_receipt.json",
        }
        self.payload["deployment_sha256"] = launcher.deployment_digest(
            self.files, self.remote_root,
        )

    def manifest(self, payload=None):
        path = self.root / "deployment.json"
        path.write_text(json.dumps(payload or self.payload, sort_keys=True) + "\n", encoding="utf-8")
        return path, sha(path)

    def rehash_deployment(self, payload):
        rows = []
        for item in payload["files"]:
            row = dict(item)
            row["bytes"] = (self.workspace / item["local_path"]).stat().st_size
            rows.append(row)
        payload["deployment_sha256"] = launcher.deployment_digest(rows, self.remote_root)


def test_manifest_is_exact_host_single_ssh_and_content_addressed():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        path, digest = fixture.manifest()
        manifest = launcher.load_manifest(path, digest, fixture.workspace)
        assert manifest["host"] == "127.0.0.1"
        assert manifest["ssh_connections"] == 1
        assert manifest["ssh_retries"] == 0
        assert manifest["network_ceiling"] == 2
        assert manifest["browser_ceiling"] == 0
        assert launcher.build_payload(manifest)["production_writes"] is False


def test_same_relative_exact_plan_passes_launcher_and_controller_preflight():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        adapter_relative = "automation/adapters/TST/source.py"
        adapter = fixture.workspace / adapter_relative
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_text(
            "class Config:\n"
            " source_name='example.test'; country_code='TST'; production_writes=False\n"
            "PRODUCTION_WRITES=False\n"
            "def discover_urls(start,end,evidence): return []\n"
            "def scrape_url(url): return {}\n",
            encoding="utf-8",
        )
        adapter_row = {
            "local_path": adapter_relative,
            "remote_path": "%s/%s" % (fixture.remote_root, adapter_relative),
            "sha256": sha(adapter),
            "bytes": adapter.stat().st_size,
            "private": False,
            "mode": "0644",
        }
        fixture.files.append(adapter_row)
        fixture.payload["files"].append({
            key: adapter_row[key] for key in (
                "local_path", "remote_path", "sha256", "private", "mode",
            )
        })

        parent = fixture.root / "parent" / "observe-parallel-parent"
        parent.mkdir(parents=True)
        (parent / "manifest.json").write_text(json.dumps({
            "run_id": "parent", "status": "observed", "source": "example.test",
        }) + "\n", encoding="utf-8")
        for name in ("urls.jsonl", "field_checks.jsonl", "issues.jsonl"):
            (parent / name).write_text("{}\n", encoding="utf-8")
        parent_hashes = {
            name: sha(parent / name) for name in sequence.PARENT_ARTIFACTS
        }
        private_input = fixture.workspace / "private/exact.txt"
        private_input.write_text("https://example.test/a\n", encoding="utf-8")
        for row in fixture.files:
            if row["local_path"] == "private/exact.txt":
                row["sha256"] = sha(private_input)
                row["bytes"] = private_input.stat().st_size
        for row in fixture.payload["files"]:
            if row["local_path"] == "private/exact.txt":
                row["sha256"] = sha(private_input)

        execution = []
        for relative in (
            "automation/ops/unattended_exact_observation_sequence.py",
            "automation/ops/run_pure_discovery_diagnostic.py",
            "automation/ops/host_capacity_guard.py",
            "automation/scripts/parallel_observe_pure.py",
        ):
            execution.append({"path": relative, "sha256": sha(fixture.workspace / relative)})
        plan_payload = {
            "schema_version": 1,
            "mode": "unattended_exact_observation_sequence",
            "sequence_id": "exact-fixture",
            "production_writes": False,
            "continue_after_isolated_lane_failure": True,
            "provenance": {
                "repository": "lin61302/scraping", "branch": "main",
                "code_git_commit": "1" * 40,
            },
            "capacity": {
                "network_ceiling": 2, "browser_ceiling": 0,
                "max_wall_seconds": 100, "log_max_bytes": 50 * 1024 * 1024,
            },
            "execution_files": execution,
            "phases": [{
                "phase_id": "exact", "production_writes": False,
                "max_active_lanes": 1,
                "lanes": [{
                    "lane_id": "repair", "kind": "exact_urls",
                    "source_id": "mlp:TST:example.test", "production_writes": False,
                    "adapter": {"path": adapter_relative, "sha256": sha(adapter)},
                    "start": "2026-09-01", "end": "2026-09-30",
                    "workers": 1, "timeout_seconds": 30, "rediscovery": False,
                    "repair_class": "parser_exact",
                    "exact_input": {
                        "path": "private/exact.txt", "sha256": sha(private_input),
                    },
                    "exact_input_count": 1,
                    "parent": {"run_dir": str(parent), "artifacts": parent_hashes},
                    "derivation_reason": "shared_relative_path_integration_fixture",
                    "acceptance": {"manifest_status": "observed", "exceptions_max": 0},
                }],
            }],
        }
        plan_path = fixture.workspace / "plan.json"
        plan_path.write_text(
            json.dumps(plan_payload, sort_keys=True) + "\n", encoding="utf-8",
        )
        plan_hash = sha(plan_path)
        for row in fixture.files:
            if row["local_path"] == "plan.json":
                row["sha256"] = plan_hash
                row["bytes"] = plan_path.stat().st_size
        for row in fixture.payload["files"]:
            if row["local_path"] == "plan.json":
                row["sha256"] = plan_hash
        fixture.payload["plan_sha256"] = plan_hash
        fixture.payload["controller_sha256"] = next(
            row["sha256"] for row in fixture.files
            if row["local_path"].endswith("unattended_exact_observation_sequence.py")
        )
        fixture.payload["deployment_sha256"] = launcher.deployment_digest(
            fixture.files, fixture.remote_root,
        )
        manifest_path, manifest_hash = fixture.manifest()

        preflight = launcher.preflight(manifest_path, manifest_hash, fixture.workspace)
        loaded = sequence.load_plan(
            plan_path, plan_hash, fixture.workspace, (fixture.root / "parent",),
        )
        assert preflight["status"] == "pass"
        assert loaded.phases[0].lanes[0].exact_input.path == private_input.resolve()


def test_manifest_rejects_alias_branch_force_and_out_of_root_files():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        for mutate in (
            lambda value: value.update(host="host-alias"),
            lambda value: value.update(branch="repair"),
            lambda value: value.update(network_ceiling=4),
            lambda value: value["files"][0].update(remote_path="/tmp/escape.py"),
        ):
            payload = json.loads(json.dumps(fixture.payload))
            mutate(payload)
            path, digest = fixture.manifest(payload)
            with pytest.raises(launcher.LaunchError):
                launcher.load_manifest(path, digest, fixture.workspace)


def test_exact_input_plan_path_requires_canonical_deployment_relative_form():
    root = "/srv/scraping/deployments/exact-fixture"
    assert launcher._deployment_relative_remote(
        "private/exact.txt", root, "exact_input.path",
    ) == root + "/private/exact.txt"
    for unsafe in ("/srv/scraping/private/exact.txt", "../private/exact.txt", "./private/exact.txt"):
        with pytest.raises(launcher.LaunchError, match="deployment-root-relative"):
            launcher._deployment_relative_remote(unsafe, root, "exact_input.path")


def test_launcher_creates_marker_before_one_ssh_and_refuses_second_attempt():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        manifest, digest = fixture.manifest()
        operation = fixture.root / "operation"
        args = argparse.Namespace(
            manifest=manifest, manifest_sha256=digest,
            workspace_root=fixture.workspace, operation_root=operation,
            ssh_timeout_seconds=30, resume_existing_attempt=False,
        )
        completed = subprocess_result = mock.Mock()
        subprocess_result.returncode = 0
        subprocess_result.stdout = json.dumps({"status": "launched_unattended"})
        subprocess_result.stderr = ""
        with mock.patch.object(launcher.subprocess, "run", return_value=completed) as run:
            assert launcher.execute(args) == 0
        run.assert_called_once()
        command = run.call_args.args[0]
        assert command.count("ssh") == 1
        assert "127.0.0.1" in command
        assert (operation / "attempt_receipt.json").is_file()
        assert (operation / "connection_receipt.json").is_file()
        attempt = json.loads((operation / "attempt_receipt.json").read_text(encoding="utf-8"))
        receipt = json.loads((operation / "connection_receipt.json").read_text(encoding="utf-8"))
        assert "host" not in attempt
        assert "host" not in receipt
        with pytest.raises(launcher.LaunchError, match="attempt marker"):
            launcher.execute(args)


def test_single_use_resume_keeps_original_marker_and_adds_resume_receipts():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        manifest, digest = fixture.manifest()
        operation = fixture.root / "operation"
        initial = argparse.Namespace(
            manifest=manifest, manifest_sha256=digest,
            workspace_root=fixture.workspace, operation_root=operation,
            ssh_timeout_seconds=30, resume_existing_attempt=False,
        )
        completed = mock.Mock(returncode=0, stdout=json.dumps({
            "status": "launched_unattended",
        }), stderr="")
        with mock.patch.object(launcher.subprocess, "run", return_value=completed):
            assert launcher.execute(initial) == 0
        original_hash = sha(operation / "attempt_receipt.json")

        resume = argparse.Namespace(
            manifest=manifest, manifest_sha256=digest,
            workspace_root=fixture.workspace, operation_root=operation,
            ssh_timeout_seconds=30, resume_existing_attempt=True,
        )
        with mock.patch.object(launcher.subprocess, "run", return_value=completed) as run:
            assert launcher.execute(resume) == 0
        assert run.call_count == 1
        assert sha(operation / "attempt_receipt.json") == original_hash
        assert (operation / "resume_attempt_receipt.json").is_file()
        assert (operation / "resume_connection_receipt.json").is_file()
        assert (operation / "resume_ssh_stdout.private.json").is_file()
        remote_program = run.call_args.kwargs["input"]
        assert 'command.append("--resume")' in remote_program
        assert 'original attempt marker pins are invalid' in remote_program
        assert 'single-use resume marker already exists' in remote_program
        with pytest.raises(launcher.LaunchError, match="resume marker"):
            launcher.execute(resume)


def test_public_launcher_uses_only_the_private_manifest_host():
    source = Path(launcher.__file__).read_text(encoding="utf-8")
    assert "\nHOST =" not in source
    assert '"host": manifest["host"]' in source
    assert 'str(manifest["host"])' in source


def test_remote_program_has_bounded_startup_and_no_retry_or_supervision_loop():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        path, digest = fixture.manifest()
        manifest = launcher.load_manifest(path, digest, fixture.workspace)
        program = launcher.remote_program(launcher.build_payload(manifest))
        assert 'time.sleep(P["startup_inspection_seconds"])' in program
        assert 'P.get("network_ceiling")!=2' in program
        assert 'if resume: command.append("--resume")' in program
        assert 'if marker.exists() and not resume' in program
        assert "while True" not in program
        assert "if exit_after is not None: cleanup=stop(process)" in program
        assert "controller startup receipt missing after bounded inspection" in program
        assert "if process is not None: stop(process)" in program
        assert "controller exited during startup after verified cleanup" in program
        assert "controller exited without terminal receipt after verified cleanup" in program
        assert "controller process group remained alive after SIGKILL" in program
        assert 'counts["matching_observers"]' in program
        assert 'counts["matching_browsers"]' in program
        assert "os.getpgid(pid)" in program
        assert "stat.S_IMODE(path.stat().st_mode)!=mode" in program
        assert "os.chmod(str(path),mode)" in program


def test_manifest_rejects_wrong_modes_and_public_exact_input():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        private_index = next(
            index for index, row in enumerate(fixture.payload["files"])
            if row["local_path"] == "private/exact.txt"
        )
        plan_index = next(
            index for index, row in enumerate(fixture.payload["files"])
            if row["local_path"] == "plan.json"
        )
        controller_index = next(
            index for index, row in enumerate(fixture.payload["files"])
            if row["local_path"].endswith("unattended_exact_observation_sequence.py")
        )
        for mutate in (
            lambda value: value["files"][private_index].update(mode="0644"),
            lambda value: value["files"][private_index].update(private=False, mode="0644"),
            lambda value: value["files"][plan_index].update(private=False, mode="0644"),
            lambda value: value["files"][controller_index].update(private=True, mode="0600"),
            lambda value: value["files"][0].update(mode="0600"),
        ):
            payload = json.loads(json.dumps(fixture.payload))
            mutate(payload)
            fixture.rehash_deployment(payload)
            manifest, digest = fixture.manifest(payload)
            with pytest.raises(launcher.LaunchError):
                launcher.load_manifest(manifest, digest, fixture.workspace)


def test_deployment_digest_pins_expected_file_mode():
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(Path(directory))
        original = launcher.deployment_digest(fixture.files, fixture.remote_root)
        changed = json.loads(json.dumps(fixture.files))
        changed[0]["mode"] = "0600"
        assert launcher.deployment_digest(changed, fixture.remote_root) != original
