#!/usr/bin/env python3
"""Recheck a Zimbabwe Situation low-volume day against first-party evidence.

The original automated review remains immutable. This script emits a hash-bound
decision only after the publisher API, sitemap, and article pages reconcile.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup


BASE = "https://www.zimbabwesituation.com"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--review-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--day", type=date.fromisoformat, default=date(2026, 9, 16))
    args = parser.parse_args()

    run = json.loads((args.run_dir / "manifest.json").read_text())
    review = json.loads(args.review_json.read_text())
    assert run["source"] == review["source"] == "zimbabwesituation.com"
    assert run["status"] == "observed"
    assert run["counts"] == {"discovered": 775, "selected": 775, "passed": 772, "quarantined": 0, "skipped": 3, "exceptions": 0}
    assert review["discovery_or_runtime_failures"] == []
    assert review["undated_or_unknown_window_issues"] == []
    assert review["unresolved_by_date"] == {}
    assert len(review["volume_anomalies"]) == 1
    assert review["volume_anomalies"][0]["date"] == args.day.isoformat()
    assert review["volume_anomalies"][0]["flag"] == "volume_low"

    session = requests.Session()
    params = {
        "after": f"{args.day.isoformat()}T00:00:00",
        "before": f"{args.day.isoformat()}T23:59:59",
        "per_page": 100,
        "_fields": "date,modified,link,title",
    }
    response = session.get(f"{BASE}/wp-json/wp/v2/posts", params=params, timeout=30)
    response.raise_for_status()
    posts = response.json()
    total = int(response.headers["X-WP-Total"])
    pages = int(response.headers["X-WP-TotalPages"])
    assert total == len(posts) == 2 and pages == 1
    assert all(row["date"].startswith(args.day.isoformat()) for row in posts)

    sitemap_rows = []
    for number in (97, 98):
        sitemap_response = session.get(f"{BASE}/post-sitemap{number}.xml", timeout=30)
        sitemap_response.raise_for_status()
        for node in ET.fromstring(sitemap_response.content):
            loc, modified = node.find("{*}loc"), node.find("{*}lastmod")
            if loc is not None and modified is not None and modified.text.startswith(args.day.isoformat()):
                sitemap_rows.append({"url": loc.text, "lastmod": modified.text})
    assert len(sitemap_rows) == 3
    api_urls = {row["link"].rstrip("/") for row in posts}
    assert api_urls <= {row["url"].rstrip("/") for row in sitemap_rows}

    page_evidence = []
    for row in sitemap_rows:
        page = session.get(row["url"], timeout=30)
        page.raise_for_status()
        soup = BeautifulSoup(page.content, "html.parser")
        published = soup.find("meta", attrs={"property": "article:published_time"})
        modified = soup.find("meta", attrs={"property": "article:modified_time"})
        assert published and modified
        page_evidence.append({
            "url": row["url"],
            "http_status": page.status_code,
            "html_sha256": hashlib.sha256(page.content).hexdigest(),
            "article_published_time": published.get("content"),
            "article_modified_time": modified.get("content"),
            "sitemap_lastmod": row["lastmod"],
        })
    published_today = {row["url"].rstrip("/") for row in page_evidence if row["article_published_time"].startswith(args.day.isoformat())}
    assert published_today == api_urls
    earlier = [row for row in page_evidence if row["url"].rstrip("/") not in api_urls]
    assert len(earlier) == 1 and earlier[0]["article_published_time"].startswith("2026-09-15")
    observed = review["daily"][args.day.isoformat()]["observed"]
    assert observed == total

    decision = {
        "source": run["source"],
        "day": args.day.isoformat(),
        "classification": "publisher_confirmed_low_volume",
        "automated_flag": review["volume_anomalies"][0],
        "publisher_api_url": response.url,
        "publisher_api_total": total,
        "publisher_api_pages": pages,
        "publisher_api_posts": [{"url": row["link"], "date_publish": row["date"], "modified": row["modified"]} for row in posts],
        "sitemap_lastmod_on_day": page_evidence,
        "page_confirmed_previous_day_modification_count": len(earlier),
        "eligible_observation_count": observed,
        "full_observation_publisher_total": run["discovery_evidence"]["publisher_total"],
        "full_observation_api_pages": run["discovery_evidence"]["publisher_pages"],
        "full_observation_sitemap_candidates": run["discovery_evidence"]["sitemap_candidate_count"],
        "full_observation_page_confirmed_next_day_exclusions": run["discovery_evidence"]["sitemap_only_exclusion_count"],
        "run_manifest_sha256": sha256(args.run_dir / "manifest.json"),
        "run_field_checks_sha256": sha256(args.run_dir / "field_checks.jsonl"),
        "original_review_sha256": sha256(args.review_json),
        "reason": "All first-party posts published that day are present and validated; the third sitemap lastmod is a prior-day post modified that day.",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    decision_path = args.output_dir / "volume_anomaly_resolution.json"
    decision_path.write_text(json.dumps(decision, indent=2, ensure_ascii=False) + "\n")
    derived = dict(review)
    derived["original_automated_status"] = review["status"]
    derived["original_automated_safe_through_candidate"] = review["safe_through_candidate"]
    derived["review_derivation"] = {
        "kind": "first_party_volume_anomaly_resolution",
        "original_review_sha256": decision["original_review_sha256"],
        "resolution_json_sha256": sha256(decision_path),
    }
    derived["resolved_volume_anomalies"] = [decision]
    derived["status"] = "ready_for_dry_run"
    derived["safe_through_candidate"] = run["requested_end"]
    (args.output_dir / "zimbabwesituation_com_observation_review_resolved.json").write_text(
        json.dumps(derived, indent=2, ensure_ascii=False) + "\n"
    )
    (args.output_dir / "volume_anomaly_resolution.md").write_text(
        "# Zimbabwe Situation volume anomaly resolution\n\n"
        f"Run `{run['run_id']}`. The automated review flagged {args.day} at {observed} eligible posts versus "
        f"its {review['volume_anomalies'][0]['weekday_median']} daily median. "
        f"A fresh first-party REST date query returned exactly {total} published posts across {pages} page; "
        "both URLs occur in the publisher post sitemaps and their live article pages confirm their publication dates. "
        "A third Sep 16 sitemap lastmod belongs to a Sep 15 article revised Sep 16. "
        "The complete 775-post run had zero parse failures, zero unresolved fields, and three verified image-led cartoon exclusions.\n\n"
        "Classification: `publisher_confirmed_low_volume`; the Sep 16 alert is nonblocking. "
        "The machine review is preserved; the hash-bound derived JSON records the reviewed safe-through candidate of Sep 16.\n",
    )
    print(decision_path)


if __name__ == "__main__":
    main()
