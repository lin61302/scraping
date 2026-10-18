#!/usr/bin/env python3
"""Plot exact-verified Zimbabwe Situation production volume."""
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
    parser.add_argument("--resolution", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    metrics = json.loads(args.metrics.read_text())
    check = json.loads(args.postcheck.read_text())
    resolution = json.loads(args.resolution.read_text())
    source = "zimbabwesituation.com"
    rows = [item for item in metrics["daily"] if item["source"] == source]
    assert len(rows) == 38
    assert sum(item["rows"] for item in rows) == check["expected_eligible"] == check["exact_match"] == 772
    assert not check["missing_urls"] and not check["duplicate_urls"] and not check["field_mismatches"]
    assert resolution["classification"] == "publisher_confirmed_low_volume"
    start, end = date(2026, 8, 10), date(2026, 9, 16)
    counts = {date.fromisoformat(item["date"]): item["unique_urls"] for item in rows}
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    values = [counts.get(day, 0) for day in days]
    assert values[-1] == resolution["publisher_api_total"] == 2

    months = Counter()
    for day, value in zip(days, values):
        months[day.strftime("%b")] += value

    fig, (daily, monthly) = plt.subplots(
        2, 1, figsize=(13, 7.3), gridspec_kw={"height_ratios": [3, 1.3]},
    )
    daily.plot(days, values, color="#1769aa", marker="o", markersize=3.5, linewidth=1.8)
    daily.scatter([end], [values[-1]], color="#ba3b27", s=75, zorder=4)
    daily.annotate(
        "Sep 16: 2 publisher posts\nREST, sitemap and pages agree",
        xy=(end, values[-1]), xytext=(-165, 60), textcoords="offset points",
        arrowprops={"arrowstyle": "->", "color": "#ba3b27"},
        color="#893026", fontsize=9,
    )
    daily.set_title("Zimbabwe Situation · exact-verified production volume", loc="left", fontsize=16)
    daily.set_ylabel("Unique article URLs per day")
    daily.grid(axis="y", color="#d7dee8", alpha=.7)
    daily.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    daily.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    daily.spines[["top", "right"]].set_visible(False)

    labels = ["Aug 10–31", "Sep 1–16"]
    values_month = [months["Aug"], months["Sep"]]
    monthly.bar(labels, values_month, color=["#1769aa", "#4789b7"], width=.5)
    for index, value in enumerate(values_month):
        monthly.text(index, value + 8, f"{value:,}", ha="center", fontsize=10)
    monthly.set_ylim(0, max(values_month) * 1.18)
    monthly.set_ylabel("Partial-month total")
    monthly.spines[["top", "right"]].set_visible(False)
    fig.text(.01, .01, "772/772 exact DB matches · 3 verified image-led cartoons excluded · Jan 2025–Aug 9, 2026 remains a historical gap", fontsize=9, color="#475569")
    fig.tight_layout(rect=(0, .035, 1, 1))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=170, facecolor="white")
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
