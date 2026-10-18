from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
import unittest
from pathlib import Path


AUTOMATION = Path(__file__).resolve().parents[1]
if str(AUTOMATION) not in sys.path:
    sys.path.insert(0, str(AUTOMATION))

from ops.host_capacity_guard import (  # noqa: E402
    GIB,
    HostMetrics,
    Thresholds,
    evaluate_capacity,
    parser,
    thresholds_from_args,
)


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def healthy(**overrides: object) -> HostMetrics:
    values: dict[str, object] = {
        "filesystem_total_bytes": 1000 * GIB,
        "filesystem_free_bytes": 200 * GIB,
        "memory_total_bytes": 128 * GIB,
        "memory_available_bytes": 64 * GIB,
        "load5": 4.0,
        "cpu_count": 16,
        "swap_total_bytes": 16 * GIB,
        "swap_used_bytes": 1 * GIB,
        "scrape_processes": 2,
        "browser_processes": 1,
        "max_log_bytes": 100 * 1024**2,
        "configured_roots_total_bytes": 2 * GIB,
        "fd_soft_limit": 8192,
        "configured_log_roots": 1,
        "configured_output_roots": 1,
        "inspected_roots": 2,
        "filesystem_path": "/secret/workspace",
        "log_roots": ("/secret/logs",),
        "output_roots": ("/secret/outputs",),
        "max_log_path": "/secret/logs/controller.log",
    }
    values.update(overrides)
    return HostMetrics(**values)  # type: ignore[arg-type]


