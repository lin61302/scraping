#!/usr/bin/env python3
"""Render ZimLive's exact-verified production update volume."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--postcheck", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metrics = json.loads(args.metrics.read_text())
    check = json.loads(args.postcheck.read_text())
    review = json.loads(args.review.read_text())
    source = "zimlive.com"
    start, end = date(2026, 4, 17), date(2026, 9, 16)
    rows = [row for row in metrics["daily"] if row["source"] == source]
    updated = [row for row in rows if start <= date.fromisoformat(row["date"]) <= end]
    assert check["expected_eligible"] == check["exact_match"] == 393
    assert not check["missing_urls"] and not check["duplicate_urls"] and not check["field_mismatches"]
    assert sum(row["unique_urls"] for row in updated) == 393
    assert all(row["rows"] == row["unique_urls"] and not row["missing_title"] and not row["missing_maintext"] for row in updated)
    assert not review["volume_anomalies"] and not review["discovery_or_runtime_failures"]
    assert review["safe_through_candidate"] == end.isoformat()

    counts = {date.fromisoformat(row["date"]): row["unique_urls"] for row in rows}
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    values = [counts.get(day, 0) for day in days]
    historical = [(date.fromisoformat(row["date"]), row["unique_urls"]) for row in rows if date.fromisoformat(row["date"]) < start]
    historical = historical[-90:]
    monthly = Counter()
    for day, value in zip(days, values):
        monthly[day.strftime("%b")] += value

    fig, (daily, bars) = plt.subplots(2, 1, figsize=(13, 7.5), gridspec_kw={"height_ratios": [3, 1.3]})
    if historical:
        daily.plot([day for day, _ in historical], [count for _, count in historical], color="#a9b3c2", linewidth=1.1, label="Earlier DB records")
    daily.plot(days, values, color="#1769aa", linewidth=1.7, marker="o", markersize=2.5, label="Verified update")
    daily.axvline(start, color="#8993a3", linestyle="--", linewidth=1)
    daily.set_title("ZimLive · exact-verified production volume", loc="left", fontsize=16)
    daily.set_ylabel("Unique article URLs per day")
    daily.grid(axis="y", color="#d7dee8", alpha=.7)
    daily.legend(frameon=False, loc="upper left")
    daily.xaxis.set_major_locator(mdates.MonthLocator())
    daily.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    daily.spines[["top", "right"]].set_visible(False)

    labels = ["Apr 17–30", "May", "Jun", "Jul", "Aug", "Sep 1–16"]
    values_month = [monthly[name] for name in ("Apr", "May", "Jun", "Jul", "Aug", "Sep")]
    bars.bar(labels, values_month, color="#1769aa", width=.58)
    for index, value in enumerate(values_month):
        bars.text(index, value + 1.5, str(value), ha="center", fontsize=10)
    bars.set_ylim(0, max(values_month) * 1.18)
    bars.set_ylabel("Eligible URLs")
    bars.spines[["top", "right"]].set_visible(False)
    fig.text(.01, .012, "393/393 exact DB matches through Sep 16 · 66 first-party category exclusions · no unresolved field or volume issues", fontsize=9, color="#475569")
    fig.tight_layout(rect=(0, .035, 1, 1))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=170, facecolor="white")
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
