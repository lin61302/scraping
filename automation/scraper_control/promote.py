from __future__ import annotations

"""Idempotent promotion of a completed, fully checked observation run."""

import argparse
import hashlib
import json
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .evidence import compare_required_evidence
from .observe import adapter_bundle_sha256
from .thesun_five import URLS as THESUN_FIVE_URLS, validate_article_recovery as validate_five_recovery
from .validation import (
    THESUN_REDIRECT_LOOP_POLICY, THESUN_REDIRECT_LOOP_URL,
    THESUN_REMOVED_REDIRECT_URL,
    resolve_policy_exclusion, resolve_with_page_evidence, validate_article,
    validate_thesun_redirect_loop_manifest, validate_thesun_redirect_loop_recovery,
)


def _read_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _parse_date(value: object) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _load_candidates(
    run_dir: Path,
    manifest: dict,
    through_date: Optional[date] = None,
) -> tuple[list[dict], dict]:
    start = date.fromisoformat(manifest["requested_start"])
    end = date.fromisoformat(manifest["requested_end"])
    selected = list(_read_jsonl(run_dir / "urls.jsonl"))
    articles = list(_read_jsonl(run_dir / "articles.jsonl"))
    checks = list(_read_jsonl(run_dir / "field_checks.jsonl"))
    counts = manifest["counts"]
    if counts["selected"] != counts["discovered"]:
        raise ValueError("Sampled observation cannot be promoted; every discovered URL must be selected")
    if counts.get("exceptions"):
        raise ValueError("Observation has parse exceptions")
    discovery = manifest.get("discovery_evidence", {})
    for key in ("sitemap_failures", "listing_page_failures", "page_failures"):
        if discovery.get(key):
            raise ValueError(f"Discovery is incomplete: {key}")
    if not selected:
        zero_counts_clean = all(not counts.get(key, 0) for key in ("selected", "discovered", "passed", "quarantined", "skipped", "exceptions"))
        inactivity_signal = bool(
            discovery.get("publication_inactive_for_window")
            or discovery.get("inactivity_verified")
        )
        if not (
            manifest.get("adapter_contract") == "pure_network_only"
            and zero_counts_clean
            and not articles
            and not checks
            and discovery.get("coverage_complete") is True
            and inactivity_signal
        ):
            raise ValueError("Zero-URL observation lacks complete live publication-inactivity evidence")
        return [], {
            "selected": 0, "rejected": [], "auto_resolved": [], "excluded": [],
            "window_filtered": [], "deferred_after_cutoff": [],
            "verified_inactivity": {
                "requested_start": start.isoformat(),
                "requested_end": (through_date or end).isoformat(),
                "newest_visible_publication_date": discovery.get("newest_visible_publication_date"),
                "strategy": discovery.get("strategy"),
            },
        }
    if len(selected) != counts["selected"] or len(articles) != len(selected) or len(checks) != len(selected):
        raise ValueError("Observation JSONL files do not account for every selected URL")
    selected_urls = [row["url"] for row in selected]
    if len(set(selected_urls)) != len(selected_urls):
        raise ValueError("Observation has duplicate discovered URLs")
    if not validate_thesun_redirect_loop_manifest(manifest, selected_urls):
        raise ValueError("theSun repair manifest provenance is inconsistent")
    article_by_url = {row["url"]: row for row in articles}
    check_by_url = {row["url"]: row for row in checks}
    if set(selected_urls) != set(article_by_url) or set(selected_urls) != set(check_by_url):
        raise ValueError("Article or field-check URL coverage does not match discovery")
    identity_mismatches = [
        url for url, article in article_by_url.items()
        if (
            article.get("source_domain") != manifest.get("source")
            or article.get("country_code") != manifest.get("country")
        )
    ]
    if identity_mismatches:
        raise ValueError(
            f"Article source or country identity does not match manifest: {identity_mismatches[:10]}"
        )
    eligible = []
    rejected = []
    auto_resolved = []
    excluded = []
    window_filtered = []
    deferred_after_cutoff = []
    for url in selected_urls:
        article = article_by_url[url]
        recorded = check_by_url[url]
        parsed = _parse_date(article.get("date_publish"))
        current = validate_article(article, start, end, manifest["source"])
        if article.get("parser_error"):
            rejected.append(url)
            continue
        page_evidence = recorded.get("page_evidence") or {}
        loop_recovery_required = url == THESUN_REDIRECT_LOOP_URL
        five_recovery_required = url in THESUN_FIVE_URLS
        exact_recovery_evidence_required = bool(
            loop_recovery_required
            or five_recovery_required
            or article.get("collection") == "SKIP"
        )
        article_evidence = article.get("field_evidence")
        if (
            exact_recovery_evidence_required
            and (
                not isinstance(article_evidence, dict)
                or article_evidence != page_evidence
                or (
                    loop_recovery_required
                    and article.get("recovery_policy") != THESUN_REDIRECT_LOOP_POLICY
                )
            )
        ) or not validate_thesun_redirect_loop_recovery(article, page_evidence) or not validate_five_recovery(article, page_evidence):
            rejected.append(url)
            continue
        evidence_checks = compare_required_evidence(article, page_evidence) if page_evidence else []
        expected_field_count = len(current.fields) + len(evidence_checks)
        if len(recorded.get("fields", [])) != expected_field_count:
            rejected.append(url)
            continue
        # An explicit source-policy exclusion must be revalidated before any
        # generic date-window filter. Otherwise a derived pre-window SKIP is
        # mislabeled as window_filtered and manifest skipped accounting fails.
        if article.get("collection") == "SKIP":
            reason = article.get("exclusion_reason")
            exclusion_evidence = article.get("exclusion_evidence")
            exclusion_valid, exclusion_resolution = resolve_policy_exclusion(
                current, evidence_checks, page_evidence, reason, exclusion_evidence,
                article_url=url,
            )
            if not (
                exclusion_valid
                and not recorded.get("eligible")
                and recorded.get("excluded")
                and reason
                and exclusion_evidence
                and recorded.get("exclusion_reason") == reason
                and recorded.get("exclusion_evidence") == exclusion_evidence
                and (
                    recorded.get("exclusion_resolution") == exclusion_resolution
                    or (
                        "exclusion_resolution" not in recorded
                        and exclusion_resolution == {"mode": "fully_valid_article_policy_exclusion"}
                    )
                )
            ):
                rejected.append(url)
                continue
            excluded.append({"url": url, "reason": reason, "evidence": exclusion_evidence})
            continue
        if through_date and parsed and parsed.date() > through_date:
            deferred_after_cutoff.append({
                "url": url,
                "date_publish": parsed.isoformat(),
                "recorded_eligible": bool(recorded.get("eligible")),
                "recorded_excluded": False,
            })
            continue
        current_eligible, resolutions = resolve_with_page_evidence(current, evidence_checks, page_evidence)
        failed_codes = {
            code for item in current.fields if item.status != "pass" for code in item.codes
        }
        date_evidence = next((item for item in evidence_checks if item.field == "date_publish"), None)
        if (
            parsed
            and not start <= parsed.date() <= end
            and date_evidence is not None
            and date_evidence.status == "pass"
            and not recorded.get("eligible")
        ):
            window_filtered.append({
                "url": url,
                "date_publish": parsed.isoformat(),
                "codes": sorted(failed_codes),
            })
            continue
        recorded_eligible = bool(recorded.get("eligible"))
        recorded_resolutions = recorded.get("automatic_resolutions") or []
        recorded_failed_codes = {
            code for item in recorded.get("fields", []) if item.get("status") != "pass"
            for code in item.get("codes", [])
        }
        evidence_revalidation = []
        if (
            current_eligible
            and not recorded_eligible
            and not resolutions
            and recorded_failed_codes
            and recorded_failed_codes <= {"date_signal_disagreement"}
        ):
            evidence_revalidation = [{
                "code": "date_evidence_revalidated_day_first",
                "field": "date_publish",
                "original_codes": sorted(recorded_failed_codes),
            }]
        effective_resolutions = [*resolutions, *evidence_revalidation]
        # Runs created before the resolver existed retain their raw quarantine
        # decision. Revalidation may upgrade only the same narrow, evidence-
        # confirmed brief-article case; every original failed field is kept.
        legacy_resolution = current_eligible and not recorded_eligible and bool(effective_resolutions) and not recorded_resolutions
        if recorded_eligible != current_eligible and not legacy_resolution:
            rejected.append(url)
            continue
        if recorded_resolutions and [item.get("code") for item in recorded_resolutions] != [item.get("code") for item in resolutions]:
            rejected.append(url)
            continue
        if not (
            current_eligible and parsed
            and start <= parsed.date() <= end
            and all(item.status == "pass" for item in evidence_checks)
        ):
            rejected.append(url)
            continue
        candidate = dict(article)
        candidate["_parsed_date"] = parsed
        candidate["_automatic_resolutions"] = effective_resolutions
        eligible.append(candidate)
        if effective_resolutions:
            auto_resolved.append({"url": url, "resolutions": effective_resolutions, "legacy_observation": legacy_resolution})
    if counts.get("skipped", 0) != len(excluded):
        raise ValueError("Policy exclusion accounting does not match manifest skipped count")
    if len(eligible) + len(rejected) + len(excluded) + len(window_filtered) + len(deferred_after_cutoff) != len(selected_urls):
        raise ValueError("Promotion accounting mismatch")
    return eligible, {
        "selected": len(selected_urls), "rejected": rejected, "auto_resolved": auto_resolved,
        "excluded": excluded, "window_filtered": window_filtered,
        "deferred_after_cutoff": deferred_after_cutoff,
        "verified_inactivity": None,
    }