class HostCapacityGuardTests(unittest.TestCase):
    def test_healthy_snapshot_is_green_with_four_plus_one(self):
        report = evaluate_capacity(healthy(), generated_at=NOW)
        self.assertEqual(report["status"], "green")
        self.assertTrue(report["launch_allowed"])
        self.assertEqual(report["recommended_limits"], {"network_workers": 4, "browser_workers": 1})
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["generated_at"], "2026-10-18T12:00:00Z")

    def test_default_report_does_not_leak_paths_or_host_details(self):
        report = evaluate_capacity(healthy(), generated_at=NOW)
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("/secret", rendered)
        self.assertNotIn("paths", report)
        self.assertNotIn("hostname", rendered.casefold())

        explicit = evaluate_capacity(healthy(), include_paths=True, generated_at=NOW)
        self.assertEqual(explicit["paths"]["filesystem"], "/secret/workspace")  # type: ignore[index]
        self.assertEqual(explicit["paths"]["largest_log"], "/secret/logs/controller.log")  # type: ignore[index]

    def test_reduced_recommendation_uses_two_network_and_zero_browser(self):
        report = evaluate_capacity(healthy(
            filesystem_free_bytes=50 * GIB,
            memory_available_bytes=15 * GIB,
            load5=12.8,  # Exactly 0.8 per CPU.
            swap_used_bytes=8 * GIB,  # Exactly 50 percent.
            scrape_processes=6,
            browser_processes=1,  # Combined count is 7, strictly greater than 6.
            max_log_bytes=1 * GIB,
            configured_roots_total_bytes=25 * GIB,
            fd_soft_limit=2048,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "reduced")
        self.assertTrue(report["launch_allowed"])
        self.assertEqual(report["recommended_limits"], {"network_workers": 2, "browser_workers": 0})
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertTrue({
            "filesystem_free_bytes_reduced",
            "memory_available_bytes_reduced",
            "load_per_cpu_reduced",
            "swap_used_percent_reduced",
            "active_processes_reduced",
            "max_log_bytes_reduced",
            "configured_roots_bytes_reduced",
            "fd_soft_limit_reduced",
        }.issubset(codes))

    def test_each_block_boundary_or_strict_limit_blocks_launch(self):
        scenarios = {
            "filesystem_bytes": {"filesystem_free_bytes": 29 * GIB},
            "filesystem_percent": {
                "filesystem_total_bytes": 2000 * GIB,
                "filesystem_free_bytes": 59 * GIB,
            },
            "memory_bytes": {"memory_available_bytes": 7 * GIB},
            "memory_percent": {
                "memory_total_bytes": 128 * GIB,
                "memory_available_bytes": 12 * GIB,
            },
            "load": {"load5": 24.0},  # Exactly 1.5 per CPU.
            "swap_pressure": {
                "swap_used_bytes": 12 * GIB,  # Exactly 75 percent.
                "memory_available_bytes": 15 * GIB,
            },
            "processes": {"scrape_processes": 12, "browser_processes": 1},
            "max_log": {"max_log_bytes": 4 * GIB},
            "roots": {"configured_roots_total_bytes": 50 * GIB},
            "fd": {"fd_soft_limit": 1023},
        }
        for name, values in scenarios.items():
            with self.subTest(name=name):
                report = evaluate_capacity(healthy(**values), generated_at=NOW)
                self.assertEqual(report["status"], "blocked")
                self.assertFalse(report["launch_allowed"])
                self.assertEqual(report["recommended_limits"], {"network_workers": 0, "browser_workers": 0})

    def test_sticky_high_swap_with_healthy_current_memory_reduces_instead_of_blocks(self):
        report = evaluate_capacity(healthy(
            swap_used_bytes=12 * GIB,
            memory_available_bytes=48 * GIB,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "reduced")
        self.assertTrue(report["launch_allowed"])
        self.assertEqual(report["recommended_limits"], {"network_workers": 2, "browser_workers": 0})
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertIn("swap_used_percent_reduced", codes)
        self.assertNotIn("swap_pressure_blocked", codes)

    def test_blocked_reason_overrides_reduced_reasons(self):
        report = evaluate_capacity(healthy(
            filesystem_free_bytes=50 * GIB,
            load5=24.0,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "blocked")
        severities = {row["severity"] for row in report["reasons"]}  # type: ignore[index]
        self.assertEqual(severities, {"reduced", "blocked"})

    def test_missing_metrics_and_collection_errors_fail_closed(self):
        report = evaluate_capacity(healthy(
            memory_available_bytes=None,
            collection_errors=("memory_or_swap_collection_failed",),
        ), generated_at=NOW)
        self.assertEqual(report["status"], "blocked")
        codes = [row["code"] for row in report["reasons"]]  # type: ignore[index]
        self.assertIn("memory_available_unavailable", codes)
        self.assertIn("collection_error", codes)

    def test_collection_warning_reduces_but_does_not_block(self):
        report = evaluate_capacity(healthy(
            collection_warnings=("configured_root_scan_incomplete",),
        ), generated_at=NOW)
        self.assertEqual(report["status"], "reduced")
        self.assertEqual(report["recommended_limits"], {"network_workers": 2, "browser_workers": 0})

    def test_cli_threshold_and_worker_overrides_are_applied_without_live_probe(self):
        args = parser().parse_args([
            "--filesystem-block-gib", "20",
            "--filesystem-reduce-gib", "40",
            "--memory-block-gib", "4",
            "--memory-reduce-gib", "12",
            "--green-network-workers", "3",
            "--green-browser-workers", "0",
            "--reduced-network-workers", "1",
        ])
        limits = thresholds_from_args(args)
        self.assertEqual(limits.filesystem_block_bytes, 20 * GIB)
        self.assertEqual(limits.filesystem_reduce_bytes, 40 * GIB)
        self.assertEqual(limits.memory_block_bytes, 4 * GIB)
        self.assertEqual(limits.memory_reduce_bytes, 12 * GIB)
        report = evaluate_capacity(healthy(), limits, generated_at=NOW)
        self.assertEqual(report["recommended_limits"], {"network_workers": 3, "browser_workers": 0})

    def test_practical_disk_percentage_defaults_are_three_and_eight(self):
        limits = Thresholds()
        self.assertEqual(limits.filesystem_block_percent, 3.0)
        self.assertEqual(limits.filesystem_reduce_percent, 8.0)

    def test_threshold_validation_rejects_inverted_severity(self):
        with self.assertRaises(ValueError):
            Thresholds(
                filesystem_block_bytes=70 * GIB,
                filesystem_reduce_bytes=60 * GIB,
            ).validate()


if __name__ == "__main__":
    unittest.main()
