#!/usr/bin/env python3
"""Read-only host capacity guard for unattended scraping campaigns.

The guard deliberately does not start, stop, or modify any process or file.  It
collects a small host snapshot, emits sanitized JSON, and recommends one of
three bounded worker configurations:

* ``green``: 4 network workers and 1 browser worker
* ``reduced``: 2 network workers and no browser worker
* ``blocked``: no workers

Paths, process command lines, user names, and host names are omitted from the
default report.  Operators may explicitly request configured paths with
``--include-paths``.  Exit codes are 0 (green), 10 (reduced), and 20 (blocked).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import resource
import shutil
import stat
import subprocess
import sys
from typing import Iterable, Sequence


GIB = 1024**3
UTC = timezone.utc
EXIT_CODES = {"green": 0, "reduced": 10, "blocked": 20}
SEVERITY = {"green": 0, "reduced": 1, "blocked": 2}

DEFAULT_SCRAPE_PATTERNS = (
    "scrape",
    "observe_pure",
    "observer",
    "country_campaign",
    "unattended_country_sessions",
)
DEFAULT_BROWSER_PATTERNS = (
    "chromium",
    "google chrome",
    "chrome",
    "chromedriver",
    "firefox",
    "geckodriver",
    "playwright",
    "webkit",
)
LOG_SUFFIXES = frozenset({".log", ".out", ".err", ".jsonl"})


@dataclass(frozen=True)
class Thresholds:
    filesystem_block_bytes: int = 30 * GIB
    filesystem_reduce_bytes: int = 60 * GIB
    filesystem_block_percent: float = 3.0
    filesystem_reduce_percent: float = 8.0
    memory_block_bytes: int = 8 * GIB
    memory_reduce_bytes: int = 16 * GIB
    memory_block_percent: float = 10.0
    memory_reduce_percent: float = 20.0
    load_reduce_per_cpu: float = 0.8
    load_block_per_cpu: float = 1.5
    swap_reduce_percent: float = 50.0
    swap_block_percent: float = 75.0
    processes_reduce_above: int = 6
    processes_block_above: int = 12
    max_log_reduce_bytes: int = 1 * GIB
    max_log_block_bytes: int = 4 * GIB
    roots_reduce_bytes: int = 25 * GIB
    roots_block_bytes: int = 50 * GIB
    fd_reduce_below: int = 4096
    fd_block_below: int = 1024
    green_network_workers: int = 4
    green_browser_workers: int = 1
    reduced_network_workers: int = 2
    reduced_browser_workers: int = 0

    def validate(self) -> None:
        nonnegative = (
            self.filesystem_block_bytes,
            self.filesystem_reduce_bytes,
            self.memory_block_bytes,
            self.memory_reduce_bytes,
            self.processes_reduce_above,
            self.processes_block_above,
            self.max_log_reduce_bytes,
            self.max_log_block_bytes,
            self.roots_reduce_bytes,
            self.roots_block_bytes,
            self.fd_reduce_below,
            self.fd_block_below,
            self.green_network_workers,
            self.green_browser_workers,
            self.reduced_network_workers,
            self.reduced_browser_workers,
        )
        if any(value < 0 for value in nonnegative):
            raise ValueError("capacity thresholds and worker limits must be nonnegative")
        percentages = (
            self.filesystem_block_percent,
            self.filesystem_reduce_percent,
            self.memory_block_percent,
            self.memory_reduce_percent,
            self.swap_reduce_percent,
            self.swap_block_percent,
        )
        if any(value < 0 or value > 100 for value in percentages):
            raise ValueError("percentage thresholds must be between 0 and 100")
        ordered = (
            self.filesystem_block_bytes <= self.filesystem_reduce_bytes,
            self.filesystem_block_percent <= self.filesystem_reduce_percent,
            self.memory_block_bytes <= self.memory_reduce_bytes,
            self.memory_block_percent <= self.memory_reduce_percent,
            self.load_reduce_per_cpu <= self.load_block_per_cpu,
            self.swap_reduce_percent <= self.swap_block_percent,
            self.processes_reduce_above <= self.processes_block_above,
            self.max_log_reduce_bytes <= self.max_log_block_bytes,
            self.roots_reduce_bytes <= self.roots_block_bytes,
            self.fd_block_below <= self.fd_reduce_below,
        )
        if not all(ordered):
            raise ValueError("blocked and reduced thresholds are inconsistently ordered")


@dataclass(frozen=True)
class HostMetrics:
    filesystem_total_bytes: int | None
    filesystem_free_bytes: int | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    load5: float | None
    cpu_count: int | None
    swap_total_bytes: int | None
    swap_used_bytes: int | None
    scrape_processes: int | None
    browser_processes: int | None
    max_log_bytes: int | None
    configured_roots_total_bytes: int | None
    fd_soft_limit: int | None
    configured_log_roots: int = 0
    configured_output_roots: int = 0
    inspected_roots: int = 0
    collection_errors: tuple[str, ...] = ()
    collection_warnings: tuple[str, ...] = ()
    filesystem_path: str | None = None
    log_roots: tuple[str, ...] = ()
    output_roots: tuple[str, ...] = ()
    max_log_path: str | None = None


@dataclass(frozen=True)
class RootScan:
    total_bytes: int | None
    max_log_bytes: int | None
    inspected_roots: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    max_log_path: str | None


def _percent(part: int | None, total: int | None) -> float | None:
    if part is None or total is None or total <= 0:
        return None
    return part * 100.0 / total


def _reason(code: str, severity: str, actual: int | float | str | None,
            threshold: int | float | str | None, message: str) -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "actual": actual,
        "threshold": threshold,
        "message": message,
    }


def evaluate_capacity(metrics: HostMetrics, thresholds: Thresholds | None = None,
                      *, include_paths: bool = False,
                      generated_at: datetime | None = None) -> dict[str, object]:
    """Evaluate injected metrics and return a sanitized capacity report."""
    limits = thresholds or Thresholds()
    limits.validate()
    reasons: list[dict[str, object]] = []

    def missing(name: str, value: object) -> bool:
        if value is None:
            reasons.append(_reason(
                f"{name}_unavailable", "blocked", None, "required",
                f"Required {name.replace('_', ' ')} metric is unavailable.",
            ))
            return True
        return False

    fs_missing = missing("filesystem_capacity", metrics.filesystem_total_bytes) | missing(
        "filesystem_free", metrics.filesystem_free_bytes)
    fs_free_percent = _percent(metrics.filesystem_free_bytes, metrics.filesystem_total_bytes)
    if not fs_missing:
        assert metrics.filesystem_free_bytes is not None and fs_free_percent is not None
        if metrics.filesystem_free_bytes < limits.filesystem_block_bytes:
            reasons.append(_reason(
                "filesystem_free_bytes_blocked", "blocked", metrics.filesystem_free_bytes,
                limits.filesystem_block_bytes, "Filesystem free bytes are below the blocked threshold.",
            ))
        elif metrics.filesystem_free_bytes < limits.filesystem_reduce_bytes:
            reasons.append(_reason(
                "filesystem_free_bytes_reduced", "reduced", metrics.filesystem_free_bytes,
                limits.filesystem_reduce_bytes, "Filesystem free bytes are below the reduced threshold.",
            ))
        if fs_free_percent < limits.filesystem_block_percent:
            reasons.append(_reason(
                "filesystem_free_percent_blocked", "blocked", round(fs_free_percent, 3),
                limits.filesystem_block_percent, "Filesystem free percentage is below the blocked threshold.",
            ))
        elif fs_free_percent < limits.filesystem_reduce_percent:
            reasons.append(_reason(
                "filesystem_free_percent_reduced", "reduced", round(fs_free_percent, 3),
                limits.filesystem_reduce_percent, "Filesystem free percentage is below the reduced threshold.",
            ))

    memory_missing = missing("memory_capacity", metrics.memory_total_bytes) | missing(
        "memory_available", metrics.memory_available_bytes)
    memory_available_percent = _percent(metrics.memory_available_bytes, metrics.memory_total_bytes)
    if not memory_missing:
        assert metrics.memory_available_bytes is not None and memory_available_percent is not None
        if metrics.memory_available_bytes < limits.memory_block_bytes:
            reasons.append(_reason(
                "memory_available_bytes_blocked", "blocked", metrics.memory_available_bytes,
                limits.memory_block_bytes, "Available memory bytes are below the blocked threshold.",
            ))
        elif metrics.memory_available_bytes < limits.memory_reduce_bytes:
            reasons.append(_reason(
                "memory_available_bytes_reduced", "reduced", metrics.memory_available_bytes,
                limits.memory_reduce_bytes, "Available memory bytes are below the reduced threshold.",
            ))
        if memory_available_percent < limits.memory_block_percent:
            reasons.append(_reason(
                "memory_available_percent_blocked", "blocked", round(memory_available_percent, 3),
                limits.memory_block_percent, "Available memory percentage is below the blocked threshold.",
            ))
        elif memory_available_percent < limits.memory_reduce_percent:
            reasons.append(_reason(
                "memory_available_percent_reduced", "reduced", round(memory_available_percent, 3),
                limits.memory_reduce_percent, "Available memory percentage is below the reduced threshold.",
            ))

    load_missing = missing("load5", metrics.load5) | missing("cpu_count", metrics.cpu_count)
    load_per_cpu: float | None = None
    if not load_missing:
        assert metrics.load5 is not None and metrics.cpu_count is not None
        if metrics.cpu_count <= 0:
            reasons.append(_reason(
                "cpu_count_invalid", "blocked", metrics.cpu_count, ">=1",
                "CPU count must be positive before load can be normalized.",
            ))
        else:
            load_per_cpu = metrics.load5 / metrics.cpu_count
            if load_per_cpu >= limits.load_block_per_cpu:
                reasons.append(_reason(
                    "load_per_cpu_blocked", "blocked", round(load_per_cpu, 3),
                    limits.load_block_per_cpu, "Normalized five-minute load reached the blocked threshold.",
                ))
            elif load_per_cpu >= limits.load_reduce_per_cpu:
                reasons.append(_reason(
                    "load_per_cpu_reduced", "reduced", round(load_per_cpu, 3),
                    limits.load_reduce_per_cpu, "Normalized five-minute load reached the reduced threshold.",
                ))

    swap_missing = missing("swap_capacity", metrics.swap_total_bytes) | missing(
        "swap_used", metrics.swap_used_bytes)
    swap_used_percent: float | None = None
    if not swap_missing:
        assert metrics.swap_total_bytes is not None and metrics.swap_used_bytes is not None
        swap_used_percent = 0.0 if metrics.swap_total_bytes == 0 else _percent(
            metrics.swap_used_bytes, metrics.swap_total_bytes)
        if swap_used_percent is None or metrics.swap_total_bytes < 0 or metrics.swap_used_bytes < 0:
            reasons.append(_reason(
                "swap_values_invalid", "blocked", None, "nonnegative",
                "Swap metrics are invalid.",
            ))
        elif swap_used_percent >= limits.swap_block_percent:
            reasons.append(_reason(
                "swap_used_percent_blocked", "blocked", round(swap_used_percent, 3),
                limits.swap_block_percent, "Swap use reached the blocked threshold.",
            ))
        elif swap_used_percent >= limits.swap_reduce_percent:
            reasons.append(_reason(
                "swap_used_percent_reduced", "reduced", round(swap_used_percent, 3),
                limits.swap_reduce_percent, "Swap use reached the reduced threshold.",
            ))

    process_missing = missing("scrape_process_count", metrics.scrape_processes) | missing(
        "browser_process_count", metrics.browser_processes)
    active_processes: int | None = None
    if not process_missing:
        assert metrics.scrape_processes is not None and metrics.browser_processes is not None
        active_processes = metrics.scrape_processes + metrics.browser_processes
        if metrics.scrape_processes < 0 or metrics.browser_processes < 0:
            reasons.append(_reason(
                "process_counts_invalid", "blocked", active_processes, "nonnegative",
                "Process counts must be nonnegative.",
            ))
        elif active_processes > limits.processes_block_above:
            reasons.append(_reason(
                "active_processes_blocked", "blocked", active_processes,
                f">{limits.processes_block_above}", "Scrape and browser process count exceeded the blocked threshold.",
            ))
        elif active_processes > limits.processes_reduce_above:
            reasons.append(_reason(
                "active_processes_reduced", "reduced", active_processes,
                f">{limits.processes_reduce_above}", "Scrape and browser process count exceeded the reduced threshold.",
            ))

    if not missing("max_log_size", metrics.max_log_bytes):
        assert metrics.max_log_bytes is not None
        if metrics.max_log_bytes >= limits.max_log_block_bytes:
            reasons.append(_reason(
                "max_log_bytes_blocked", "blocked", metrics.max_log_bytes,
                limits.max_log_block_bytes, "Largest configured log reached the blocked threshold.",
            ))
        elif metrics.max_log_bytes >= limits.max_log_reduce_bytes:
            reasons.append(_reason(
                "max_log_bytes_reduced", "reduced", metrics.max_log_bytes,
                limits.max_log_reduce_bytes, "Largest configured log reached the reduced threshold.",
            ))

    if not missing("configured_roots_size", metrics.configured_roots_total_bytes):
        assert metrics.configured_roots_total_bytes is not None
        if metrics.configured_roots_total_bytes >= limits.roots_block_bytes:
            reasons.append(_reason(
                "configured_roots_bytes_blocked", "blocked", metrics.configured_roots_total_bytes,
                limits.roots_block_bytes, "Configured log and output roots reached the blocked threshold.",
            ))
        elif metrics.configured_roots_total_bytes >= limits.roots_reduce_bytes:
            reasons.append(_reason(
                "configured_roots_bytes_reduced", "reduced", metrics.configured_roots_total_bytes,
                limits.roots_reduce_bytes, "Configured log and output roots reached the reduced threshold.",
            ))

    if not missing("fd_soft_limit", metrics.fd_soft_limit):
        assert metrics.fd_soft_limit is not None
        if metrics.fd_soft_limit < limits.fd_block_below:
            reasons.append(_reason(
                "fd_soft_limit_blocked", "blocked", metrics.fd_soft_limit,
                limits.fd_block_below, "Soft file-descriptor limit is below the blocked threshold.",
            ))
        elif metrics.fd_soft_limit < limits.fd_reduce_below:
            reasons.append(_reason(
                "fd_soft_limit_reduced", "reduced", metrics.fd_soft_limit,
                limits.fd_reduce_below, "Soft file-descriptor limit is below the reduced threshold.",
            ))

    for code in sorted(set(metrics.collection_errors)):
        reasons.append(_reason(
            "collection_error", "blocked", code, "none",
            "A required host metric could not be collected.",
        ))
    for code in sorted(set(metrics.collection_warnings)):
        reasons.append(_reason(
            "collection_warning", "reduced", code, "none",
            "Host inspection was incomplete; use reduced capacity.",
        ))

    state = "green"
    if reasons:
        state = max((str(row["severity"]) for row in reasons), key=SEVERITY.__getitem__)
    if state == "green":
        workers = {
            "network_workers": limits.green_network_workers,
            "browser_workers": limits.green_browser_workers,
        }
    elif state == "reduced":
        workers = {
            "network_workers": limits.reduced_network_workers,
            "browser_workers": limits.reduced_browser_workers,
        }
    else:
        workers = {"network_workers": 0, "browser_workers": 0}

    public_metrics: dict[str, object] = {
        "filesystem_total_bytes": metrics.filesystem_total_bytes,
        "filesystem_free_bytes": metrics.filesystem_free_bytes,
        "filesystem_free_percent": None if fs_free_percent is None else round(fs_free_percent, 3),
        "memory_total_bytes": metrics.memory_total_bytes,
        "memory_available_bytes": metrics.memory_available_bytes,
        "memory_available_percent": None if memory_available_percent is None else round(memory_available_percent, 3),
        "load5": metrics.load5,
        "cpu_count": metrics.cpu_count,
        "load5_per_cpu": None if load_per_cpu is None else round(load_per_cpu, 3),
        "swap_total_bytes": metrics.swap_total_bytes,
        "swap_used_bytes": metrics.swap_used_bytes,
        "swap_used_percent": None if swap_used_percent is None else round(swap_used_percent, 3),
        "scrape_processes": metrics.scrape_processes,
        "browser_processes": metrics.browser_processes,
        "active_scrape_browser_processes": active_processes,
        "max_log_bytes": metrics.max_log_bytes,
        "configured_roots_total_bytes": metrics.configured_roots_total_bytes,
        "fd_soft_limit": metrics.fd_soft_limit,
        "configured_log_roots": metrics.configured_log_roots,
        "configured_output_roots": metrics.configured_output_roots,
        "inspected_roots": metrics.inspected_roots,
    }
    report: dict[str, object] = {
        "schema_version": 1,
        "kind": "host_capacity_guard",
        "generated_at": (generated_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "status": state,
        "recommended_limits": workers,
        "launch_allowed": state != "blocked",
        "metrics": public_metrics,
        "thresholds": asdict(limits),
        "reasons": reasons,
    }
    if include_paths:
        report["paths"] = {
            "filesystem": metrics.filesystem_path,
            "log_roots": list(metrics.log_roots),
            "output_roots": list(metrics.output_roots),
            "largest_log": metrics.max_log_path,
        }
    return report


def _read_linux_memory(proc_meminfo: Path = Path("/proc/meminfo")) -> tuple[int, int, int, int]:
    values: dict[str, int] = {}
    for line in proc_meminfo.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Za-z_()]+):\s+(\d+)\s+kB", line.strip())
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    required = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
    if any(name not in values for name in required):
        raise RuntimeError("linux_meminfo_incomplete")
    return (
        values["MemTotal"], values["MemAvailable"], values["SwapTotal"],
        max(0, values["SwapTotal"] - values["SwapFree"]),
    )


def _run_readonly(command: Sequence[str]) -> str:
    result = subprocess.run(
        list(command), capture_output=True, text=True, timeout=5, check=False,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    if result.returncode != 0:
        raise RuntimeError("readonly_command_failed")
    return result.stdout


def _read_macos_memory() -> tuple[int, int, int, int]:
    total = int(_run_readonly(("/usr/sbin/sysctl", "-n", "hw.memsize")).strip())
    vm = _run_readonly(("/usr/bin/vm_stat",))
    page_match = re.search(r"page size of (\d+) bytes", vm)
    if not page_match:
        raise RuntimeError("macos_vm_stat_page_size_missing")
    page_size = int(page_match.group(1))
    pages: dict[str, int] = {}
    for line in vm.splitlines():
        match = re.fullmatch(r"([^:]+):\s+(\d+)\.", line.strip())
        if match:
            pages[match.group(1)] = int(match.group(2))
    available_names = ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
    if not any(name in pages for name in available_names):
        raise RuntimeError("macos_vm_stat_available_missing")
    available = sum(pages.get(name, 0) for name in available_names) * page_size
    swap_text = _run_readonly(("/usr/sbin/sysctl", "-n", "vm.swapusage"))
    swap_match = re.search(r"total\s*=\s*([\d.]+)([KMG])\s+used\s*=\s*([\d.]+)([KMG])", swap_text)
    if not swap_match:
        raise RuntimeError("macos_swapusage_unparseable")
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
    swap_total = int(float(swap_match.group(1)) * multipliers[swap_match.group(2)])
    swap_used = int(float(swap_match.group(3)) * multipliers[swap_match.group(4)])
    return total, min(total, available), swap_total, swap_used


def _process_commands() -> Iterable[tuple[int, str]]:
    if sys.platform.startswith("linux"):
        proc = Path("/proc")
        for candidate in proc.iterdir():
            if not candidate.name.isdigit():
                continue
            pid = int(candidate.name)
            try:
                command = (candidate / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            yield pid, command
        return
    if sys.platform == "darwin":
        text = _run_readonly(("/bin/ps", "-axo", "pid=,command="))
        for line in text.splitlines():
            match = re.match(r"\s*(\d+)\s+(.*)", line)
            if match:
                yield int(match.group(1)), match.group(2)
        return
    raise RuntimeError("unsupported_process_platform")


def count_campaign_processes(scrape_patterns: Sequence[str], browser_patterns: Sequence[str]) -> tuple[int, int]:
    scrape_terms = tuple(item.casefold() for item in scrape_patterns if item)
    browser_terms = tuple(item.casefold() for item in browser_patterns if item)
    scrape = 0
    browser = 0
    own = {os.getpid(), os.getppid()}
    for pid, command in _process_commands():
        if pid in own:
            continue
        folded = command.casefold()
        if any(term in folded for term in browser_terms):
            browser += 1
        elif any(term in folded for term in scrape_terms):
            scrape += 1
    return scrape, browser


def _normalized_roots(paths: Sequence[Path]) -> list[Path]:
    unique = sorted({path.expanduser().resolve(strict=False) for path in paths}, key=lambda item: (len(item.parts), str(item)))
    result: list[Path] = []
    for candidate in unique:
        if any(candidate == parent or parent in candidate.parents for parent in result):
            continue
        result.append(candidate)
    return result


def _is_log_file(path: Path) -> bool:
    folded = path.name.casefold()
    return path.suffix.casefold() in LOG_SUFFIXES or "log" in folded


def _walk_regular_files(root: Path) -> Iterable[tuple[Path, os.stat_result]]:
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                mode = info.st_mode
                if stat.S_ISDIR(mode):
                    stack.append(Path(entry.path))
                elif stat.S_ISREG(mode):
                    yield Path(entry.path), info


def scan_roots(log_roots: Sequence[Path], output_roots: Sequence[Path]) -> RootScan:
    all_roots = _normalized_roots([*log_roots, *output_roots])
    normalized_logs = _normalized_roots(log_roots)
    errors: list[str] = []
    warnings: list[str] = []
    inspected = 0
    seen: set[tuple[int, int]] = set()
    total = 0
    for root in all_roots:
        if not root.is_dir() or root.is_symlink():
            errors.append("configured_root_unavailable")
            continue
        inspected += 1
        try:
            for _, info in _walk_regular_files(root):
                identity = (info.st_dev, info.st_ino)
                if identity not in seen:
                    seen.add(identity)
                    total += info.st_size
        except (OSError, PermissionError):
            warnings.append("configured_root_scan_incomplete")

    max_log = 0
    max_path: str | None = None
    for root in normalized_logs:
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            for path, info in _walk_regular_files(root):
                if _is_log_file(path) and info.st_size > max_log:
                    max_log = info.st_size
                    max_path = str(path)
        except (OSError, PermissionError):
            if "configured_root_scan_incomplete" not in warnings:
                warnings.append("configured_root_scan_incomplete")
    if all_roots and inspected == 0:
        return RootScan(None, None, inspected, tuple(sorted(set(errors))), tuple(sorted(set(warnings))), None)
    return RootScan(total, max_log, inspected, tuple(sorted(set(errors))), tuple(sorted(set(warnings))), max_path)


def collect_metrics(filesystem_path: Path, log_roots: Sequence[Path], output_roots: Sequence[Path],
                    scrape_patterns: Sequence[str] = DEFAULT_SCRAPE_PATTERNS,
                    browser_patterns: Sequence[str] = DEFAULT_BROWSER_PATTERNS) -> HostMetrics:
    """Collect one read-only host snapshot without exposing paths in the report."""
    errors: list[str] = []
    warnings: list[str] = []
    fs_total: int | None = None
    fs_free: int | None = None
    try:
        disk = shutil.disk_usage(filesystem_path)
        fs_total, fs_free = disk.total, disk.free
    except OSError:
        errors.append("filesystem_stat_failed")

    memory_total: int | None = None
    memory_available: int | None = None
    swap_total: int | None = None
    swap_used: int | None = None
    try:
        if sys.platform.startswith("linux"):
            memory_total, memory_available, swap_total, swap_used = _read_linux_memory()
        elif sys.platform == "darwin":
            memory_total, memory_available, swap_total, swap_used = _read_macos_memory()
        else:
            raise RuntimeError("unsupported_memory_platform")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        errors.append("memory_or_swap_collection_failed")

    load5: float | None = None
    try:
        load5 = float(os.getloadavg()[1])
    except (AttributeError, OSError):
        errors.append("load_collection_failed")
    cpu_count = os.cpu_count()
    if cpu_count is None:
        errors.append("cpu_count_collection_failed")

    scrape_count: int | None = None
    browser_count: int | None = None
    try:
        scrape_count, browser_count = count_campaign_processes(scrape_patterns, browser_patterns)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        errors.append("process_collection_failed")

    root_scan = scan_roots(log_roots, output_roots)
    errors.extend(root_scan.errors)
    warnings.extend(root_scan.warnings)

    fd_soft: int | None = None
    try:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        fd_soft = int(soft)
    except (OSError, ValueError):
        errors.append("fd_limit_collection_failed")

    return HostMetrics(
        filesystem_total_bytes=fs_total,
        filesystem_free_bytes=fs_free,
        memory_total_bytes=memory_total,
        memory_available_bytes=memory_available,
        load5=load5,
        cpu_count=cpu_count,
        swap_total_bytes=swap_total,
        swap_used_bytes=swap_used,
        scrape_processes=scrape_count,
        browser_processes=browser_count,
        max_log_bytes=root_scan.max_log_bytes,
        configured_roots_total_bytes=root_scan.total_bytes,
        fd_soft_limit=fd_soft,
        configured_log_roots=len(log_roots),
        configured_output_roots=len(output_roots),
        inspected_roots=root_scan.inspected_roots,
        collection_errors=tuple(sorted(set(errors))),
        collection_warnings=tuple(sorted(set(warnings))),
        filesystem_path=str(filesystem_path),
        log_roots=tuple(str(item) for item in log_roots),
        output_roots=tuple(str(item) for item in output_roots),
        max_log_path=root_scan.max_log_path,
    )


def _gib(value: float) -> int:
    if value < 0:
        raise argparse.ArgumentTypeError("GiB values must be nonnegative")
    return int(value * GIB)


def parser() -> argparse.ArgumentParser:
    automation_root = Path(__file__).resolve().parents[1]
    workspace_root = automation_root.parent
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--filesystem-path", type=Path, default=workspace_root)
    result.add_argument("--log-root", action="append", type=Path, default=None)
    result.add_argument("--output-root", action="append", type=Path, default=None)
    result.add_argument("--scrape-pattern", action="append", default=None)
    result.add_argument("--browser-pattern", action="append", default=None)
    result.add_argument("--include-paths", action="store_true")
    result.add_argument("--pretty", action="store_true")
    result.add_argument("--always-zero", action="store_true", help="emit the report but always exit zero")

    result.add_argument("--filesystem-block-gib", type=float, default=30.0)
    result.add_argument("--filesystem-reduce-gib", type=float, default=60.0)
    result.add_argument("--filesystem-block-percent", type=float, default=3.0)
    result.add_argument("--filesystem-reduce-percent", type=float, default=8.0)
    result.add_argument("--memory-block-gib", type=float, default=8.0)
    result.add_argument("--memory-reduce-gib", type=float, default=16.0)
    result.add_argument("--memory-block-percent", type=float, default=10.0)
    result.add_argument("--memory-reduce-percent", type=float, default=20.0)
    result.add_argument("--load-reduce-per-cpu", type=float, default=0.8)
    result.add_argument("--load-block-per-cpu", type=float, default=1.5)
    result.add_argument("--swap-reduce-percent", type=float, default=50.0)
    result.add_argument("--swap-block-percent", type=float, default=75.0)
    result.add_argument("--processes-reduce-above", type=int, default=6)
    result.add_argument("--processes-block-above", type=int, default=12)
    result.add_argument("--max-log-reduce-gib", type=float, default=1.0)
    result.add_argument("--max-log-block-gib", type=float, default=4.0)
    result.add_argument("--roots-reduce-gib", type=float, default=25.0)
    result.add_argument("--roots-block-gib", type=float, default=50.0)
    result.add_argument("--fd-reduce-below", type=int, default=4096)
    result.add_argument("--fd-block-below", type=int, default=1024)
    result.add_argument("--green-network-workers", type=int, default=4)
    result.add_argument("--green-browser-workers", type=int, default=1)
    result.add_argument("--reduced-network-workers", type=int, default=2)
    result.add_argument("--reduced-browser-workers", type=int, default=0)
    return result


def thresholds_from_args(args: argparse.Namespace) -> Thresholds:
    return Thresholds(
        filesystem_block_bytes=_gib(args.filesystem_block_gib),
        filesystem_reduce_bytes=_gib(args.filesystem_reduce_gib),
        filesystem_block_percent=args.filesystem_block_percent,
        filesystem_reduce_percent=args.filesystem_reduce_percent,
        memory_block_bytes=_gib(args.memory_block_gib),
        memory_reduce_bytes=_gib(args.memory_reduce_gib),
        memory_block_percent=args.memory_block_percent,
        memory_reduce_percent=args.memory_reduce_percent,
        load_reduce_per_cpu=args.load_reduce_per_cpu,
        load_block_per_cpu=args.load_block_per_cpu,
        swap_reduce_percent=args.swap_reduce_percent,
        swap_block_percent=args.swap_block_percent,
        processes_reduce_above=args.processes_reduce_above,
        processes_block_above=args.processes_block_above,
        max_log_reduce_bytes=_gib(args.max_log_reduce_gib),
        max_log_block_bytes=_gib(args.max_log_block_gib),
        roots_reduce_bytes=_gib(args.roots_reduce_gib),
        roots_block_bytes=_gib(args.roots_block_gib),
        fd_reduce_below=args.fd_reduce_below,
        fd_block_below=args.fd_block_below,
        green_network_workers=args.green_network_workers,
        green_browser_workers=args.green_browser_workers,
        reduced_network_workers=args.reduced_network_workers,
        reduced_browser_workers=args.reduced_browser_workers,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    automation_root = Path(__file__).resolve().parents[1]
    workspace_root = automation_root.parent
    log_roots = args.log_root if args.log_root is not None else [automation_root / "var"]
    output_roots = args.output_root if args.output_root is not None else [workspace_root / "outputs"]
    limits = thresholds_from_args(args)
    try:
        limits.validate()
    except ValueError as exc:
        parser().error(str(exc))
    metrics = collect_metrics(
        args.filesystem_path,
        log_roots,
        output_roots,
        args.scrape_pattern or DEFAULT_SCRAPE_PATTERNS,
        args.browser_pattern or DEFAULT_BROWSER_PATTERNS,
    )
    report = evaluate_capacity(metrics, limits, include_paths=args.include_paths)
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if args.always_zero else EXIT_CODES[str(report["status"])]


if __name__ == "__main__":
    raise SystemExit(main())
