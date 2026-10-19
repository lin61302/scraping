import ast
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = (
    ROOT
    / "automation"
    / "reports"
    / "ALL_SOURCE_FIRSTPASS_20261014_CSM_EXACT4_PREP"
    / "run_exact4_repair.py"
)
SPEC = importlib.util.spec_from_file_location("csm_exact4_public_runner", RUNNER_PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def test_exact_four_runner_has_no_production_database_imports():
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"pymongo", "sqlalchemy", "psycopg2", "mysql", "sqlite3"})


def test_exact_four_runner_rejects_any_other_acceptance_window(tmp_path):
    with pytest.raises(RuntimeError, match="frozen to"):
        RUNNER.main(
            [
                "--urls-file",
                str(tmp_path / "exact.txt"),
                "--preserved-urls",
                str(tmp_path / "preserved.txt"),
                "--original-urls",
                str(tmp_path / "original.txt"),
                "--output-root",
                str(tmp_path / "out"),
                "--start",
                "2026-08-13",
                "--end",
                "2026-10-01",
            ]
        )


def test_exact_four_runner_exposes_only_hashed_exclusion_identity():
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    string_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "EXPECTED_IMAGE_ONLY_URL_SHA256" in RUNNER_PATH.read_text(encoding="utf-8")
    assert not any(value.startswith(("http://", "https://")) for value in string_literals)
    assert len(RUNNER.EXPECTED_IMAGE_ONLY_URL_SHA256) == 64


def test_exact_four_runner_returns_failure_when_recombination_is_not_accepted(
    monkeypatch, tmp_path
):
    exact = [f"item-{index}" for index in range(4)]
    preserved = [f"item-{index}" for index in range(4, 314)]
    monkeypatch.setattr(
        RUNNER,
        "validate_inputs",
        lambda *_: {
            "accepted": True,
            "exact_urls": exact,
            "preserved_urls": preserved,
            "original_urls": exact + preserved,
        },
    )

    def fail_observation(_):
        raise RuntimeError("synthetic observation failure")

    monkeypatch.setattr(RUNNER.csmonitor, "scrape_url", fail_observation)
    result = RUNNER.main(
        [
            "--urls-file",
            str(tmp_path / "exact.txt"),
            "--preserved-urls",
            str(tmp_path / "preserved.txt"),
            "--original-urls",
            str(tmp_path / "original.txt"),
            "--output-root",
            str(tmp_path / "out"),
            "--start",
            "2026-08-13",
            "--end",
            "2026-09-30",
        ]
    )
    assert result == 1
