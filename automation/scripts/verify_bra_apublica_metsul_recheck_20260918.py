"""Read-only evidence gate for the 2026-09-18 A Pública/MetSul rechecks.

Run on 17 after mirroring the two monthly plots. This verifies the immutable
original and recheck audit indexes, every discovered URL and required field,
the four narrow podcast exclusions, the discovery inventories, and the plots.
It does not write to production or the control database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from scripts.record_verified_segment import _pure_inventory_complete
from scripts.verify_cycle_audit import verify as verify_audit


SOURCES = {
    "mleed:BRA:apublica.org": {
        "name": "apublica.org", "original": "orchestrate-20260918T004707Z-4fd0c23a",
        "original_sha": "439af384d578d1a3cdbe3de7e2d41340275ea96e51124dac494670fa827bc3bc",
        "recheck": "orchestrate-20260918T034454Z-5a2b467e",
        "recheck_sha": "7138fe98724db613293e9c8f5669091386e6308974c113cb27336d4ae71fd535",
        "adapter_sha": "e86f11da60903518ddb51fcb471032a76456f21b9d26efc270bfc5651523fa15",
        "selected": 18, "passed": 14, "skipped": 4,
        "monthly_plot": "apublica_org_recheck_candidate_monthly_20260916.png",
        "monthly_plot_sha": "e17a0fe07120bc985fb0fb092d764bfb374a5c1ee0c19a9fa2f2d5a271851cce",
    },
    "mleed:BRA:metsul.com": {
        "name": "metsul.com", "original": "orchestrate-20260918T005427Z-da18c9a7",
        "original_sha": "b671ac814086ba5c296670cf58397dae06c64d1c02a1510cc2e007c2b2ae09b6",
        "recheck": "orchestrate-20260918T034555Z-d90aa9de",
        "recheck_sha": "4e707d7e7c97e26a6c7b3ac96da2a8daf560ea9b22c5f8b369d30807a9e103b8",
        "adapter_sha": "5cf4dd9764915b7558ed4ebed5f2e9ff29ec83af22dcd8e0eb23af20271bb5a0",
        "selected": 174, "passed": 174, "skipped": 0,
        "monthly_plot": "metsul_com_recheck_candidate_monthly_20260916.png",
        "monthly_plot_sha": "8bf9c70d7c5899e46f0a170b67f298a750b306df43b3d369e86e058308d28a4f",
    },
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path: Path) -> dict[str, dict]:
    items = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    result = {item["url"]: item for item in items}
    require(len(result) == len(items), f"duplicate_url:{path}")
    return result


def artifact(index: dict, source: dict, key: str) -> Path | None:
    record = source["artifacts"][key]
    if not record["exists"]:
        return None
    path = Path(index["roots"][record["root"]]) / record["path"]
    require(path.is_file() and digest(path) == record["sha256"], f"artifact_hash_mismatch:{key}")
    return path


def source_record(index: dict, source_id: str) -> dict:
    hits = [source for source in index["sources"] if source["source_id"] == source_id]
    require(len(hits) == 1 and index["source_count"] == 1, f"source_audit_identity:{source_id}")
    return hits[0]


def verify_source(root: Path, source_id: str, setting: dict) -> dict:
    old_audit = root / "status/orchestrator/runs" / (setting["original"] + ".audit.json")
    new_audit = root / "status/bra-apublica-metsul-recheck-20260918/runs" / (setting["recheck"] + ".audit.json")
    for path, expected in ((old_audit, setting["original_sha"]), (new_audit, setting["recheck_sha"])):
        require(digest(path) == expected, f"audit_index_hash_mismatch:{path}")
        receipt = verify_audit(path)
        require(receipt["passed"] and not receipt["findings"], f"audit_verification_failed:{path}")
    old_index = json.loads(old_audit.read_text())
    new_index = json.loads(new_audit.read_text())
    old = source_record(old_index, source_id)
    new = source_record(new_index, source_id)
    require(new_index["commit_enabled"] is False and new["status"] == "ready_for_commit",
            f"recheck_not_observe_ready:{source_id}")
    expected_counts = {"discovered": setting["selected"], "selected": setting["selected"],
                       "passed": setting["passed"], "quarantined": 0,
                       "skipped": setting["skipped"], "exceptions": 0}
    require(new["manifest_counts"] == expected_counts, f"recheck_count_mismatch:{source_id}")
    require(new["window"] == {"start": "2026-08-10", "end": "2026-09-16"},
            f"recheck_window_mismatch:{source_id}")
    adapter = root / "adapters/BRA" / ("apublica.py" if setting["name"] == "apublica.org" else "metsul.py")
    require(digest(adapter) == setting["adapter_sha"], f"active_adapter_changed:{source_id}")
    manifest = json.loads(artifact(new_index, new, "observation_manifest").read_text())
    review = json.loads(artifact(new_index, new, "observation_review").read_text())
    dry = json.loads(artifact(new_index, new, "promotion_dry_run").read_text())
    require(manifest["source_sha256"] == setting["adapter_sha"] and manifest["counts"] == expected_counts,
            f"loaded_adapter_or_manifest_mismatch:{source_id}")
    require(review["status"] == "ready_for_dry_run" and review["safe_through_candidate"] == "2026-09-16" and
            review["manifest_counts"] == expected_counts and not review["volume_anomalies"] and
            not review["discovery_or_runtime_failures"] and not review["issue_code_counts"],
            f"review_not_clean:{source_id}")
    discovery = manifest["discovery_evidence"]
    require(discovery == review["discovery_evidence"] and discovery.get("coverage_complete") is True,
            f"discovery_evidence_mismatch:{source_id}")
    _pure_inventory_complete(setting["name"], discovery, date(2026, 8, 10), date(2026, 9, 16),
                             setting["selected"])
    require(dry["mode"] == "dry_run" and dry["eligible"] == setting["passed"] and
            dry["new"] == setting["passed"] and dry["already_present"] == 0 and
            dry["rejected"] == 0 and not dry["errors"] and dry["inserted"] == 0,
            f"dry_run_not_clean:{source_id}")
    require(artifact(new_index, new, "promotion_commit") is None and
            artifact(new_index, new, "exact_postcheck") is None,
            f"unexpected_production_artifact:{source_id}")
    old_urls = rows(artifact(old_index, old, "discovered_urls"))
    old_articles = rows(artifact(old_index, old, "parsed_articles"))
    new_urls = rows(artifact(new_index, new, "discovered_urls"))
    articles = rows(artifact(new_index, new, "parsed_articles"))
    checks = rows(artifact(new_index, new, "field_checks"))
    require(len(new_urls) == setting["selected"] and
            set(old_urls) == set(new_urls) == set(old_articles) == set(articles) == set(checks),
            f"url_set_not_reconciled:{source_id}")
    excluded = set()
    eligible = set()
    monthly = Counter()
    for url, item in checks.items():
        require(all(any(field["field"] == name and field["status"] == "pass" for field in item["fields"])
                    for name in ("date_publish", "title", "maintext")) and
                all(field["status"] == "pass" for field in item["fields"]),
                f"field_check_failed:{url}")
        article = articles[url]
        require(all(article.get(name) == old_articles[url].get(name)
                    for name in ("date_publish", "title", "maintext")),
                f"parsed_field_changed_from_original:{url}")
        require(article["title"].strip() and article["maintext"].strip() and
                date(2026, 8, 10) <= datetime.fromisoformat(article["date_publish"]).date() <= date(2026, 9, 16),
                f"article_field_or_window_invalid:{url}")
        if item.get("excluded"):
            excluded.add(url)
            require(not item.get("eligible") and
                    item.get("exclusion_reason") == "excluded_podcast_description_without_transcript" and
                    setting["name"] == "apublica.org", f"unexpected_exclusion:{url}")
        else:
            require(item.get("eligible") is True, f"unclassified_article:{url}")
            eligible.add(url)
            monthly[article["date_publish"][:7]] += 1
    require(len(eligible) == setting["passed"] and len(excluded) == setting["skipped"],
            f"eligible_excluded_count_mismatch:{source_id}")
    policy = review["policy_exclusions"]
    require({item["url"] for item in policy} == excluded and policy == dry["excluded"],
            f"policy_dry_run_mismatch:{source_id}")
    if setting["skipped"]:
        issue_path = artifact(new_index, new, "observation_issues")
        require(issue_path is not None, f"missing_policy_issue_file:{source_id}")
        issues = rows(issue_path)
        require(set(issues) == excluded and all(item["status"] == "excluded" and
                item["code"] == "excluded_podcast_description_without_transcript"
                for item in issues.values()), f"policy_issue_mismatch:{source_id}")
    else:
        require(artifact(new_index, new, "observation_issues") is None,
                f"unexpected_issue_file:{source_id}")
    daily = review["daily"]
    require(sum(day["observed"] for day in daily.values()) == setting["passed"] and
            all(day["flag"] is None for day in daily.values()), f"daily_volume_mismatch:{source_id}")
    daily_plot = artifact(new_index, new, "candidate_daily_plot")
    monthly_plot = root / "plots/BRA" / setting["monthly_plot"]
    require(digest(monthly_plot) == setting["monthly_plot_sha"], f"monthly_plot_hash_mismatch:{source_id}")
    return {"source_id": source_id, "original_audit_sha256": setting["original_sha"],
            "recheck_audit_sha256": setting["recheck_sha"], "counts": expected_counts,
            "original_and_recheck_url_sets_equal": True, "original_and_recheck_required_fields_equal": True,
            "all_required_field_checks_pass": True, "publisher_inventory_complete": True,
            "policy_exclusion_urls": sorted(excluded), "candidate_monthly": dict(sorted(monthly.items())),
            "daily_plot_sha256": digest(daily_plot), "monthly_plot_sha256": digest(monthly_plot),
            "promotion_dry_run": {key: dry[key] for key in ("eligible", "new", "already_present", "rejected", "inserted")},
            "production_commit": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--automation-root", type=Path, default=Path("scraping_automation"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.automation_root.resolve()
    result = {source_id: verify_source(root, source_id, setting) for source_id, setting in SOURCES.items()}
    text = json.dumps({"kind": "bra_apublica_metsul_recheck_verification", "passed": True,
                       "sources": result}, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
