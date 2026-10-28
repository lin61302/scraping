#!/usr/bin/env python3
"""Build the public-safe data snapshot used by the static tracker Space.

The source workbook JSON remains local.  Only an explicit allowlist of fields is
exported, and every narrative field is scrubbed before it can reach the Space.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any


SNAPSHOT_VERSION = 4
DEFAULT_SOURCE = Path(__file__).resolve().parents[2] / "source_tracker" / "master_source_tracker_rows.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "tracker_snapshot.json"
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_COUNT_ROOTS = Path(__file__).with_name("count_roots.private.json")


def configured_count_root(environment_key: str, config_key: str) -> Path | None:
    """Resolve a private aggregate root without embedding it in publishable code."""

    value = os.environ.get(environment_key, "").strip()
    if not value and PRIVATE_COUNT_ROOTS.is_file():
        try:
            private_config = json.loads(PRIVATE_COUNT_ROOTS.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            private_config = {}
        if isinstance(private_config, dict):
            value = str(private_config.get(config_key) or "").strip()
    return Path(value).expanduser() if value else None


DEFAULT_CIVIC_COUNT_ROOT = configured_count_root(
    "SOURCE_TRACKER_CIVIC_COUNT_ROOT", "civic_count_root"
)
DEFAULT_ENV_COUNT_ROOT = configured_count_root(
    "SOURCE_TRACKER_ENV_COUNT_ROOT", "env_count_root"
)
DEFAULT_SCRAPE_METRICS_ROOT = WORKSPACE_ROOT / "automation" / "reports"
COUNT_SERIES_MONTHS = 18
COUNT_SNAPSHOT_DIR = re.compile(r"^(\d{4})_(\d{1,2})_(\d{1,2})$")
COUNT_SERIES_KEYS = {
    "scrape_as_of",
    "tcg_as_of",
    "scrape_basis",
    "tcg_basis",
    "through",
    "points",
}

ALLOWED_ROW_KEYS = {
    "country",
    "source",
    "project",
    "cycle_wave",
    "required_from",
    "required_through",
    "backlog_months",
    "baseline_status",
    "scraping_progress",
    "coverage_through",
    "last_checked",
    "method",
    "problem_summary",
    "needs_revisit",
    "next_action",
    "languages",
    "stage",
    "patchtools_status",
    "pretcg_status",
    "downstream_ready",
    "scheduled_wave",
    "coverage_basis",
    "scrape_verified_through",
    "scrape_record_through",
    "scrape_record_state",
    "scrape_record_basis",
    "boundary_check",
    "tcg_record_through",
    "tcg_record_state",
    "tcg_record_basis",
    "current_evidence_addressed",
    "pipeline_state",
    "attention_state",
    "recorded_notion_boundary",
    "first_pass_target",
    "first_pass_verified_through",
    "current_month_partial_through",
    "first_pass_state",
    "first_pass_note",
}

FIRST_PASS_ROW_KEYS = {
    "recorded_notion_boundary",
    "first_pass_target",
    "first_pass_verified_through",
    "current_month_partial_through",
    "first_pass_state",
    "first_pass_note",
}
REQUIRED_ROW_KEYS = ALLOWED_ROW_KEYS - FIRST_PASS_ROW_KEYS
FIRST_PASS_STATES = {
    "Complete through target",
    "In progress",
    "Blocked",
    "Verified through earlier boundary",
    "Inspected; verification pending",
    "Not inspected",
}

ALLOWED_COUNTRY_KEYS = {
    "country",
    "cycle_wave",
    "sources",
    "required_from",
    "safe_through",
    "coverage_recorded",
    "receipt_grade_sources",
    "legacy_record_sources",
    "later_coverage_required",
    "baseline_done",
    "scrape_open",
    "scrape_unassessed",
    "open_or_unassessed",
    "later_coverage_from",
    "later_coverage_through",
    "later_coverage_sources",
    "later_coverage_pct",
    "cycle_coverage_from",
    "cycle_coverage_through",
    "cycle_coverage_sources",
    "cycle_coverage_total",
    "cycle_coverage_pct",
    "cycle_coverage_state",
    "recorded_scrape_floor",
    "recorded_scrape_sources",
    "recorded_scrape_majority_through",
    "recorded_scrape_majority_sources",
    "recorded_scrape_majority_pct",
    "scrape_record_complete",
    "scrape_record_active",
    "scrape_record_queued",
    "scrape_record_open",
    "scrape_record_unassessed",
    "recorded_tcg_floor",
    "recorded_tcg_sources",
    "recorded_tcg_majority_through",
    "recorded_tcg_majority_sources",
    "recorded_tcg_majority_pct",
    "patchtools_complete",
    "patchtools_active",
    "patchtools_waiting",
    "patchtools_blocked",
    "patchtools_unverified",
    "patchtools_unassessed",
    "pretcg_complete",
    "pretcg_partial",
    "pretcg_active",
    "pretcg_waiting",
    "pretcg_blocked",
    "pretcg_unverified",
    "pretcg_unassessed",
    "downstream_ready",
    "active",
    "blocked",
    "review",
    "progress",
    "completion_pct",
    "first_pass_target",
    "first_pass_inspected",
    "first_pass_complete",
    "first_pass_current_month_partial",
    "first_pass_completion_pct",
    "first_pass_progress",
}

FIRST_PASS_COUNTRY_KEYS = {
    "first_pass_target",
    "first_pass_inspected",
    "first_pass_complete",
    "first_pass_current_month_partial",
    "first_pass_completion_pct",
    "first_pass_progress",
}
REQUIRED_COUNTRY_KEYS = ALLOWED_COUNTRY_KEYS - FIRST_PASS_COUNTRY_KEYS

DROP_SOURCE_KEYS = {
    "Plot path",
    "Adapter",
    "Latest run",
    "Last unattended run",
    "Source ID",
    "Note",
}

REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_ -]?key|access[_ -]?token|bearer)\s*[:=]\s*\S+"), "[sensitive detail removed]"),
    (re.compile(r"(?i)\b(?:ssh|sftp)://\S+"), "[remote address removed]"),
    (re.compile(r"(?i)https?://\S+"), "[link removed]"),
    (re.compile(r"(?<![\w.])/(?:Users|home|var|tmp|opt|srv|mnt|Volumes)/[^\s,;]+"), "[local path removed]"),
    (re.compile(r"(?i)\b[A-Z]:\\[^\s,;]+"), "[local path removed]"),
    (re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[email removed]"),
    (re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"), "[network address removed]"),
    (re.compile(r"(?i)\b(?:pid|process id)\s*[:=#-]?\s*\d+\b"), "[process reference removed]"),
    (re.compile(r"(?i)\bmlp\d*@[\w.-]+\b"), "[remote account removed]"),
    (re.compile(r"(?i)\bmachine\s*17\b"), "remote worker"),
    (re.compile(r"(?i)\bnotion(?:\.so|\.site|\.com)/\S+"), "[workspace link removed]"),
    (re.compile(r"(?i)\b[0-9a-f]{32,}\b"), "[internal id removed]"),
)

FORBIDDEN_OUTPUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w.])/(?:Users|home|var|tmp|opt|srv|mnt|Volumes)/"),
    re.compile(r"(?i)\b[A-Z]:\\"),
    re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|access[_ -]?token|bearer)\s*[:=]"),
    re.compile(r"(?i)\b(?:ssh|sftp)://"),
    re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?i)\b(?:pid|process id)\s*[:=#-]?\s*\d+\b"),
    re.compile(r"(?i)\bmlp\d*@"),
    re.compile(r"(?i)\bmachine\s*\d+\b"),
    re.compile(r"(?i)(?:^|[\s\"'])\b(?:automation|outputs|reports|scraping_automation)/"),
    re.compile(r"(?i)\b(?:observe|run|receipt|wrapper|manifest)[-_][a-z0-9_.-]*\d{6,}\b"),
    re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"(?i)\b[0-9a-f]{32,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)


def clean_text(value: Any, *, limit: int = 420) -> str:
    """Return a short, single-line narrative with sensitive details removed."""
    text = "" if value is None else str(value)
    text = re.sub(r"[\r\n\t]+", " ", text)
    for pattern, replacement in REPLACEMENTS:
        text = pattern.sub(replacement, text)
    # A filename-like token often identifies an internal receipt or script.  The
    # dashboard only needs the operational meaning, not that implementation detail.
    text = re.sub(r"(?i)(?<![\w.-])[^\s,;]+\.(?:py|jsonl?|log|xlsx?|csv|md)(?![\w.-])", "[internal file removed]", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;.-")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def clean_scalar(value: Any, *, limit: int = 100) -> str:
    return clean_text(value, limit=limit)


def normalize_method(value: Any) -> str:
    text = str(value or "").lower()
    if any(token in text for token in ("selenium", "browser", "playwright", "chrome")):
        return "Browser"
    if "sitemap" in text:
        return "Sitemap"
    if any(token in text for token in ("rss", "feed")):
        return "Feed"
    if any(token in text for token in ("api", "rest", "json endpoint")):
        return "API"
    if any(token in text for token in ("archive", "search", "category", "listing")):
        return "Archive / listing"
    if any(token in text for token in ("request", "html", "http", "article")):
        return "HTML"
    if not text.strip():
        return "Unspecified"
    return "Custom"


def pipeline_state(row: dict[str, Any]) -> str:
    baseline = str(row.get("Baseline status", "")).lower()
    patch = patchtools_bucket(row.get("Patchtools status"))
    pretcg = pretcg_bucket(row.get("Pre-TCG status"))
    ready = str(row.get("Downstream ready", "")).lower()
    legacy = normalized_stage(row.get("Legacy record stage") or row.get("Notion stage"))
    if ready == "yes":
        return "Downstream ready"
    if pretcg == "complete":
        return "Pre-TCG complete"
    if pretcg == "partial":
        return "Pre-TCG partial"
    if pretcg in {"active", "blocked"}:
        return "Pre-TCG"
    if patch == "complete":
        return "Patchtools complete"
    if patch in {"active", "waiting", "blocked"}:
        return "Patchtools / promotion"
    if patch == "unassessed" and pretcg == "unassessed":
        legacy_states = {
            "counts_generated": "Recorded TCG/count complete",
            "complete": "Recorded Patchtools complete",
            "postprocessing": "Recorded TCG in progress",
            "scraped": "Recorded scraping complete",
            "running": "Scraping active",
            "queued": "Scraping queued",
        }
        if legacy in legacy_states:
            return legacy_states[legacy]
    if baseline == "done":
        return "Scraping complete"
    if baseline == "open":
        return "Scraping open"
    return "Unassessed"


def attention_state(row: dict[str, Any]) -> str:
    combined = " ".join(
        str(row.get(key, ""))
        for key in (
            "Baseline status",
            "Scraping progress",
            "Needs revisit",
            "Patchtools status",
            "Pre-TCG status",
            "Next action",
        )
    ).lower()
    if str(row.get("Downstream ready", "")).lower() == "yes":
        return "Ready"
    legacy = normalized_stage(row.get("Legacy record stage") or row.get("Notion stage"))
    patch = patchtools_bucket(row.get("Patchtools status"))
    pretcg = pretcg_bucket(row.get("Pre-TCG status"))
    if patch == "unassessed" and pretcg == "unassessed":
        if legacy in {"postprocessing", "running"}:
            return "Active"
        if legacy == "queued":
            return "Backlog"
        if legacy in {"counts_generated", "complete", "scraped"}:
            return "On track"
    if any(token in combined for token in ("running", "active", "queued")):
        return "Active"
    if any(token in combined for token in ("blocked", "permission", "licensed feed", "suspended", "external gap")):
        return "Blocked"
    if str(row.get("Baseline status", "")).lower() == "unassessed":
        return "Backlog"
    if str(row.get("Needs revisit", "")).lower() in {"no", "", "unassessed"}:
        return "On track"
    return "Review"


def safe_source(value: Any) -> str:
    source = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]+", source):
        raise ValueError(f"Unsafe or malformed source label: {source!r}")
    return source


def count_series_key(project: Any, source: Any) -> str:
    """Return the stable public key used to join a tracker row to its chart."""

    project_name = str(project or "").strip().upper()
    if project_name not in {"MLP", "MLEED"}:
        raise ValueError(f"Unsupported project for count series: {project_name!r}")
    return f"{project_name}|{safe_source(source)}"


def _snapshot_date(path: Path, root: Path) -> date | None:
    """Read a count snapshot date from a parent YYYY_M_D directory."""

    for parent in path.parents:
        if parent == root:
            break
        match = COUNT_SNAPSHOT_DIR.fullmatch(parent.name)
        if not match:
            continue
        try:
            return date(*(int(value) for value in match.groups()))
        except ValueError:
            return None
    return None


def _month_before(value: date) -> str:
    first = value.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def _month_value(value: Any) -> str:
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})(?:-\d{2})?$", text)
    if not match:
        return ""
    try:
        date(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return ""
    return f"{match.group(1)}-{match.group(2)}"


def _nonnegative_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _read_downstream_count_csv(
    path: Path, *, snapshot_date: date
) -> tuple[dict[str, int], dict[str, int]] | None:
    """Return complete-month scrape-input and post-TCG counts from one CSV.

    Only monthly aggregates are retained.  Article identities, URLs, labels,
    paths, and other raw fields never enter the public snapshot.
    """

    cutoff = _month_before(snapshot_date)
    scraped: dict[str, int] = {}
    tcg_done: dict[str, int] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            if "total_articles" not in fields:
                return None
            has_scrape_input = "total_local_docs" in fields
            for row in reader:
                month = _month_value(row.get("date"))
                if not month or month > cutoff:
                    continue
                tcg_value = _nonnegative_int(row.get("total_articles"))
                scrape_value = (
                    _nonnegative_int(row.get("total_local_docs"))
                    if has_scrape_input
                    else None
                )
                if tcg_value is not None:
                    tcg_done[month] = tcg_value
                if scrape_value is not None:
                    scraped[month] = scrape_value
    except (OSError, csv.Error, UnicodeError):
        return None
    if not tcg_done and not scraped:
        return None
    return scraped, tcg_done


def _latest_downstream_counts(
    raw_rows: list[dict[str, Any]],
    *,
    civic_root: Path | None,
    env_root: Path | None,
) -> dict[str, dict[str, Any]]:
    """Select one latest usable downstream count snapshot per source."""

    project_roots = {"MLP": civic_root, "MLEED": env_root}
    wanted = {
        project: {
            f"{safe_source(row.get('Source'))}.csv"
            for row in raw_rows
            if str(row.get("Project") or "").strip().upper() == project
        }
        for project in project_roots
    }
    candidates: dict[str, list[tuple[date, Path]]] = defaultdict(list)
    for project, root in project_roots.items():
        if root is None or not root.is_dir():
            continue
        project_wanted = wanted[project]
        for path in root.rglob("*.csv"):
            filename = path.name.lower()
            if filename not in project_wanted:
                continue
            captured = _snapshot_date(path, root)
            if captured:
                candidates[f"{project}|{filename[:-4]}"].append((captured, path))

    selected: dict[str, dict[str, Any]] = {}
    for key, artifacts in candidates.items():
        for captured, path in sorted(
            artifacts, key=lambda item: (item[0], str(item[1])), reverse=True
        ):
            counts = _read_downstream_count_csv(path, snapshot_date=captured)
            if counts is None:
                continue
            scraped, tcg_done = counts
            selected[key] = {
                "captured": captured,
                "scraped": scraped,
                "tcg_done": tcg_done,
            }
            break
    return selected


def _source_from_metrics(payload: dict[str, Any]) -> str:
    source = payload.get("source_domain") or payload.get("source")
    if not source:
        source_id = str(payload.get("source_id") or "")
        source = source_id.rsplit(":", 1)[-1] if ":" in source_id else source_id
    try:
        return safe_source(source)
    except ValueError:
        return ""


def _scrape_metric_candidate(
    payload: dict[str, Any], *, fallback_captured: date | None = None
) -> tuple[date, str, dict[str, int]] | None:
    """Read one saved, already aggregated scrape-count artifact."""

    captured = iso_date(str(payload.get("captured_at_utc") or "")[:10])
    captured = captured or iso_date(str(payload.get("as_of") or "")[:10])
    captured = captured or fallback_captured
    if captured is None:
        return None
    cutoff = _month_before(captured)

    daily = payload.get("daily")
    if isinstance(daily, list):
        grouped: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for item in daily:
            if not isinstance(item, dict):
                continue
            source = _source_from_metrics(item)
            month = _month_value(item.get("date"))
            rows = _nonnegative_int(item.get("rows"))
            if source and month and month <= cutoff and rows is not None:
                grouped[source][month] += rows
        # The caller handles multi-source artifacts separately.
        if grouped:
            return captured, "", dict(grouped)

    source = _source_from_metrics(payload)
    monthly = payload.get("monthly")
    if source and isinstance(monthly, list):
        counts: dict[str, int] = {}
        for item in monthly:
            if not isinstance(item, dict):
                continue
            month = _month_value(item.get("month") or item.get("date"))
            rows = _nonnegative_int(item.get("rows"))
            if month and month <= cutoff and rows is not None:
                counts[month] = rows
        if counts:
            return captured, source, counts
    return None


def _latest_saved_scrape_counts(metrics_root: Path) -> dict[str, dict[str, Any]]:
    """Collect the latest saved production scrape series without DB access."""

    if not metrics_root.is_dir():
        return {}
    selected: dict[str, dict[str, Any]] = {}
    for path in metrics_root.rglob("*metrics*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        candidate = _scrape_metric_candidate(payload)
        if candidate is None:
            continue
        captured, source, counts = candidate
        candidates = counts.items() if source == "" else [(source, counts)]
        for source_name, monthly in candidates:
            if not isinstance(monthly, dict):
                continue
            current = selected.get(source_name)
            if current is None or captured > current["captured"]:
                selected[source_name] = {
                    "captured": captured,
                    "scraped": dict(monthly),
                }
    return selected


def build_source_count_series(
    raw_rows: list[dict[str, Any]],
    *,
    civic_root: Path | None = DEFAULT_CIVIC_COUNT_ROOT,
    env_root: Path | None = DEFAULT_ENV_COUNT_ROOT,
    scrape_metrics_root: Path = DEFAULT_SCRAPE_METRICS_ROOT,
    months: int = COUNT_SERIES_MONTHS,
) -> dict[str, dict[str, Any]]:
    """Build privacy-safe monthly scrape and post-TCG series.

    The downstream CSVs are already source/month aggregates.  Saved production
    metrics add scrape counts where the downstream export lacks input volume.
    The published result contains only month labels and nonnegative counts.
    """

    if months < 2 or months > 60:
        raise ValueError("Count-series month window must be between 2 and 60")
    downstream = _latest_downstream_counts(
        raw_rows, civic_root=civic_root, env_root=env_root
    )
    saved_scrapes = _latest_saved_scrape_counts(scrape_metrics_root)
    unique_rows = {
        count_series_key(row.get("Project"), row.get("Source")): row
        for row in raw_rows
    }
    projects_by_source: dict[str, set[str]] = defaultdict(set)
    for row in raw_rows:
        source = safe_source(row.get("Source"))
        project = str(row.get("Project") or "").strip().upper()
        projects_by_source[source].add(project)
    result: dict[str, dict[str, Any]] = {}
    for key, row in sorted(unique_rows.items()):
        source = safe_source(row.get("Source"))
        downstream_item = downstream.get(key, {})
        tcg_counts = dict(downstream_item.get("tcg_done") or {})
        downstream_scrapes = dict(downstream_item.get("scraped") or {})
        # Older aggregate metrics identify the source but not always its
        # project.  Do not copy such a series across projects when one domain is
        # tracked in both MLP and MLEED.
        saved_item = (
            saved_scrapes.get(source, {})
            if len(projects_by_source[source]) == 1
            else {}
        )
        saved_counts = dict(saved_item.get("scraped") or {})

        # Prefer the downstream source-input volume when present because it is
        # captured alongside the TCG count.  Use saved scrape metrics otherwise.
        scrape_counts = downstream_scrapes or saved_counts
        scrape_as_of = downstream_item.get("captured") if downstream_scrapes else saved_item.get("captured")
        tcg_as_of = downstream_item.get("captured") if tcg_counts else None
        months_present = sorted(set(scrape_counts) | set(tcg_counts))
        if not months_present:
            continue
        selected_months = months_present[-months:]
        points = [
            [month, scrape_counts.get(month), tcg_counts.get(month)]
            for month in selected_months
        ]
        result[key] = {
            "scrape_as_of": scrape_as_of.isoformat() if scrape_as_of else "",
            "tcg_as_of": tcg_as_of.isoformat() if tcg_as_of else "",
            "scrape_basis": (
                "Downstream source input snapshot"
                if downstream_scrapes
                else "Saved scrape metrics"
                if saved_counts
                else "Not available"
            ),
            "tcg_basis": (
                "Downstream filtered count snapshot" if tcg_counts else "Not available"
            ),
            "through": selected_months[-1],
            "points": points,
        }
    return result


def latest_complete_month_target(
    payload: dict[str, Any], *, generated_on: date | None
) -> date | None:
    """Return the configured first-pass target or the last complete month.

    ``first_pass_target_end`` freezes a campaign across later refreshes.  The
    calculated fallback keeps older master payloads exportable during the
    additive schema migration.
    """

    configured = iso_date(payload.get("first_pass_target_end"))
    if configured:
        return configured
    if generated_on:
        return generated_on.replace(day=1) - timedelta(days=1)
    return iso_date(payload.get("baseline_end"))


def first_pass_state(
    row: dict[str, Any], *, verified: date | None, target: date | None
) -> str:
    explicit = clean_scalar(row.get("First-pass state"), limit=50)
    if explicit in FIRST_PASS_STATES:
        return explicit
    if verified and target and verified >= target:
        return "Complete through target"
    combined = " ".join(
        str(row.get(key) or "")
        for key in (
            "Scraping progress",
            "Patchtools status",
            "Pre-TCG status",
            "Problems identified",
            "Needs revisit",
            "Next action",
        )
    ).lower()
    if any(token in combined for token in ("running", "queued", "in progress", "active")):
        return "In progress"
    if any(
        token in combined
        for token in (
            "blocked",
            "permission",
            "licensed feed",
            "suspended",
            "publisher 403",
            "cloudflare",
        )
    ):
        return "Blocked"
    if verified:
        return "Verified through earlier boundary"
    addressed = str(row.get("Current evidence addressed") or "").lower() == "yes"
    addressed = addressed or bool(str(row.get("Last checked") or "").strip())
    addressed = addressed or str(row.get("Baseline status") or "").lower() in {
        "done",
        "open",
    }
    progress = str(row.get("Scraping progress") or "").strip().lower()
    addressed = addressed or bool(
        progress and progress not in {"inventory only", "not assessed", "unassessed"}
    )
    return "Inspected; verification pending" if addressed else "Not inspected"


def export_row(
    row: dict[str, Any],
    *,
    as_of: date | None = None,
    first_pass_target: date | None = None,
) -> dict[str, Any]:
    future_legacy = has_future_legacy_record(row, as_of=as_of)
    record_through = output_record_date(row, as_of=as_of)
    record_state = clean_scalar(row.get("Scrape record state"), limit=20) or fallback_scrape_record_state(row)
    record_basis = clean_scalar(row.get("Operational basis") or row.get("Scrape record basis"), limit=50)
    if not record_basis:
        record_basis = "Verified current-cycle evidence" if iso_date(row.get("Coverage through")) else "Legacy Notion tracker date" if record_through else "Missing"
    problem_summary = clean_text(row.get("Problems identified"), limit=420)
    needs_revisit = clean_scalar(row.get("Needs revisit"), limit=80)
    next_action = clean_text(row.get("Next action"), limit=420)
    row_attention = attention_state(row)
    if future_legacy:
        review_note = "A future-dated legacy update record was held out of the operational boundary."
        problem_summary = clean_text(f"{problem_summary}; {review_note}" if problem_summary else review_note, limit=420)
        needs_revisit = "Recorded date review"
        review_action = "Confirm the dated source note before restoring that boundary."
        next_action = clean_text(f"{next_action}; {review_action}" if next_action else review_action, limit=420)
        record_state = "Review"
        receipt_date = clean_scalar(row.get("Coverage through"), limit=12)
        if record_through and record_through == receipt_date:
            record_basis = "Receipt boundary; future legacy date held for review"
        elif record_through:
            record_basis = "Prior cycle boundary; future legacy date held for review"
        else:
            record_basis = "Future legacy date held for review"
        row_attention = "Review"
    tcg_state = clean_scalar(row.get("TCG record state"), limit=50) or fallback_tcg_record_state(row)
    tcg_through = clean_scalar(row.get("TCG record through"), limit=12)
    if iso_date(tcg_through) is None:
        tcg_through = ""
    target = iso_date(row.get("First-pass target")) or first_pass_target
    verified = iso_date(row.get("First-pass verified through"))
    verified = verified or iso_date(row.get("Strict verified through"))
    verified = verified or iso_date(row.get("Coverage through"))
    notion_boundary = iso_date(row.get("Recorded Notion boundary"))
    notion_boundary = notion_boundary or iso_date(row.get("Required from"))
    explicit_partial = iso_date(row.get("Current-month partial through"))
    current_partial = None
    if (
        explicit_partial
        and target
        and verified
        and target < explicit_partial <= verified
    ):
        current_partial = explicit_partial
    elif verified and target and verified > target:
        current_partial = verified
    pass_state = first_pass_state(row, verified=verified, target=target)
    pass_note = clean_text(row.get("First-pass note"), limit=420)
    if not pass_note:
        method_label = normalize_method(row.get("Method"))
        if pass_state == "Not inspected":
            pass_note = f"Method: {method_label}. First-pass inspection pending."
        elif problem_summary:
            pass_note = clean_text(
                f"Method: {method_label}. Issue: {problem_summary}", limit=420
            )
        else:
            pass_note = f"Method: {method_label}. No issue note recorded."
    return {
        "country": clean_scalar(row.get("Country"), limit=12),
        # Publisher domains are the tracker entity, so they are intentionally
        # retained. Infrastructure hosts and addresses are stripped elsewhere.
        "source": safe_source(row.get("Source")),
        "project": clean_scalar(row.get("Project"), limit=20),
        "cycle_wave": clean_scalar(row.get("Cycle wave"), limit=40),
        "required_from": clean_scalar(row.get("Required from"), limit=12),
        "required_through": clean_scalar(row.get("Required through"), limit=12),
        "backlog_months": int(row.get("Backlog months") or 0),
        "baseline_status": clean_scalar(row.get("Baseline status"), limit=40),
        "scraping_progress": clean_text(row.get("Scraping progress"), limit=300),
        "coverage_through": clean_scalar(row.get("Coverage through"), limit=12),
        "last_checked": clean_scalar(row.get("Last checked"), limit=32),
        "method": normalize_method(row.get("Method")),
        "problem_summary": problem_summary,
        "needs_revisit": needs_revisit,
        "next_action": next_action,
        "languages": clean_scalar(row.get("Languages"), limit=80),
        "stage": clean_scalar(row.get("Notion stage"), limit=80),
        "patchtools_status": clean_scalar(row.get("Patchtools status"), limit=120),
        "pretcg_status": clean_scalar(row.get("Pre-TCG status"), limit=140),
        "downstream_ready": clean_scalar(row.get("Downstream ready"), limit=12),
        "scheduled_wave": clean_scalar(row.get("Next scheduled run"), limit=50),
        "coverage_basis": clean_text(row.get("Coverage basis"), limit=360),
        "scrape_verified_through": clean_scalar(row.get("Scrape verified through") or row.get("Coverage through"), limit=12),
        "scrape_record_through": record_through,
        "scrape_record_state": record_state,
        "scrape_record_basis": record_basis,
        "boundary_check": clean_scalar(row.get("Boundary-month check"), limit=120),
        "tcg_record_through": tcg_through,
        "tcg_record_state": tcg_state,
        "tcg_record_basis": clean_scalar(row.get("TCG record basis"), limit=50),
        "current_evidence_addressed": clean_scalar(row.get("Current evidence addressed"), limit=12),
        "pipeline_state": pipeline_state(row),
        "attention_state": row_attention,
        "recorded_notion_boundary": (
            notion_boundary.isoformat() if notion_boundary else ""
        ),
        "first_pass_target": target.isoformat() if target else "",
        "first_pass_verified_through": verified.isoformat() if verified else "",
        "current_month_partial_through": (
            current_partial.isoformat() if current_partial else ""
        ),
        "first_pass_state": pass_state,
        "first_pass_note": pass_note,
    }


def iso_min(values: list[str]) -> str:
    valid = [value for value in values if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or "")]
    return min(valid) if valid else ""


def iso_date(value: Any) -> date | None:
    """Parse a tracker date only when it is a real ISO calendar date."""
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def has_future_legacy_record(row: dict[str, Any], *, as_of: date | None) -> bool:
    """Return true only for a future dated non-receipt scrape record."""
    if "future legacy date excluded" in str(row.get("Boundary-month check") or "").lower():
        return True
    explicit = iso_date(clean_scalar(row.get("Scrape record through"), limit=12))
    receipt = iso_date(clean_scalar(row.get("Coverage through"), limit=12))
    return bool(as_of and explicit and explicit > as_of and explicit != receipt)


def output_record_date(row: dict[str, Any], *, as_of: date | None = None) -> str:
    """Choose the usable operational boundary without rewriting receipt evidence."""
    operational = clean_scalar(row.get("Operational through"), limit=12)
    parsed_operational = iso_date(operational)
    if parsed_operational and (not as_of or parsed_operational <= as_of):
        return operational
    explicit = clean_scalar(row.get("Scrape record through"), limit=12)
    parsed_explicit = iso_date(explicit)
    if parsed_explicit and (not as_of or parsed_explicit <= as_of):
        return explicit
    coverage = clean_scalar(row.get("Coverage through"), limit=12)
    if iso_date(coverage):
        return coverage
    prior = clean_scalar(row.get("Required from"), limit=12)
    parsed_prior = iso_date(prior)
    return prior if parsed_prior and (not as_of or parsed_prior <= as_of) else ""


def normalized_stage(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def fallback_scrape_record_state(row: dict[str, Any]) -> str:
    stage = normalized_stage(row.get("Legacy record stage") or row.get("Notion stage"))
    if str(row.get("Baseline status", "")).lower() == "done" or stage in {
        "counts_generated", "complete", "postprocessing", "scraped"
    }:
        return "Complete"
    if stage == "running":
        return "Active"
    if stage == "queued":
        return "Queued"
    if str(row.get("Baseline status", "")).lower() == "open":
        return "Open"
    return "Unassessed"


def fallback_tcg_record_state(row: dict[str, Any]) -> str:
    stage = normalized_stage(row.get("Legacy record stage") or row.get("Notion stage"))
    if stage == "counts_generated":
        return "Complete"
    if stage == "postprocessing":
        return "In progress"
    if stage in {"complete", "scraped", "running", "queued"}:
        return "Pending"
    return "Not assessed"


def recorded_rollup(rows: list[dict[str, Any]], *, date_key: str) -> dict[str, Any]:
    """Summarize operational history separately from strict receipt coverage."""
    parsed = [iso_date(row.get(date_key)) for row in rows]
    recorded = [value for value in parsed if value is not None]
    total = len(rows)
    strict = min(recorded) if total and len(recorded) == total else None

    broad_frontier: date | None = None
    broad_count = 0
    broad_needed = math.ceil(0.8 * total)
    for candidate in sorted(set(recorded), reverse=True):
        covered = sum(value >= candidate for value in recorded)
        if covered >= broad_needed:
            broad_frontier = candidate
            broad_count = covered
            break

    return {
        "floor": strict.isoformat() if strict else "Open",
        "recorded": len(recorded),
        "required": broad_needed,
        "majority_through": broad_frontier.isoformat() if broad_frontier else "",
        "majority_sources": broad_count,
        "majority_pct": round(100 * broad_count / total, 1) if broad_frontier and total else 0,
    }


def patchtools_bucket(value: Any) -> str:
    status = str(value or "").strip().lower()
    if not status or status == "not assessed":
        return "unassessed"
    if "not verified" in status:
        return "unverified"
    if status.startswith("n/a:") and "zero interval" in status:
        return "complete"
    # Promotion completion alone is not Patchtools completion. Active tokens
    if any(token in status for token in ("running", "queued", "pending")):
        return "active"
    if "waiting" in status:
        return "waiting"
    if "blocked" in status:
        return "blocked"
    if status == "complete" or "patchtools complete" in status:
        return "complete"
    return "unverified"


def pretcg_bucket(value: Any) -> str:
    status = str(value or "").strip().lower()
    if not status or status == "not assessed":
        return "unassessed"
    if "not verified" in status:
        return "unverified"
    if status.startswith("n/a:") and "zero interval" in status:
        return "complete"
    if any(token in status for token in ("running", "queued")):
        return "active"
    if "waiting" in status:
        return "waiting"
    if "partial" in status or ("complete" in status and ("blocked" in status or "classifier" in status)):
        return "partial"
    if "blocked" in status:
        return "blocked"
    if "complete" in status:
        return "complete"
    return "unverified"


def build_country_rows(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    countries: list[dict[str, Any]] = []
    for country in sorted({row["country"] for row in source_rows}):
        rows = [row for row in source_rows if row["country"] == country]
        baseline = Counter(row["baseline_status"].strip().lower() for row in rows)
        done = baseline.get("done", 0)
        patch = Counter(patchtools_bucket(row["patchtools_status"]) for row in rows)
        pretcg = Counter(pretcg_bucket(row["pretcg_status"]) for row in rows)
        downstream = sum(row["downstream_ready"].lower() == "yes" for row in rows)
        scrape_records = Counter(row["scrape_record_state"] for row in rows)
        receipt_dates = [iso_date(row.get("coverage_through")) for row in rows]
        valid_receipts = [value for value in receipt_dates if value is not None]
        strict_safe = min(valid_receipts).isoformat() if len(valid_receipts) == len(rows) else "Open"
        recorded_scrape = recorded_rollup(rows, date_key="scrape_record_through")
        recorded_tcg = recorded_rollup(rows, date_key="tcg_record_through")
        cycle_through = iso_min([row["required_through"] for row in rows])
        cycle_date = iso_date(cycle_through)
        cycle_from = cycle_date.replace(day=1).isoformat() if cycle_date else ""
        cycle_covered = done
        cycle_pct = round(100 * cycle_covered / len(rows), 1) if rows else 0
        cycle_needed = math.ceil(0.8 * len(rows))
        cycle_state = (
            "complete" if cycle_covered == len(rows)
            else "mostly covered" if cycle_covered >= cycle_needed
            else "partial" if cycle_covered
            else "not checked this cycle"
        )
        attention = Counter(row["attention_state"] for row in rows)
        first_pass = Counter(
            row.get("first_pass_state") or "Not inspected" for row in rows
        )
        first_pass_inspected = len(rows) - first_pass.get("Not inspected", 0)
        first_pass_complete = first_pass.get("Complete through target", 0)
        first_pass_current_partial = sum(
            bool(iso_date(row.get("current_month_partial_through"))) for row in rows
        )
        first_pass_target = iso_min(
            [str(row.get("first_pass_target") or "") for row in rows]
        )
        if first_pass_complete == len(rows):
            first_pass_progress = "Complete"
        elif first_pass.get("In progress", 0):
            first_pass_progress = "In progress"
        elif first_pass.get("Blocked", 0):
            first_pass_progress = "Blocked"
        elif first_pass_inspected:
            first_pass_progress = "Partially inspected"
        else:
            first_pass_progress = "Not started"
        active = attention.get("Active", 0)
        blocked = attention.get("Blocked", 0)
        review = attention.get("Review", 0)
        if downstream == len(rows):
            progress = "Downstream ready"
        elif active:
            progress = "Active"
        elif blocked:
            progress = "Blocked"
        elif done == len(rows):
            progress = "Scraping complete"
        else:
            progress = "In progress"
        waves = sorted({row["cycle_wave"] for row in rows})
        countries.append(
            {
                "country": country,
                "cycle_wave": ", ".join(waves),
                "sources": len(rows),
                "required_from": iso_min([row["required_from"] for row in rows]),
                "safe_through": strict_safe,
                "coverage_recorded": len(valid_receipts),
                "receipt_grade_sources": len(valid_receipts),
                "legacy_record_sources": sum(
                    bool(iso_date(row.get("scrape_record_through")))
                    and not bool(iso_date(row.get("coverage_through")))
                    for row in rows
                ),
                "later_coverage_required": cycle_needed,
                "later_coverage_from": cycle_from,
                "later_coverage_through": cycle_through,
                "later_coverage_sources": cycle_covered,
                "later_coverage_pct": cycle_pct,
                "cycle_coverage_from": cycle_from,
                "cycle_coverage_through": cycle_through,
                "cycle_coverage_sources": cycle_covered,
                "cycle_coverage_total": len(rows),
                "cycle_coverage_pct": cycle_pct,
                "cycle_coverage_state": cycle_state,
                "recorded_scrape_floor": recorded_scrape["floor"],
                "recorded_scrape_sources": recorded_scrape["recorded"],
                "recorded_scrape_majority_through": recorded_scrape["majority_through"],
                "recorded_scrape_majority_sources": recorded_scrape["majority_sources"],
                "recorded_scrape_majority_pct": recorded_scrape["majority_pct"],
                "scrape_record_complete": scrape_records.get("Complete", 0),
                "scrape_record_active": scrape_records.get("Active", 0),
                "scrape_record_queued": scrape_records.get("Queued", 0),
                "scrape_record_open": scrape_records.get("Open", 0),
                "scrape_record_unassessed": scrape_records.get("Unassessed", 0),
                "recorded_tcg_floor": recorded_tcg["floor"],
                "recorded_tcg_sources": recorded_tcg["recorded"],
                "recorded_tcg_majority_through": recorded_tcg["majority_through"],
                "recorded_tcg_majority_sources": recorded_tcg["majority_sources"],
                "recorded_tcg_majority_pct": recorded_tcg["majority_pct"],
                "baseline_done": done,
                "scrape_open": baseline.get("open", 0),
                "scrape_unassessed": baseline.get("unassessed", 0),
                "open_or_unassessed": len(rows) - done,
                "patchtools_complete": patch.get("complete", 0),
                "patchtools_active": patch.get("active", 0),
                "patchtools_waiting": patch.get("waiting", 0),
                "patchtools_blocked": patch.get("blocked", 0),
                "patchtools_unverified": patch.get("unverified", 0),
                "patchtools_unassessed": patch.get("unassessed", 0),
                "pretcg_complete": pretcg.get("complete", 0),
                "pretcg_partial": pretcg.get("partial", 0),
                "pretcg_active": pretcg.get("active", 0),
                "pretcg_waiting": pretcg.get("waiting", 0),
                "pretcg_blocked": pretcg.get("blocked", 0),
                "pretcg_unverified": pretcg.get("unverified", 0),
                "pretcg_unassessed": pretcg.get("unassessed", 0),
                "downstream_ready": downstream,
                "active": active,
                "blocked": blocked,
                "review": review,
                "progress": progress,
                "completion_pct": round(100 * done / len(rows), 1) if rows else 0,
                "first_pass_target": first_pass_target,
                "first_pass_inspected": first_pass_inspected,
                "first_pass_complete": first_pass_complete,
                "first_pass_current_month_partial": first_pass_current_partial,
                "first_pass_completion_pct": round(
                    100 * first_pass_complete / len(rows), 1
                )
                if rows
                else 0,
                "first_pass_progress": first_pass_progress,
            }
        )
    return countries


def validate_export(snapshot: dict[str, Any]) -> None:
    rows = snapshot.get("sources")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Snapshot must contain at least one source row")
    for index, row in enumerate(rows):
        extra = set(row) - ALLOWED_ROW_KEYS
        missing = REQUIRED_ROW_KEYS - set(row)
        if extra or missing:
            raise ValueError(f"Row {index} schema mismatch; extra={sorted(extra)}, missing={sorted(missing)}")
        first_pass_present = set(row) & FIRST_PASS_ROW_KEYS
        if first_pass_present and first_pass_present != FIRST_PASS_ROW_KEYS:
            raise ValueError(
                f"Row {index} has a partial first-pass schema: "
                f"{sorted(first_pass_present)}"
            )
        if not row["country"] or not row["source"]:
            raise ValueError(f"Row {index} has no country/source")
        if first_pass_present:
            target = iso_date(row["first_pass_target"])
            verified = iso_date(row["first_pass_verified_through"])
            current_partial = iso_date(row["current_month_partial_through"])
            if target is None:
                raise ValueError(f"Row {index} has no valid first-pass target")
            if row["first_pass_state"] not in FIRST_PASS_STATES:
                raise ValueError(f"Row {index} has an invalid first-pass state")
            if (
                row["first_pass_state"] == "Complete through target"
                and (verified is None or verified < target)
            ):
                raise ValueError(
                    f"Row {index} claims first-pass completion without target coverage"
                )
            if current_partial and (
                verified is None
                or current_partial <= target
                or current_partial > verified
            ):
                raise ValueError(
                    f"Row {index} has a current-month date without matching receipt coverage"
                )
    count_series = snapshot.get("count_series", {})
    if not isinstance(count_series, dict):
        raise ValueError("count_series must be an object")
    scrape_series = 0
    tcg_series = 0
    for key, series in count_series.items():
        if not re.fullmatch(r"(?:MLP|MLEED)\|[a-z0-9._-]+", key):
            raise ValueError(f"Unsafe count-series key: {key!r}")
        if not isinstance(series, dict) or set(series) != COUNT_SERIES_KEYS:
            raise ValueError(f"Count series {key!r} has an invalid schema")
        if series["scrape_basis"] not in {
            "Downstream source input snapshot",
            "Saved scrape metrics",
            "Not available",
        }:
            raise ValueError(f"Count series {key!r} has an invalid scrape basis")
        if series["tcg_basis"] not in {
            "Downstream filtered count snapshot",
            "Not available",
        }:
            raise ValueError(f"Count series {key!r} has an invalid TCG basis")
        for date_key in ("scrape_as_of", "tcg_as_of"):
            if series[date_key] and iso_date(series[date_key]) is None:
                raise ValueError(f"Count series {key!r} has an invalid {date_key}")
        points = series["points"]
        if not isinstance(points, list) or not 1 <= len(points) <= 60:
            raise ValueError(f"Count series {key!r} has an invalid point count")
        months: list[str] = []
        has_scrape = False
        has_tcg = False
        for point in points:
            if not isinstance(point, list) or len(point) != 3:
                raise ValueError(f"Count series {key!r} has a malformed point")
            month, scraped, tcg_done = point
            if not re.fullmatch(r"\d{4}-\d{2}", str(month)) or not _month_value(month):
                raise ValueError(f"Count series {key!r} has an invalid month")
            months.append(month)
            for label, value in (("scraped", scraped), ("tcg_done", tcg_done)):
                if value is not None and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                ):
                    raise ValueError(
                        f"Count series {key!r} has an invalid {label} value"
                    )
            has_scrape = has_scrape or scraped is not None
            has_tcg = has_tcg or tcg_done is not None
        if months != sorted(set(months)) or series["through"] != months[-1]:
            raise ValueError(f"Count series {key!r} has unordered months")
        if not has_scrape and not has_tcg:
            raise ValueError(f"Count series {key!r} has no counts")
        scrape_series += int(has_scrape)
        tcg_series += int(has_tcg)
    raw = json.dumps(snapshot, ensure_ascii=False)
    for pattern in FORBIDDEN_OUTPUT_PATTERNS:
        match = pattern.search(raw)
        if match:
            raise ValueError(f"Snapshot still contains forbidden content matching {pattern.pattern!r}: {match.group(0)!r}")
    meta = snapshot.get("meta", {})
    if meta.get("source_count") != len(rows):
        raise ValueError("meta.source_count does not match exported source rows")
    if meta.get("count_series_source_count", 0) != len(count_series):
        raise ValueError("meta.count_series_source_count does not match count_series")
    if meta.get("scrape_count_series_source_count", 0) != scrape_series:
        raise ValueError("meta.scrape_count_series_source_count does not match count_series")
    if meta.get("tcg_count_series_source_count", 0) != tcg_series:
        raise ValueError("meta.tcg_count_series_source_count does not match count_series")
    country_groups = snapshot.get("countries", [])
    if meta.get("country_group_count") != len(country_groups):
        raise ValueError("meta.country_group_count does not match exported country rows")
    for index, row in enumerate(country_groups):
        extra = set(row) - ALLOWED_COUNTRY_KEYS
        missing = REQUIRED_COUNTRY_KEYS - set(row)
        if extra or missing:
            raise ValueError(
                f"Country row {index} schema mismatch; extra={sorted(extra)}, missing={sorted(missing)}"
            )
        first_pass_present = set(row) & FIRST_PASS_COUNTRY_KEYS
        if first_pass_present and first_pass_present != FIRST_PASS_COUNTRY_KEYS:
            raise ValueError(
                f"Country row {index} has a partial first-pass schema"
            )
        if first_pass_present:
            if not 0 <= row["first_pass_complete"] <= row["first_pass_inspected"] <= row["sources"]:
                raise ValueError(f"Country row {index} has invalid first-pass counts")
            expected = round(
                100 * row["first_pass_complete"] / row["sources"], 1
            )
            if row["first_pass_completion_pct"] != expected:
                raise ValueError(
                    f"Country row {index} has an invalid first-pass percentage"
                )
    national_count = sum(item.get("country") not in {"INT", "REG"} for item in country_groups)
    if meta.get("national_country_count") != national_count:
        raise ValueError("meta.national_country_count does not match national country rows")


def build_snapshot(
    payload: dict[str, Any],
    *,
    count_series: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("Input does not contain a rows list")
    generated_on = iso_date(str(payload.get("generated_at_utc") or "")[:10])
    first_pass_target = latest_complete_month_target(
        payload, generated_on=generated_on
    )
    rows = sorted(
        (
            export_row(
                row,
                as_of=generated_on,
                first_pass_target=first_pass_target,
            )
            for row in raw_rows
        ),
        key=lambda row: (row["country"], row["source"]),
    )
    countries = build_country_rows(rows)
    status_counts = Counter(row["baseline_status"] for row in rows)
    pipeline_counts = Counter(row["pipeline_state"] for row in rows)
    attention_counts = Counter(row["attention_state"] for row in rows)
    project_counts = Counter(row["project"] for row in rows)
    method_counts = Counter(row["method"] for row in rows)
    first_pass_counts = Counter(row["first_pass_state"] for row in rows)
    public_count_series = count_series or {}
    scrape_series_count = sum(
        any(point[1] is not None for point in series["points"])
        for series in public_count_series.values()
    )
    tcg_series_count = sum(
        any(point[2] is not None for point in series["points"])
        for series in public_count_series.values()
    )
    snapshot = {
        "meta": {
            "snapshot_version": SNAPSHOT_VERSION,
            "generated_at_utc": clean_scalar(payload.get("generated_at_utc"), limit=32),
            "baseline_end": clean_scalar(payload.get("baseline_end"), limit=12),
            "source_count": len(rows),
            "national_country_count": int(payload.get("country_count") or 0),
            "country_group_count": len(countries),
            "international_regional_source_count": sum(row["country"] in {"INT", "REG"} for row in rows),
            "source_month_units": int(payload.get("source_month_units") or 0),
            "first_pass_target_end": (
                first_pass_target.isoformat() if first_pass_target else ""
            ),
            "current_month_start": (
                (first_pass_target + timedelta(days=1)).isoformat()
                if first_pass_target
                else ""
            ),
            "count_series_source_count": len(public_count_series),
            "scrape_count_series_source_count": scrape_series_count,
            "tcg_count_series_source_count": tcg_series_count,
        },
        "summary": {
            "baseline_status": dict(sorted(status_counts.items())),
            "pipeline_state": dict(sorted(pipeline_counts.items())),
            "attention_state": dict(sorted(attention_counts.items())),
            "project": dict(sorted(project_counts.items())),
            "method": dict(sorted(method_counts.items())),
            "first_pass_state": dict(sorted(first_pass_counts.items())),
        },
        "countries": countries,
        "sources": rows,
        "count_series": public_count_series,
        "waves": [
            {
                "wave": int(item.get("Wave", 0)),
                "countries": clean_text(item.get("Countries"), limit=160),
                "rule": clean_text(item.get("Rule"), limit=180),
            }
            for item in payload.get("waves", [])
            if isinstance(item, dict)
        ],
    }
    validate_export(snapshot)
    return snapshot


def write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--js-output", type=Path, default=None)
    parser.add_argument("--civic-count-root", type=Path, default=DEFAULT_CIVIC_COUNT_ROOT)
    parser.add_argument("--env-count-root", type=Path, default=DEFAULT_ENV_COUNT_ROOT)
    parser.add_argument(
        "--scrape-metrics-root", type=Path, default=DEFAULT_SCRAPE_METRICS_ROOT
    )
    parser.add_argument("--count-series-months", type=int, default=COUNT_SERIES_MONTHS)
    args = parser.parse_args()
    js_output = args.js_output or args.output.with_suffix(".js")

    payload = json.loads(args.source.read_text(encoding="utf-8"))
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("Input does not contain a rows list")
    count_series = build_source_count_series(
        raw_rows,
        civic_root=args.civic_count_root,
        env_root=args.env_count_root,
        scrape_metrics_root=args.scrape_metrics_root,
        months=args.count_series_months,
    )
    snapshot = build_snapshot(payload, count_series=count_series)
    pretty = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    compact = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    json_changed = write_if_changed(args.output, pretty)
    js_changed = write_if_changed(js_output, f"window.TRACKER_DATA={compact};\n")
    state = "updated" if json_changed or js_changed else "unchanged"
    print(
        f"Safe snapshot {state}: {len(snapshot['sources'])} sources, "
        f"{len(snapshot['countries'])} country groups, "
        f"{snapshot['meta']['scrape_count_series_source_count']} scrape series, "
        f"{snapshot['meta']['tcg_count_series_source_count']} TCG series -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
