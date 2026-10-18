from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def load_country_map(path: Path) -> dict[str, list[dict[str, str]]]:
    groups = defaultdict(list)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT source_id, project, country_code, domain
               FROM sources
               ORDER BY country_code, project, domain"""
        ).fetchall()
    for row in rows:
        groups[row["country_code"]].append({
            "source_id": row["source_id"],
            "project": row["project"],
            "domain": row["domain"],
        })
    return groups


def load_watermarks(path: Path) -> dict[str, dict]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT s.source_id, s.project, s.country_code, s.domain,
                   w.discovery_through, w.parsed_through, w.validated_through,
                   w.safe_through, w.confidence, w.run_id, w.assessed_at
            FROM sources s
            LEFT JOIN watermarks w USING (source_id)
            """
        ).fetchall()
    return {row["source_id"]: dict(row) for row in rows}


def monthly_plot(
    country: str, domains: list[str], frame: pd.DataFrame, output: Path, end: date
) -> None:
    columns = 2
    rows = math.ceil(len(domains) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(14, max(4, rows * 3.2)), squeeze=False)
    for ax, source in zip(axes.flat, domains):
        data = frame[frame.source == source].copy()
        future = data[data["date"].dt.date > end]
        data = data[data["date"].dt.date <= end]
        if data.empty:
            ax.text(.5, .5, "No dated 2026 rows", ha="center", va="center", transform=ax.transAxes, color="#6b7280")
        else:
            data["month"] = data["date"].dt.to_period("M").dt.to_timestamp()
            agg = data.groupby("month", as_index=False).agg(rows=("rows", "sum"), unique_urls=("unique_urls", "sum"))
            months = agg["month"].to_numpy()
            stored = agg["rows"].to_numpy()
            unique = agg["unique_urls"].to_numpy()
            ax.plot(months, stored, color="#ca3c25", lw=2, marker="o", ms=3, label="stored rows")
            ax.plot(months, unique, color="#16697a", lw=2, marker="o", ms=3, label="unique URLs")
            ax.fill_between(months, unique, stored, color="#ca3c25", alpha=.12)
        future_count = int(future.rows.sum()) if not future.empty else 0
        if future_count:
            ax.text(
                .99, .96, f"{future_count:,} future-dated rows excluded", ha="right", va="top",
                transform=ax.transAxes, color="#b91c1c", fontsize=7,
            )
        ax.set_title(source, loc="left", fontsize=10, fontweight="bold")
        ax.grid(axis="y", color="#e5e7eb", lw=.7)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
        ax.tick_params(labelsize=8)
    for ax in axes.flat[len(domains):]:
        ax.axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, ncol=2, loc="upper right", bbox_to_anchor=(.98, .992), frameon=False)
    fig.suptitle(
        f"{country} · 2026 monthly article volume", x=.02, y=.998,
        ha="left", va="top", fontsize=17, fontweight="bold",
    )
    fig.text(
        .02, .972,
        f"Stored rows versus unique URLs through {end.isoformat()}. Dates after the cutoff are excluded and flagged.",
        va="top", fontsize=10, color="#4b5563",
    )
    fig.tight_layout(rect=(0, 0, 1, .93))
    fig.savefig(output, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def recent_heatmap(country: str, domains: list[str], frame: pd.DataFrame, output: Path, end: date) -> None:
    start = end - timedelta(days=119)
    dates = pd.date_range(start, end, freq="D")
    matrix = []
    for source in domains:
        source_frame = frame[(frame.source == source) & (frame.date.dt.date >= start) & (frame.date.dt.date <= end)]
        values = source_frame.groupby("date").unique_urls.sum().reindex(dates, fill_value=0)
        matrix.append(values.to_numpy())
    values = np.log1p(np.array(matrix, dtype=float))
    fig, ax = plt.subplots(figsize=(15, max(4.5, len(domains) * .5 + 2)))
    image = ax.imshow(values, aspect="auto", interpolation="nearest", cmap="YlGnBu")
    ax.set_yticks(np.arange(len(domains)))
    ax.set_yticklabels(domains, fontsize=8)
    tick_idx = np.linspace(0, len(dates) - 1, min(9, len(dates)), dtype=int)
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([dates[i].strftime("%b %d") for i in tick_idx], fontsize=8)
    ax.set_title(f"{country} · unique URLs per publication day · {start.isoformat()} to {end.isoformat()}", loc="left", fontsize=15, fontweight="bold", pad=12)
    ax.set_xlabel("Color uses log(1 + unique URL count); blank days are zero")
    for spine in ax.spines.values():
        spine.set_visible(False)
    bar = fig.colorbar(image, ax=ax, fraction=.02, pad=.015)
    bar.set_label("log volume")
    fig.tight_layout()
    fig.savefig(output, dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def build_source_row(
    source: dict[str, str], source_summary: dict[str, dict], frame: pd.DataFrame,
    watermark: dict | None, as_of: date,
) -> dict:
    domain = source["domain"]
    summary = dict(source_summary.get(domain, {"source": domain}))
    data = frame[frame.source == domain]
    through_cutoff = data[data["date"].dt.date <= as_of]
    future = data[data["date"].dt.date > as_of]
    raw = int(data.rows.sum()) if not data.empty else 0
    unique = int(data.unique_urls.sum()) if not data.empty else 0
    summary["source_id"] = source.get("source_id") or None
    summary["project"] = source.get("project") or None
    summary["unique_urls"] = unique
    summary["duplicate_share"] = round((raw - unique) / raw, 4) if raw else 0
    cutoff_dates = sorted(through_cutoff["date"].dt.date.unique()) if not through_cutoff.empty else []
    latest_through = cutoff_dates[-1].isoformat() if cutoff_dates else None
    summary["latest_date_through_as_of"] = latest_through
    summary["days_behind_as_of"] = (as_of - date.fromisoformat(latest_through)).days if latest_through else None

    future_dates = sorted(future["date"].dt.date.unique()) if not future.empty else []
    summary["future_date_count"] = int(future.rows.sum()) if not future.empty else 0
    summary["future_date_unique_urls"] = int(future.unique_urls.sum()) if not future.empty else 0
    summary["future_date_min"] = future_dates[0].isoformat() if future_dates else None
    summary["future_date_max"] = future_dates[-1].isoformat() if future_dates else None
    summary["future_date_sample"] = [value.isoformat() for value in future_dates[:5]]
    future_urls = []
    if "url_samples" in future:
        for samples in future["url_samples"]:
            if isinstance(samples, list):
                future_urls.extend(str(value) for value in samples if value)
    summary["future_url_sample"] = sorted(set(future_urls))[:10]

    watermark = watermark or {}
    for field in (
        "discovery_through", "parsed_through", "validated_through", "safe_through",
        "confidence", "run_id", "assessed_at",
    ):
        output_field = f"watermark_{field}" if field in ("run_id", "assessed_at") else field
        summary[output_field] = watermark.get(field)
    summary["resume_through"] = watermark.get("validated_through")
    if watermark.get("safe_through"):
        confidence = watermark.get("confidence") or "unknown"
        summary["safe_through_reason"] = f"Authoritative control.db watermark ({confidence} confidence)"
    else:
        summary["safe_through_reason"] = "No authoritative watermark recorded in control.db"
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", required=True)
    parser.add_argument(
        "--inventory",
        help="Deprecated compatibility option; source groups come from --control-db.",
    )
    parser.add_argument("--control-db", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--countries", nargs="+", required=True)
    args = parser.parse_args()
    metrics = json.loads(Path(args.metrics).read_text())
    control_db = Path(args.control_db)
    groups = load_country_map(control_db)
    watermarks = load_watermarks(control_db)
    frame = pd.DataFrame(metrics["daily"])
    frame["date"] = pd.to_datetime(frame["date"])
    source_summary = {row["source"]: row for row in metrics["sources"]}
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    as_of = date.fromisoformat(args.as_of)

    for country in args.countries:
        sources = groups[country]
        domains = [source["domain"] for source in sources]
        directory = output_root / country
        directory.mkdir(parents=True, exist_ok=True)
        monthly_plot(country, domains, frame, directory / f"{country}_monthly_volume_2026.png", as_of)
        recent_heatmap(country, domains, frame, directory / f"{country}_recent_daily_2026.png", as_of)
        rows = []
        for source in sources:
            rows.append(build_source_row(
                source, source_summary, frame, watermarks.get(source["source_id"]), as_of,
            ))
        (directory / f"{country}_baseline_summary.json").write_text(json.dumps(rows, indent=2) + "\n")
        with (directory / f"{country}_baseline_summary.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        alerts = []
        for row in rows:
            reasons = []
            if row["duplicate_share"] >= .1:
                reasons.append(f"{row['duplicate_share']:.1%} duplicate share")
            if row.get("nodate_downloads", 0):
                reasons.append(f"{row['nodate_downloads']} undated 2026 downloads")
            if row.get("missing_title", 0) or row.get("missing_maintext", 0):
                reasons.append(f"missing title/maintext {row.get('missing_title', 0)}/{row.get('missing_maintext', 0)}")
            if row.get("future_date_count", 0):
                reasons.append(
                    f"{row['future_date_count']} future-dated rows "
                    f"({row['future_date_min']} to {row['future_date_max']})"
                )
                if row.get("future_url_sample"):
                    reasons.append(f"sample URL {row['future_url_sample'][0]}")
            if row.get("days_behind_as_of") is None or row["days_behind_as_of"] > 14:
                reasons.append(f"latest through cutoff {row.get('latest_date_through_as_of') or 'none'}")
            if reasons:
                alerts.append(f"- **{row['source']}**: " + "; ".join(reasons))
        report = [
            f"# {country} baseline anomaly summary", "",
            f"Captured from production collections at {metrics['captured_at_utc']}; reporting cutoff {as_of.isoformat()}.", "",
            "Safe-through and resume dates below come from the authoritative control database. Monthly plots exclude dates after the reporting cutoff.", "",
            "## Alerts", "", *(alerts or ["- No rule-based baseline alerts."]), "",
        ]
        (directory / f"{country}_baseline_summary.md").write_text("\n".join(report), encoding="utf-8")


if __name__ == "__main__":
    main()
