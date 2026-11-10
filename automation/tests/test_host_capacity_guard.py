from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
import unittest
from unittest.mock import patch
from pathlib import Path


AUTOMATION = Path(__file__).resolve().parents[1]
if str(AUTOMATION) not in sys.path:
    sys.path.insert(0, str(AUTOMATION))

from ops.host_capacity_guard import (  # noqa: E402
    GIB,
    HostMetrics,
    Thresholds,
    _parse_macos_cpu_idle,
    _read_macos_cpu_idle,
    build_capacity_scope,
    evaluate_capacity,
    main,
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
        "platform": "linux",
        "cpu_idle_percent": None,
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

    def test_darwin_high_load_with_independent_cpu_headroom_is_reduced(self):
        report = evaluate_capacity(healthy(
            platform="darwin",
            load5=80.0,
            cpu_count=16,
            cpu_idle_percent=45.0,
            scrape_processes=0,
            browser_processes=0,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "reduced")
        self.assertTrue(report["launch_allowed"])
        self.assertEqual(report["recommended_limits"], {"network_workers": 2, "browser_workers": 0})
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertIn("darwin_high_load_reduced_with_cpu_headroom", codes)
        self.assertNotIn("load_per_cpu_blocked", codes)
        self.assertEqual(report["metrics"]["cpu_idle_percent"], 45.0)  # type: ignore[index]
        self.assertEqual(report["metrics"]["cpu_busy_percent"], 55.0)  # type: ignore[index]

    def test_darwin_high_load_with_low_cpu_idle_blocks(self):
        report = evaluate_capacity(healthy(
            platform="darwin",
            load5=80.0,
            cpu_count=16,
            cpu_idle_percent=15.0,
            scrape_processes=0,
            browser_processes=0,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "blocked")
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertIn("load_per_cpu_blocked", codes)
        self.assertIn("darwin_cpu_idle_percent_blocked", codes)

    def test_darwin_high_load_without_cpu_idle_sample_blocks(self):
        report = evaluate_capacity(healthy(
            platform="darwin",
            load5=80.0,
            cpu_count=16,
            cpu_idle_percent=None,
            scrape_processes=0,
            browser_processes=0,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "blocked")
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertIn("darwin_cpu_idle_unavailable", codes)

    def test_darwin_high_load_requires_quiescent_campaign_processes(self):
        report = evaluate_capacity(healthy(
            platform="darwin",
            load5=80.0,
            cpu_count=16,
            cpu_idle_percent=60.0,
            scrape_processes=1,
            browser_processes=0,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "blocked")
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertIn("load_per_cpu_blocked", codes)

    def test_darwin_high_load_requires_healthy_current_memory(self):
        report = evaluate_capacity(healthy(
            platform="darwin",
            load5=80.0,
            cpu_count=16,
            cpu_idle_percent=60.0,
            memory_available_bytes=15 * GIB,
            scrape_processes=0,
            browser_processes=0,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "blocked")
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertIn("load_per_cpu_blocked", codes)

    def test_darwin_high_load_below_hard_disk_floor_blocks(self):
        report = evaluate_capacity(healthy(
            platform="darwin",
            filesystem_free_bytes=29 * GIB,
            load5=80.0,
            cpu_count=16,
            cpu_idle_percent=60.0,
            scrape_processes=0,
            browser_processes=0,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "blocked")
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertIn("filesystem_free_bytes_blocked", codes)
        self.assertIn("load_per_cpu_blocked", codes)

    def test_darwin_high_load_in_reduced_disk_band_stays_reduced(self):
        report = evaluate_capacity(healthy(
            platform="darwin",
            filesystem_free_bytes=50 * GIB,
            load5=80.0,
            cpu_count=16,
            cpu_idle_percent=60.0,
            scrape_processes=0,
            browser_processes=0,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "reduced")
        self.assertTrue(report["launch_allowed"])
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertIn("filesystem_free_bytes_reduced", codes)
        self.assertIn("darwin_high_load_reduced_with_cpu_headroom", codes)

    def test_darwin_high_load_with_invalid_cpu_idle_blocks(self):
        for value in (float("nan"), 101.0):
            with self.subTest(value=value):
                report = evaluate_capacity(healthy(
                    platform="darwin",
                    load5=80.0,
                    cpu_count=16,
                    cpu_idle_percent=value,
                    scrape_processes=0,
                    browser_processes=0,
                ), generated_at=NOW)
                self.assertEqual(report["status"], "blocked")
                codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
                self.assertIn("darwin_cpu_idle_invalid", codes)

    def test_normal_darwin_load_does_not_require_cpu_idle_sample(self):
        report = evaluate_capacity(healthy(
            platform="darwin",
            cpu_idle_percent=None,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "green")
        self.assertTrue(report["launch_allowed"])

    def test_linux_high_load_still_blocks_even_with_cpu_idle_value(self):
        report = evaluate_capacity(healthy(
            platform="linux",
            load5=80.0,
            cpu_count=16,
            cpu_idle_percent=60.0,
            scrape_processes=0,
            browser_processes=0,
        ), generated_at=NOW)
        self.assertEqual(report["status"], "blocked")
        codes = {row["code"] for row in report["reasons"]}  # type: ignore[index]
        self.assertIn("load_per_cpu_blocked", codes)

    def test_macos_cpu_idle_parser_uses_final_top_sample(self):
        text = """
CPU usage: 40.0% user, 20.0% sys, 40.0% idle
CPU usage: 16.77% user, 19.34% sys, 63.88% idle
"""
        self.assertEqual(_parse_macos_cpu_idle(text), 63.88)

    def test_macos_cpu_idle_parser_fails_closed_on_missing_or_invalid_value(self):
        with self.assertRaises(RuntimeError):
            _parse_macos_cpu_idle("no cpu summary")
        with self.assertRaises(RuntimeError):
            _parse_macos_cpu_idle("CPU usage: 0% user, 0% sys, 101% idle")

    def test_macos_cpu_idle_reader_uses_two_bounded_top_samples(self):
        top_output = "CPU usage: 10% user, 10% sys, 80% idle\n"
        with patch("ops.host_capacity_guard._run_readonly", return_value=top_output) as run:
            self.assertEqual(_read_macos_cpu_idle(), 80.0)
        run.assert_called_once_with(("/usr/bin/top", "-l", "2", "-n", "0", "-s", "1"))

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
            "--darwin-high-load-cpu-idle-safe-percent", "35",
            "--green-network-workers", "3",
            "--green-browser-workers", "0",
            "--reduced-network-workers", "1",
        ])
        limits = thresholds_from_args(args)
        self.assertEqual(limits.filesystem_block_bytes, 20 * GIB)
        self.assertEqual(limits.filesystem_reduce_bytes, 40 * GIB)
        self.assertEqual(limits.memory_block_bytes, 4 * GIB)
        self.assertEqual(limits.memory_reduce_bytes, 12 * GIB)
        self.assertEqual(limits.darwin_high_load_cpu_idle_safe_percent, 35.0)
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

    def test_threshold_validation_rejects_invalid_darwin_idle_percentage(self):
        for value in (-0.1, 100.1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Thresholds(
                        darwin_high_load_cpu_idle_safe_percent=value,
                    ).validate()

    def test_capacity_scope_exact_five_six_six_and_no_raw_values(self):
        roots = [Path(f"/private/runtime/root-{index}") for index in range(5)]
        scraper = [f"private-scraper-{index}" for index in range(6)]
        browser = [f"private-browser-{index}" for index in range(6)]
        scope = build_capacity_scope(roots[:1], roots[1:], scraper, browser)
        self.assertEqual(scope["root_count"], 5)
        self.assertEqual(scope["scraper_pattern_count"], 6)
        self.assertEqual(scope["browser_pattern_count"], 6)
        for key in ("root_set_sha256", "scraper_pattern_set_sha256",
                    "browser_pattern_set_sha256"):
            value = scope[key]
            self.assertIsInstance(value, str)
            self.assertEqual(len(value), 64)
            self.assertTrue(all(character in "0123456789abcdef" for character in value))
        rendered = json.dumps(scope, sort_keys=True)
        for raw_value in [*(str(item) for item in roots), *scraper, *browser]:
            self.assertNotIn(raw_value, rendered)

    def test_capacity_scope_is_order_independent_and_uses_effective_sets(self):
        roots = [Path(f"/private/runtime/root-{index}") for index in range(5)]
        scraper = [f"scraper-{index}" for index in range(6)]
        browser = [f"browser-{index}" for index in range(6)]
        expected = build_capacity_scope(roots[:1], roots[1:], scraper, browser)
        actual = build_capacity_scope(
            list(reversed(roots[:1])), list(reversed(roots[1:])),
            list(reversed(scraper)) + [scraper[0].upper()],
            list(reversed(browser)) + [browser[0].upper()],
        )
        self.assertEqual(actual, expected)

    def test_capacity_scope_changed_component_changes_only_its_hash(self):
        roots = [Path(f"/private/runtime/root-{index}") for index in range(5)]
        scraper = [f"scraper-{index}" for index in range(6)]
        browser = [f"browser-{index}" for index in range(6)]
        baseline = build_capacity_scope(roots[:1], roots[1:], scraper, browser)
        changed_root = build_capacity_scope(
            roots[:1], [*roots[1:-1], Path("/private/runtime/root-new")], scraper, browser,
        )
        changed_scraper = build_capacity_scope(
            roots[:1], roots[1:], [*scraper[:-1], "scraper-new"], browser,
        )
        changed_browser = build_capacity_scope(
            roots[:1], roots[1:], scraper, [*browser[:-1], "browser-new"],
        )
        self.assertNotEqual(changed_root["root_set_sha256"], baseline["root_set_sha256"])
        self.assertEqual(changed_root["scraper_pattern_set_sha256"], baseline["scraper_pattern_set_sha256"])
        self.assertEqual(changed_root["browser_pattern_set_sha256"], baseline["browser_pattern_set_sha256"])
        self.assertNotEqual(changed_scraper["scraper_pattern_set_sha256"], baseline["scraper_pattern_set_sha256"])
        self.assertEqual(changed_scraper["root_set_sha256"], baseline["root_set_sha256"])
        self.assertEqual(changed_scraper["browser_pattern_set_sha256"], baseline["browser_pattern_set_sha256"])
        self.assertNotEqual(changed_browser["browser_pattern_set_sha256"], baseline["browser_pattern_set_sha256"])
        self.assertEqual(changed_browser["root_set_sha256"], baseline["root_set_sha256"])
        self.assertEqual(changed_browser["scraper_pattern_set_sha256"], baseline["scraper_pattern_set_sha256"])

    def test_cli_receipt_adds_privacy_safe_scope_without_changing_decision(self):
        arguments = [
            "--log-root", "/private/runtime/logs",
            *sum((["--output-root", f"/private/runtime/root-{index}"]
                  for index in range(4)), []),
            *sum((["--scrape-pattern", f"private-scraper-{index}"]
                  for index in range(6)), []),
            *sum((["--browser-pattern", f"private-browser-{index}"]
                  for index in range(6)), []),
        ]
        with patch("ops.host_capacity_guard.collect_metrics", return_value=healthy()), \
                patch("builtins.print") as emit:
            exit_code = main(arguments)
        self.assertEqual(exit_code, 0)
        receipt = json.loads(emit.call_args.args[0])
        self.assertEqual(receipt["status"], "green")
        self.assertTrue(receipt["launch_allowed"])
        self.assertEqual(receipt["recommended_limits"], {
            "network_workers": 4, "browser_workers": 1,
        })
        self.assertEqual(receipt["scope"]["root_count"], 5)
        self.assertEqual(receipt["scope"]["scraper_pattern_count"], 6)
        self.assertEqual(receipt["scope"]["browser_pattern_count"], 6)
        rendered = json.dumps(receipt, sort_keys=True)
        self.assertNotIn("/private/runtime", rendered)
        self.assertNotIn("private-scraper", rendered)
        self.assertNotIn("private-browser", rendered)


if __name__ == "__main__":
    unittest.main()
