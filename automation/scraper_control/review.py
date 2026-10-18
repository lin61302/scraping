#!/usr/bin/env python3
"""Build a review packet for one completed observation run.

The report keeps field failures, discovery failures, and volume anomalies separate.
It never changes the observation or writes to MongoDB.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .evidence import compare_required_evidence
from .thesun_five import URLS as THESUN_FIVE_URLS, validate_article_recovery as validate_five_recovery
from .validation import (
    THESUN_REDIRECT_LOOP_POLICY, THESUN_REDIRECT_LOOP_URL,
    THESUN_REMOVED_REDIRECT_URL,
    resolve_policy_exclusion, resolve_with_page_evidence, validate_article,
    validate_thesun_redirect_loop_manifest, validate_thesun_redirect_loop_recovery,
)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def parse_date(value: object) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def date_range(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def robust_expected(values: list[int]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    median = float(statistics.median(values))
    mad = float(statistics.median(abs(value - median) for value in values))
    return median, mad


def issue_codes(check: dict) -> list[str]:
    codes: list[str] = []
    for field in check.get("fields", []):
        if field.get("status") != "pass":
            codes.extend(field.get("codes") or [f"{field.get('field', 'unknown')}_failed"])
    return codes


def load_baseline(path: Optional[Path], source: str, start: date) -> tuple[dict[int, list[int]], list[dict]]:
    by_weekday: dict[int, list[int]] = defaultdict(list)
    rows: list[dict] = []
    if not path:
        return by_weekday, rows
    baseline = json.loads(path.read_text(encoding="utf-8"))
    lower = start - timedelta(days=90)
    for row in baseline.get("daily", []):
        if row.get("source") != source:
            continue
        day = date.fromisoformat(row["date"])
        if lower <= day < start:
            count = int(row.get("unique_urls", row.get("rows", 0)))
            by_weekday[day.weekday()].append(count)
            rows.append({"date": day, "count": count})
    return by_weekday, rows


COMPARISON_LABELS = {
    "historical_weekday": "historical weekday median",
    "historical_all_days": "historical all-day median",
    "within_observation": "within-observation median",
}


def comparison_label(expected: dict[date, dict]) -> str:
    bases = {item["comparison_basis"] for item in expected.values()
             if item["comparison_basis"] in COMPARISON_LABELS}
    if len(bases) == 1:
        return COMPARISON_LABELS[next(iter(bases))]
    return "comparison median (basis varies)"


def draw_daily_pillow(
    png_path: Path, source: str, days: list[date], expected: dict[date, dict],
    volume_anomalies: list[dict], historical_count: int,
    verified_inactive_window: bool = False,
) -> None:
    """Render the same candidate counts when matplotlib is unavailable."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1200, 560
    left, right, top, bottom = 88, 36, 82, 100
    x0, x1, y0, y1 = left, width - right, top, height - bottom
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    def font(size: int):
        try:
            return ImageFont.truetype("DejaVuSans.ttf", size)
        except OSError:
            return ImageFont.load_default()

    regular, small, heading = font(16), font(13), font(23)
    values = [expected[day]["observed"] for day in days]
    active = [expected[day]["weekday_median"] for day in days
              if expected[day]["comparison_basis"] in COMPARISON_LABELS]
    ceiling = max(1, math.ceil(max(max(values, default=0), max(active, default=0)) * 1.12))
    x = lambda index: x0 + (x1 - x0) * index / max(1, len(days) - 1)
    y = lambda value: y1 - (y1 - y0) * value / ceiling

    draw.text((left, 18), f"{source}: candidate daily publication volume", fill="#1b2936", font=heading)
    subtitle = "unique article URLs"
    if verified_inactive_window:
        subtitle += " · publisher window verified inactive"
    draw.text((left, 51), subtitle, fill="#526171", font=small)
    tick_step = max(1, math.ceil(ceiling / 5))
    for value in range(0, ceiling + 1, tick_step):
        yy = y(value)
        draw.line((x0, yy, x1, yy), fill="#e6edf3", width=1)
        draw.text((x0 - 56, yy - 8), str(value), fill="#526171", font=small)
    draw.line((x0, y0, x0, y1, x1, y1), fill="#667788", width=2)
    for index, day in enumerate(days):
        if index % max(1, (len(days) + 9) // 10) == 0 or index == len(days) - 1:
            xx = x(index)
            draw.line((xx, y1, xx, y1 + 5), fill="#667788", width=1)
            draw.text((xx - 19, y1 + 9), day.strftime("%m-%d"), fill="#526171", font=small)

    blue, orange, red = "#1769aa", "#e07a1f", "#c62828"
    for index in range(len(days) - 1):
        draw.line((x(index), y(values[index]), x(index + 1), y(values[index + 1])), fill=blue, width=3)
    for index, value in enumerate(values):
        xx, yy = x(index), y(value)
        draw.ellipse((xx - 5, yy - 5, xx + 5, yy + 5), fill=blue)
    for index in range(len(days) - 1):
        left_item, right_item = expected[days[index]], expected[days[index + 1]]
        if (left_item["comparison_basis"] not in COMPARISON_LABELS
                or right_item["comparison_basis"] not in COMPARISON_LABELS):
            continue
        start_x, start_y = x(index), y(left_item["weekday_median"])
        end_x, end_y = x(index + 1), y(right_item["weekday_median"])
        for step in range(0, 20, 2):
            a, b = step / 20, min(1, (step + 1) / 20)
            draw.line((start_x + (end_x - start_x) * a, start_y + (end_y - start_y) * a,
                       start_x + (end_x - start_x) * b, start_y + (end_y - start_y) * b),
                      fill=orange, width=2)
    anomaly_days = {date.fromisoformat(item["date"]) for item in volume_anomalies}
    for index, day in enumerate(days):
        if day in anomaly_days:
            xx, yy = x(index), y(values[index])
            draw.ellipse((xx - 6, yy - 6, xx + 6, yy + 6), fill=red)

    legend_y = height - 50
    draw.line((left, legend_y + 7, left + 28, legend_y + 7), fill=blue, width=3)
    draw.text((left + 36, legend_y), "eligible candidates", fill="#1b2936", font=regular)
    if active:
        draw.line((left + 280, legend_y + 7, left + 308, legend_y + 7), fill=orange, width=2)
        draw.text((left + 316, legend_y), comparison_label(expected), fill="#1b2936", font=regular)
    if historical_count:
        draw.text((left, height - 24), f"Comparison uses {historical_count} prior daily records where available.",
                  fill="#667788", font=small)
    image.save(png_path, format="PNG")


def analyze(run_dir: Path, baseline_path: Optional[Path], output_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "observed":
        raise ValueError(f"Run is not complete: {manifest.get('status')}")

    start = date.fromisoformat(manifest["requested_start"])
    end = date.fromisoformat(manifest["requested_end"])
    source = manifest["source"]
    selected_rows = read_jsonl(run_dir / "urls.jsonl")
    articles = read_jsonl(run_dir / "articles.jsonl")
    checks = read_jsonl(run_dir / "field_checks.jsonl")
    selected_urls = [row.get("url") for row in selected_rows]
    article_urls = [row.get("url") for row in articles]
    check_urls = [row.get("url") for row in checks]
    artifact_integrity_failures: list[dict] = []
    for artifact, urls in (
        ("urls.jsonl", selected_urls),
        ("articles.jsonl", article_urls),
        ("field_checks.jsonl", check_urls),
    ):
        invalid = [url for url in urls if not isinstance(url, str) or not url]
        url_counts = Counter(url for url in urls if isinstance(url, str) and url)
        duplicates = sorted(url for url, count in url_counts.items() if count > 1)
        if invalid:
            artifact_integrity_failures.append({
                "kind": "artifact_url_missing", "detail": {"artifact": artifact, "rows": len(invalid)},
            })
        if duplicates:
            artifact_integrity_failures.append({
                "kind": "artifact_duplicate_urls", "detail": {"artifact": artifact, "urls": duplicates[:20]},
            })
    selected_set = {url for url in selected_urls if isinstance(url, str) and url}
    article_set = {url for url in article_urls if isinstance(url, str) and url}
    check_set = {url for url in check_urls if isinstance(url, str) and url}
    if article_set != selected_set or check_set != selected_set:
        artifact_integrity_failures.append({
            "kind": "artifact_url_coverage_mismatch",
            "detail": {
                "missing_articles": sorted(selected_set - article_set)[:20],
                "missing_checks": sorted(selected_set - check_set)[:20],
                "extra_articles": sorted(article_set - selected_set)[:20],
                "extra_checks": sorted(check_set - selected_set)[:20],
            },
        })
    identity_mismatch_urls = {
        row.get("url") for row in articles
        if (
            row.get("source_domain") != source
            or row.get("country_code") != manifest.get("country")
        )
    }
    if identity_mismatch_urls:
        artifact_integrity_failures.append({
            "kind": "article_manifest_identity_mismatch",
            "detail": {"urls": sorted(str(url) for url in identity_mismatch_urls)[:20]},
        })
    expected_result_order = selected_urls
    if article_urls != check_urls or check_urls != expected_result_order:
        artifact_integrity_failures.append({
            "kind": "artifact_url_order_mismatch",
            "detail": {
                "selected_result_order": expected_result_order[:20],
                "article_order": article_urls[:20],
                "check_order": check_urls[:20],
            },
        })
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    actual_passed = sum(bool(row.get("eligible")) for row in checks)
    actual_skipped = sum(bool(row.get("excluded")) for row in checks)
    actual_quarantined = len(checks) - actual_passed - actual_skipped
    actual_exceptions = len(selected_rows) - len(checks)
    expected_counts = {
        "discovered": len(selected_rows),
        "selected": len(selected_rows),
        "passed": actual_passed,
        "quarantined": actual_quarantined,
        "skipped": actual_skipped,
        "exceptions": actual_exceptions,
    }
    if (
        actual_quarantined < 0
        or actual_exceptions < 0
        or len(articles) != len(checks)
        or any(counts.get(key) != value for key, value in expected_counts.items())
    ):
        artifact_integrity_failures.append({
            "kind": "manifest_result_accounting_mismatch",
            "detail": {"manifest": counts, "actual": expected_counts},
        })
    try:
        manifest_loop_valid = bool(
            all(isinstance(url, str) for url in selected_urls)
            and validate_thesun_redirect_loop_manifest(manifest, selected_urls)
        )
    except (OSError, ValueError) as exc:
        manifest_loop_valid = False
        artifact_integrity_failures.append({
            "kind": "thesun_audit_report_unavailable_or_changed",
            "detail": {"error": str(exc)},
        })
    errors = read_jsonl(run_dir / "errors.jsonl")
    article_by_url = {item.get("url"): item for item in articles}

    counts_by_day: Counter[date] = Counter()
    all_dates_by_day: Counter[date] = Counter()
    unresolved_by_day: dict[date, list[dict]] = defaultdict(list)
    undated_unresolved: list[dict] = []
    code_counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    spillover = Counter()
    automatic_resolutions: list[dict] = []
    policy_exclusions: list[dict] = []

    for check in checks:
        url = check.get("url")
        article = article_by_url.get(url, {})
        published = parse_date(article.get("date_publish"))
        if published:
            all_dates_by_day[published] += 1
        base_check = validate_article(article, start, end, source)
        page_evidence = check.get("page_evidence") or {}
        semantic_checks = compare_required_evidence(article, page_evidence) if page_evidence else []
        resolved_eligible, resolutions = resolve_with_page_evidence(base_check, semantic_checks, page_evidence)
        loop_recovery_required = url == THESUN_REDIRECT_LOOP_URL
        five_recovery_required = url in THESUN_FIVE_URLS
        removed_recovery_required = url == THESUN_REMOVED_REDIRECT_URL
        policy_skip_evidence_required = article.get("collection") == "SKIP"
        exact_recovery_evidence_required = loop_recovery_required or five_recovery_required or policy_skip_evidence_required
        article_evidence = article.get("field_evidence")
        redirect_recovery_valid = bool(
            manifest_loop_valid
            and (
                not exact_recovery_evidence_required
                or (
                    isinstance(article_evidence, dict)
                    and article_evidence == page_evidence
                    and (
                        not loop_recovery_required
                        or article.get("recovery_policy") == THESUN_REDIRECT_LOOP_POLICY
                    )
                )
            )
            and validate_thesun_redirect_loop_recovery(article, page_evidence)
            and validate_five_recovery(article, page_evidence)
        )
        article_identity_valid = url not in identity_mismatch_urls
        if not article_identity_valid:
            resolved_eligible, resolutions = False, []
            check = {**check, "eligible": False}
        if not redirect_recovery_valid:
            resolved_eligible, resolutions = False, []
            if exact_recovery_evidence_required or article.get("recovery_policy"):
                check = {**check, "eligible": False}
        codes = issue_codes(check)
        # An adapter may reject a page because publisher signals disagree even
        # when the three generic field validators pass. The observer records
        # that parser error as an open issue; keep it visible in the summary.
        if article.get("parser_error") and "adapter_parser_error" not in codes:
            codes.append("adapter_parser_error")
        if not article_identity_valid:
            codes = [*codes, "article_manifest_identity_mismatch"]
        if (exact_recovery_evidence_required or article.get("recovery_policy")) and not redirect_recovery_valid:
            codes = [
                *codes,
                (
                    "removed_redirect_recovery_evidence_invalid"
                    if removed_recovery_required
                    else (
                        "redirect_loop_recovery_evidence_invalid"
                        if loop_recovery_required
                        else (
                            "thesun_five_post_recovery_evidence_invalid"
                            if five_recovery_required
                            else "thesun_policy_recovery_evidence_invalid"
                        )
                    )
                ),
            ]
        evidence_revalidation = []
        if (
            resolved_eligible
            and not check.get("eligible")
            and not resolutions
            and codes
            and set(codes) <= {"date_signal_disagreement"}
        ):
            evidence_revalidation = [{
                "code": "date_evidence_revalidated_day_first",
                "field": "date_publish",
                "original_codes": sorted(set(codes)),
            }]
        effective_resolutions = [*resolutions, *evidence_revalidation]
        if effective_resolutions:
            automatic_resolutions.append({"url": url, "resolutions": effective_resolutions})
        if article.get("collection") == "SKIP":
            exclusion_valid, exclusion_resolution = resolve_policy_exclusion(
                base_check, semantic_checks, page_evidence,
                article.get("exclusion_reason"), article.get("exclusion_evidence"),
                article_url=article.get("url"),
            )
            valid_exclusion = (
                redirect_recovery_valid
                and exclusion_valid
                and check.get("excluded")
                and article.get("exclusion_reason")
                and article.get("exclusion_evidence")
                and check.get("exclusion_reason") == article.get("exclusion_reason")
                and check.get("exclusion_evidence") == article.get("exclusion_evidence")
                and (
                    check.get("exclusion_resolution") == exclusion_resolution
                    or (
                        "exclusion_resolution" not in check
                        and exclusion_resolution == {"mode": "fully_valid_article_policy_exclusion"}
                    )
                )
            )
            if valid_exclusion:
                policy_exclusions.append({
                    "url": url, "reason": article["exclusion_reason"],
                    "evidence": article["exclusion_evidence"],
                })
                continue
        if not article.get("parser_error") and (check.get("eligible") or (resolved_eligible and effective_resolutions)):
            if published and start <= published <= end:
                counts_by_day[published] += 1
            continue

        for code in codes or ["unclassified_failure"]:
            code_counts[code] += 1
            if len(examples[code]) < 10 and url:
                examples[code].append(url)

        # Once page evidence supplies a publication date outside the requested
        # interval, that candidate is discovery spillover. Other field defects
        # on the out-of-window page remain counted for diagnostics, but cannot
        # block the requested interval's safe-through decision.
        if published and not start <= published <= end:
            spillover["before_window" if published < start else "after_window"] += 1
        elif published and start <= published <= end:
            unresolved_by_day[published].append({"url": url, "codes": codes})
        else:
            undated_unresolved.append({"url": url, "codes": codes, "date_publish": article.get("date_publish")})

    historical_by_weekday, historical_rows = load_baseline(baseline_path, source, start)
    expected: dict[date, dict] = {}
    volume_anomalies: list[dict] = []
    requested_days = list(date_range(start, end))
    historical_all = [item["count"] for item in historical_rows]
    observed_days = sorted(counts_by_day)
    observed_span = (
        list(date_range(observed_days[0], observed_days[-1]))
        if observed_days else []
    )
    observed_span_set = set(observed_span)
    within_run_all = [counts_by_day[day] for day in observed_span]
    volume_baseline_sufficient = bool(historical_rows) or len(observed_span) >= 7 or (
        not observed_span and len(requested_days) >= 7
    )
    for day in requested_days:
        weekday_values = historical_by_weekday.get(day.weekday(), [])
        if len(weekday_values) >= 3:
            comparison_values = weekday_values
            comparison_basis = "historical_weekday"
        elif len(historical_all) >= 14:
            comparison_values = historical_all
            comparison_basis = "historical_all_days"
        elif day in observed_span_set and len(observed_span) >= 7:
            comparison_values = within_run_all
            comparison_basis = "within_observation"
        elif observed_span and day not in observed_span_set:
            comparison_values = []
            comparison_basis = "outside_observed_span"
        else:
            comparison_values = []
            comparison_basis = "insufficient_history"
        median, mad = robust_expected(comparison_values)
        observed = counts_by_day[day]
        low_threshold = max(1.0, median * 0.25, median - 4 * max(mad, 1.0))
        high_threshold = max(20.0, median * 4, median + 6 * max(mad, 1.0))
        flag = None
        if median >= 4 and observed < low_threshold:
            flag = "volume_low"
        elif observed > high_threshold:
            flag = "volume_high"
        expected[day] = {
            "observed": observed,
            "weekday_median": round(median, 2),
            "weekday_mad": round(mad, 2),
            "comparison_basis": comparison_basis,
            "flag": flag,
        }
        if flag:
            volume_anomalies.append({"date": day.isoformat(), **expected[day]})

    discovery = manifest.get("discovery_evidence") or {}
    manifest_counts = manifest.get("counts", {})
    verified_inactive_window = bool(
        discovery.get("coverage_complete") is True
        and discovery.get("publication_inactive_for_window") is True
        and manifest_counts.get("discovered") == 0
        and manifest_counts.get("selected") == 0
        and manifest_counts.get("exceptions", 0) == 0
    )
    discovery_failures = []
    discovery_failures.extend(artifact_integrity_failures)
    if not manifest_loop_valid:
        if THESUN_REDIRECT_LOOP_URL in selected_set:
            discovery_failures.append({
                "kind": "redirect_loop_manifest_provenance_invalid",
                "detail": {"url": THESUN_REDIRECT_LOOP_URL},
            })
        if THESUN_REMOVED_REDIRECT_URL in selected_set:
            discovery_failures.append({
                "kind": "removed_redirect_manifest_provenance_invalid",
                "detail": {"url": THESUN_REMOVED_REDIRECT_URL},
            })
        if selected_set & THESUN_FIVE_URLS:
            discovery_failures.append({
                "kind": "thesun_five_post_manifest_provenance_invalid",
                "detail": {"urls": sorted(selected_set & THESUN_FIVE_URLS)},
            })
    if discovery.get("coverage_complete") is False:
        discovery_failures.append({
            "kind": "incomplete_discovery_coverage",
            "detail": {
                "strategy": discovery.get("strategy"),
                "access_status": discovery.get("access_status"),
                "probes": discovery.get("probes") or [],
            },
        })
    if manifest_counts.get("selected") == 0 and not verified_inactive_window:
        discovery_failures.append({
            "kind": "zero_candidate_inactivity_unverified",
            "detail": {"strategy": discovery.get("strategy") or discovery.get("method")},
        })
    if not volume_baseline_sufficient and not verified_inactive_window:
        discovery_failures.append({
            "kind": "insufficient_volume_baseline",
            "detail": {
                "historical_days": len(historical_rows),
                "observation_days": len(observed_span) if observed_span else len(requested_days),
            },
        })
    if manifest_counts.get("selected") != manifest_counts.get("discovered"):
        discovery_failures.append({
            "kind": "sampled_observation",
            "detail": {
                "discovered": manifest_counts.get("discovered"),
                "selected": manifest_counts.get("selected"),
            },
        })
    for key in ("sitemap_failures", "listing_page_failures"):
        for item in discovery.get(key, []) or []:
            discovery_failures.append({"kind": key, "detail": item})
    for item in errors:
        discovery_failures.append({"kind": "runtime_error", "detail": item})
    if manifest_counts.get("exceptions"):
        discovery_failures.append({
            "kind": "incomplete_processing",
            "detail": {
                "exceptions": manifest_counts.get("exceptions", 0),
            },
        })
    if manifest_counts.get("skipped", 0) != len(policy_exclusions):
        discovery_failures.append({
            "kind": "policy_exclusion_accounting_mismatch",
            "detail": {"manifest_skipped": manifest_counts.get("skipped", 0), "verified_exclusions": len(policy_exclusions)},
        })

    blocking_dates = set(unresolved_by_day)
    blocking_dates.update(date.fromisoformat(item["date"]) for item in volume_anomalies)
    safe_through: Optional[date] = None
    if not discovery_failures and not undated_unresolved:
        for day in date_range(start, end):
            if day in blocking_dates:
                break
            safe_through = day

    report = {
        "run_dir": str(run_dir.resolve()),
        "run_id": manifest.get("run_id"),
        "source": source,
        "country": manifest.get("country"),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "status": "review_required" if discovery_failures or undated_unresolved or blocking_dates else "ready_for_dry_run",
        "safe_through_candidate": safe_through.isoformat() if safe_through else None,
        "manifest_counts": manifest_counts,
        "eligible_in_window": sum(counts_by_day.values()),
        "verified_inactive_window": verified_inactive_window,
        "issue_code_counts": dict(code_counts.most_common()),
        "issue_examples": dict(examples),
        "automatic_resolutions": automatic_resolutions,
        "policy_exclusions": policy_exclusions,
        "unresolved_by_date": {key.isoformat(): value for key, value in sorted(unresolved_by_day.items())},
        "undated_or_unknown_window_issues": undated_unresolved,
        "discovery_or_runtime_failures": discovery_failures,
        "discovery_spillover": dict(spillover),
        "daily": {key.isoformat(): value for key, value in expected.items()},
        "volume_anomalies": volume_anomalies,
        "historical_days_used": len(historical_rows),
        "discovery_evidence": discovery,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = source.replace(".", "_")
    json_path = output_dir / f"{stem}_observation_review.json"
    md_path = output_dir / f"{stem}_observation_review.md"
    png_path = output_dir / f"{stem}_candidate_daily.png"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        f"# Observation review: {source}", "",
        f"- Run: `{manifest.get('run_id')}`",
        f"- Window: {start} through {end}",
        f"- Status: **{report['status']}**",
        f"- Safe-through candidate: **{report['safe_through_candidate'] or 'blocked'}**",
        f"- Discovered / eligible in window: {manifest.get('counts', {}).get('discovered', 0)} / {report['eligible_in_window']}",
        f"- Verified source-policy exclusions: {len(policy_exclusions)}",
        f"- Spillover before / after window: {spillover.get('before_window', 0)} / {spillover.get('after_window', 0)}",
        "", "## Blocking findings", "",
    ]
    if not discovery_failures and not undated_unresolved and not blocking_dates:
        lines.append("No blocking findings were detected.")
    else:
        lines.extend([
            f"- Discovery or runtime failures: {len(discovery_failures)}",
            f"- Unknown-window field failures: {len(undated_unresolved)}",
            f"- In-window field failure dates: {len(unresolved_by_day)}",
            f"- Volume anomaly dates: {len(volume_anomalies)}",
        ])
        failure_kinds = sorted({item["kind"] for item in discovery_failures})
        if failure_kinds:
            lines.append(f"- Discovery/runtime failure kinds: {', '.join(failure_kinds)}")
        if discovery.get("access_status"):
            lines.append(f"- Publisher access status: **{discovery['access_status']}**")
    lines.extend(["", "## Issue categories", ""])
    if code_counts:
        lines.extend(f"- `{code}`: {count}" for code, count in code_counts.most_common())
    else:
        lines.append("No field issues were recorded.")
    lines.extend(["", "## Daily volume review", ""])
    if verified_inactive_window:
        lines.append("Publisher discovery verified no publications in this window; the zero-count plot is an inactivity record.")
    if not counts_by_day and not verified_inactive_window:
        lines.append("Daily zero counts are suppressed because no eligible article volume was verified; they do not prove publisher inactivity.")
    else:
        lines.extend(["| Date | Eligible | Expected median | MAD | Flag / basis |", "|---|---:|---:|---:|---|"])
        for day, item in expected.items():
            lines.append(f"| {day} | {item['observed']} | {item['weekday_median']} | {item['weekday_mad']} | {item['flag'] or ''} ({item['comparison_basis']}) |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not counts_by_day and not verified_inactive_window:
        # Without explicit publisher inactivity evidence, a zero chart could
        # disguise blocked access or failed parsing.
        png_path.unlink(missing_ok=True)
        png_path = None
    else:
        days = list(date_range(start, end))
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            try:
                draw_daily_pillow(png_path, source, days, expected, volume_anomalies,
                                  len(historical_rows), verified_inactive_window)
            except ImportError:
                png_path = None
        else:
            values = [counts_by_day[day] for day in days]
            medians = [
                expected[day]["weekday_median"]
                if expected[day]["comparison_basis"] in COMPARISON_LABELS else float("nan")
                for day in days
            ]
            fig, ax = plt.subplots(figsize=(12, 5.5))
            if historical_rows:
                ax.plot([item["date"] for item in historical_rows], [item["count"] for item in historical_rows], color="#9aa4b2", alpha=0.45, linewidth=1, label="DB history (90 days)")
            ax.plot(days, values, color="#1769aa", marker="o", linewidth=2, label="eligible candidates")
            if any(expected[day]["comparison_basis"] in COMPARISON_LABELS for day in days):
                ax.plot(days, medians, color="#e07a1f", linestyle="--", linewidth=1.5,
                        label=comparison_label(expected))
            for item in volume_anomalies:
                day = date.fromisoformat(item["date"])
                ax.scatter(day, item["observed"], color="#c62828", zorder=5)
            ax.axvspan(start, end, color="#1769aa", alpha=0.06)
            title = f"{source}: candidate daily publication volume"
            if verified_inactive_window:
                title += " (publisher window verified inactive)"
            ax.set_title(title)
            ax.set_ylabel("unique article URLs")
            ax.grid(axis="y", alpha=0.2)
            ax.legend(frameon=False, ncol=3, fontsize=9)
            fig.autofmt_xdate()
            fig.tight_layout()
            fig.savefig(png_path, dpi=160)
            plt.close(fig)

    return {"report": report, "json": str(json_path), "markdown": str(md_path), "plot": str(png_path) if png_path else None}