def _existing_urls(db, eligible: list[dict], source: str) -> set[str]:
    grouped: dict[int, set[str]] = defaultdict(set)
    for article in eligible:
        parsed = article["_parsed_date"]
        grouped[parsed.year].add(article["url"])
    existing: set[str] = set()
    for year, urls in grouped.items():
        # A publisher can reuse or move a URL while changing its visible date.
        # Search every monthly article route in the target year so the same URL
        # cannot be inserted again merely because its parsed month or route
        # changed. This remains bounded to 24 indexed membership queries.
        query = {"source_domain": source, "url": {"$in": list(urls)}}
        for month in range(1, 13):
            for prefix in ("articles", "opinion-articles"):
                for row in db[f"{prefix}-{year}-{month}"].find(query, {"url": 1}):
                    if row.get("url"):
                        existing.add(row["url"])
    return existing


def promote(
    run_dir: Path,
    framework_root: Path,
    commit: bool = False,
    through_date: Optional[date] = None,
) -> Dict[str, object]:
    run_dir = run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "observed":
        raise ValueError("Only completed observation runs can be promoted")
    source_file = Path(manifest["source_file"])
    if not source_file.exists():
        raise FileNotFoundError(source_file)
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != manifest["source_sha256"]:
        raise ValueError("Adapter changed after observation; rerun observation")
    if manifest.get("source_bundle_sha256") and adapter_bundle_sha256(source_file) != manifest["source_bundle_sha256"]:
        raise ValueError("Adapter dependency bundle changed after observation; rerun observation")
    start = date.fromisoformat(manifest["requested_start"])
    end = date.fromisoformat(manifest["requested_end"])
    if through_date and not start <= through_date <= end:
        raise ValueError("through_date must be inside the observation window")
    eligible, accounting = _load_candidates(run_dir, manifest, through_date)

    framework_root = framework_root.resolve()
    sys.path.insert(0, str(framework_root))
    from core.base_pipeline import MONGO_DB, MONGO_URI, MongoClient

    db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000)[MONGO_DB]
    existing = _existing_urls(db, eligible, manifest["source"])
    new_articles = [article for article in eligible if article["url"] not in existing]
    result: Dict[str, object] = {
        "promotion_id": f"promote-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        "observation_run_id": manifest["run_id"],
        "source": manifest["source"],
        "mode": "commit" if commit else "dry_run",
        "through_date": through_date.isoformat() if through_date else manifest["requested_end"],
        "selected": accounting["selected"],
        "eligible": len(eligible),
        "rejected": len(accounting["rejected"]),
        "excluded": accounting["excluded"],
        "window_filtered": accounting["window_filtered"],
        "deferred_after_cutoff": accounting["deferred_after_cutoff"],
        "automatic_resolutions": accounting["auto_resolved"],
        "verified_inactivity": accounting.get("verified_inactivity"),
        "already_present": len(eligible) - len(new_articles),
        "new": len(new_articles),
        "inserted": 0,
        "errors": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if commit:
        successful_dates = []
        for article in new_articles:
            parsed = article["_parsed_date"]
            prefix = "opinion-articles" if str(article.get("collection", "")).startswith("opinion-articles") else "articles"
            collection = f"{prefix}-{parsed.year}-{parsed.month}"
            payload = {
                "url": article["url"],
                "source_domain": manifest["source"],
                "title": article["title"],
                "maintext": article["maintext"],
                "date_publish": parsed,
                "date_download": datetime.utcnow(),
                "download_via": f"validated:{article.get('parser', 'unknown')}",
            }
            if manifest.get("is_env"):
                payload["environmental"] = True
                payload["primary_location"] = f"ENV_{manifest['country']}"
            try:
                db[collection].insert_one(payload)
                result["inserted"] += 1
                successful_dates.append(parsed)
                try:
                    db["urls"].insert_one({"url": article["url"]})
                except Exception as exc:
                    result["errors"].append({"url": article["url"], "stage": "url_index", "type": type(exc).__name__})
            except Exception as exc:
                result["errors"].append({"url": article["url"], "stage": "article", "type": type(exc).__name__, "message": str(exc)})
        if successful_dates:
            try:
                db["source_meta"].update_one(
                    {"source_domain": manifest["source"]},
                    {"$max": {"last_date_publish": max(successful_dates)}, "$set": {"updated_at": datetime.utcnow()}},
                    upsert=True,
                )
            except Exception as exc:
                result["errors"].append({"stage": "source_meta", "type": type(exc).__name__, "message": str(exc)})
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    output = run_dir / ("promotion.json" if commit else "promotion_dry_run.json")
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote fully checked observation records idempotently")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--framework-root", required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--through-date", help="Promote only records on or before this YYYY-MM-DD cutoff")
    args = parser.parse_args()
    cutoff = date.fromisoformat(args.through_date) if args.through_date else None
    print(json.dumps(promote(Path(args.run_dir), Path(args.framework_root), args.commit, cutoff), indent=2))


if __name__ == "__main__":
    main()
