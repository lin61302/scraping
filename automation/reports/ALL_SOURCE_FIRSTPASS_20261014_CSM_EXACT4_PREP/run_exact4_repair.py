#!/usr/bin/env python3
"""Observe only the four frozen CSM shortfalls and account for 310 preserved passes.

This runner never discovers URLs and has no database or production-write path.
Three exact URLs are expected to become ordinary article passes.  The publisher
image-only comic may count only through the shared, evidence-backed exclusion
validator.  The output remains review evidence until a separate guarded
promotion and downstream pipeline are authorized.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path


AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_ROOT))

from adapters.INT import csmonitor  # noqa: E402
from adapters.INT.common import url_set_sha256  # noqa: E402
from scraper_control.evidence import compare_required_evidence  # noqa: E402
from scraper_control.validation import (  # noqa: E402
    resolve_policy_exclusion,
    resolve_with_page_evidence,
    validate_article,
)


EXPECTED_EXACT_COUNT = 4
EXPECTED_EXACT_URL_SET_SHA256 = "32e97ffa0ecc76e4195deccb75ec2f70ab4e55b0e91dd18c4ecbddd9a6520392"
EXPECTED_PRESERVED_COUNT = 310
EXPECTED_PRESERVED_URL_SET_SHA256 = "c014173ece2982f56d8f64d2f8be0502bd46376aaf4847b970e9e32b36ddee50"
EXPECTED_ORIGINAL_COUNT = 314
EXPECTED_ORIGINAL_URL_SET_SHA256 = "a2c08d16ca758e497857d744d5f6bdca2affc24c2c96f1f7cf3a997ffedcd44e"
EXPECTED_IMAGE_ONLY_URL_SHA256 = "99a3d80cb7828e3ec18a1c5533d8e42ebcd3292717f79921e748388cfbe91609"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_urls(path: Path) -> list[str]:
    rows = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != len(set(rows)):
        raise RuntimeError("URL file contains duplicates: %s" % path)
    return rows


def _write_json(path: Path, payload: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str))
            stream.write("\n")


def validate_inputs(exact_path: Path, preserved_path: Path, original_path: Path) -> dict:
    exact = _read_urls(exact_path)
    preserved = _read_urls(preserved_path)
    original = _read_urls(original_path)
    checks = {
        "exact_count": len(exact),
        "exact_url_set_sha256": url_set_sha256(exact),
        "preserved_count": len(preserved),
        "preserved_url_set_sha256": url_set_sha256(preserved),
        "original_count": len(original),
        "original_url_set_sha256": url_set_sha256(original),
        "preserved_and_exact_disjoint": not bool(set(preserved) & set(exact)),
        "preserved_and_exact_union_equals_original": set(preserved) | set(exact) == set(original),
        "image_only_url_in_exact_scope": any(
            _text_sha256(url) == EXPECTED_IMAGE_ONLY_URL_SHA256 for url in exact
        ),
        "exact_file_sha256": _sha256(exact_path),
        "preserved_file_sha256": _sha256(preserved_path),
        "original_file_sha256": _sha256(original_path),
    }
    accepted = all((
        checks["exact_count"] == EXPECTED_EXACT_COUNT,
        checks["exact_url_set_sha256"] == EXPECTED_EXACT_URL_SET_SHA256,
        checks["preserved_count"] == EXPECTED_PRESERVED_COUNT,
        checks["preserved_url_set_sha256"] == EXPECTED_PRESERVED_URL_SET_SHA256,
        checks["original_count"] == EXPECTED_ORIGINAL_COUNT,
        checks["original_url_set_sha256"] == EXPECTED_ORIGINAL_URL_SET_SHA256,
        checks["preserved_and_exact_disjoint"],
        checks["preserved_and_exact_union_equals_original"],
        checks["image_only_url_in_exact_scope"],
    ))
    checks["accepted"] = accepted
    if not accepted:
        raise RuntimeError("exact-four immutable input gate failed")
    checks["exact_urls"] = exact
    checks["preserved_urls"] = preserved
    checks["original_urls"] = original
    return checks


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--urls-file", required=True, type=Path)
    parser.add_argument("--preserved-urls", required=True, type=Path)
    parser.add_argument("--original-urls", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.start != "2026-08-13" or args.end != "2026-09-30":
        raise RuntimeError("exact-four repair is frozen to 2026-08-13 through 2026-09-30")
    inputs = validate_inputs(args.urls_file, args.preserved_urls, args.original_urls)
    exact_urls = inputs.pop("exact_urls")
    preserved_urls = inputs.pop("preserved_urls")
    original_urls = inputs.pop("original_urls")

    started = datetime.now(timezone.utc)
    run_id = "observe-%s-csmexact4" % started.strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    articles: list[dict] = []
    exclusions: list[dict] = []
    checks: list[dict] = []
    errors: list[dict] = []
    start_day, end_day = date.fromisoformat(args.start), date.fromisoformat(args.end)

    for url in exact_urls:
        try:
            article = dict(csmonitor.scrape_url(url))
            article_check = validate_article(article, start_day, end_day, csmonitor.Config.source_name)
            page_evidence = article.get("field_evidence") or {}
            evidence_checks = compare_required_evidence(article, page_evidence)
            semantic_ok, resolutions = resolve_with_page_evidence(
                article_check, evidence_checks, page_evidence
            )
            is_exclusion = article.get("collection") == "SKIP"
            exclusion_ok, exclusion_resolution = resolve_policy_exclusion(
                article_check,
                evidence_checks,
                page_evidence,
                article.get("exclusion_reason"),
                article.get("exclusion_evidence"),
                article_url=url,
            )
            outcome = {
                "url": url,
                "article_eligible": bool(semantic_ok and not is_exclusion and not article.get("parser_error")),
                "verified_exclusion": bool(is_exclusion and exclusion_ok and not article.get("parser_error")),
                "parser_error": article.get("parser_error"),
                "automatic_resolutions": resolutions,
                "exclusion_resolution": exclusion_resolution,
            }
            checks.append(outcome)
            if outcome["article_eligible"]:
                articles.append(article)
            elif outcome["verified_exclusion"]:
                exclusions.append({
                    "url": url,
                    "reason": article.get("exclusion_reason"),
                    "evidence": article.get("exclusion_evidence"),
                    "resolution": exclusion_resolution,
                    "article": article,
                })
            else:
                errors.append({"url": url, "type": "unresolved_validation", "outcome": outcome})
        except Exception as exc:
            errors.append({"url": url, "type": type(exc).__name__, "message": str(exc)})

    covered_urls = [row["url"] for row in articles] + [row["url"] for row in exclusions]
    recombined = set(preserved_urls) | set(covered_urls)
    accepted = all((
        len(articles) == 3,
        len(exclusions) == 1,
        exclusions
        and _text_sha256(exclusions[0]["url"]) == EXPECTED_IMAGE_ONLY_URL_SHA256,
        not errors,
        len(covered_urls) == 4,
        len(set(covered_urls)) == 4,
        set(covered_urls) == set(exact_urls),
        recombined == set(original_urls),
        url_set_sha256(recombined) == EXPECTED_ORIGINAL_URL_SET_SHA256,
    ))
    recombination = {
        "accepted": accepted,
        "preserved_text_articles": len(preserved_urls),
        "repaired_text_articles": len(articles),
        "verified_publisher_exclusions": len(exclusions),
        "accounted_identities": len(recombined),
        "expected_accounted_identities": EXPECTED_ORIGINAL_COUNT,
        "covered_exact_count": len(covered_urls),
        "covered_exact_url_set_sha256": url_set_sha256(covered_urls),
        "recombined_url_set_sha256": url_set_sha256(recombined),
        "recombined_equals_original": recombined == set(original_urls),
        "missing_exact_urls": sorted(set(exact_urls) - set(covered_urls)),
        "unexpected_urls": sorted(set(covered_urls) - set(exact_urls)),
    }
    _write_jsonl(run_dir / "urls.jsonl", [
        {"url": url, "discovery_method": "exact_four_no_rediscovery"}
        for url in exact_urls
    ])
    _write_jsonl(run_dir / "articles.jsonl", articles)
    _write_jsonl(run_dir / "policy_exclusions.jsonl", exclusions)
    _write_jsonl(run_dir / "field_checks.jsonl", checks)
    _write_jsonl(run_dir / "errors.jsonl", errors)
    _write_json(run_dir / "recombination_check.json", recombination)
    finished = datetime.now(timezone.utc)
    _write_json(run_dir / "manifest.json", {
        "schema_version": 1,
        "run_id": run_id,
        "mode": "observation_no_production_writes",
        "production_writes": False,
        "source": csmonitor.Config.source_name,
        "country": csmonitor.Config.country_code,
        "status": "observed" if accepted else "observed_needs_review",
        "adapter_contract": "pure_network_only_exact_four_no_rediscovery",
        "requested_start": args.start,
        "requested_end": args.end,
        "workers": 1,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "runtime_seconds": (finished - started).total_seconds(),
        "counts": {
            "selected": len(exact_urls),
            "article_passed": len(articles),
            "policy_excluded": len(exclusions),
            "failed": len(errors),
        },
        "input_gate": inputs,
        "recombination_acceptance": recombination,
        "production_promotion_performed": False,
        "tracker_update_performed": False,
    })
    print(json.dumps({
        "run_dir": str(run_dir),
        "article_passed": len(articles),
        "policy_excluded": len(exclusions),
        "failed": len(errors),
        "recombination_accepted": accepted,
    }, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
