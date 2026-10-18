#!/usr/bin/env python3
"""Derive verified Nation image-only exclusions from a complete observation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.PAK.common import clean, fetch
from scraper_control.evidence import compare_required_evidence
from scraper_control.observe import adapter_bundle_sha256
from scraper_control.validation import resolve_policy_exclusion, resolve_with_page_evidence, validate_article


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def apply_image_only_exclusion(article: dict, soup: BeautifulSoup) -> dict | None:
    if clean(article.get("maintext")) or not article.get("title") or not article.get("date_publish"):
        return None
    content = soup.select_one("div.news-detail-content-class, .entry-content")
    if not content:
        return None
    text = clean(content.get_text(" ", strip=True)) or ""
    text = re.sub(r"\bShare on (?:Facebook|Twitter|WhatsApp|Whatsapp)\b", " ", text, flags=re.I)
    if clean(text):
        return None
    image = soup.select_one("meta[property='og:image'][content]")
    if not image:
        return None
    image_url = urljoin(article["url"], image.get("content"))
    parsed = urlparse(image_url)
    if (parsed.hostname or "").casefold() not in {"nation.com.pk", "www.nation.com.pk"}:
        return None
    if not parsed.path.startswith(("/print_images/", "/uploads/print_news/")):
        return None
    selector = "meta[property='og:image']"
    evidence = article.setdefault("field_evidence", {})
    evidence["publisher_images"] = [{"url": image_url, "selector": selector}]
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_image_only_publisher_item"
    article["exclusion_evidence"] = {
        "policy_type": "publisher_image_only",
        "publisher_image_url": image_url,
        "publisher_dom_selector": selector,
        "publisher_item_type": "image_post",
        "structured_article_body_empty": True,
    }
    return {"policy": "publisher_image_only", "image_url": image_url}


def validate_one(article: dict, *, source: str, start: date, end: date) -> tuple[dict, list[dict], str]:
    base = validate_article(article, start, end, source)
    evidence = article.get("field_evidence") if isinstance(article.get("field_evidence"), dict) else {}
    evidence_checks = compare_required_evidence(article, evidence)
    semantic_eligible, resolutions = resolve_with_page_evidence(base, evidence_checks, evidence)
    exclusion_valid, exclusion_resolution = resolve_policy_exclusion(
        base, evidence_checks, evidence, article.get("exclusion_reason"), article.get("exclusion_evidence"),
        article_url=article.get("url"),
    )
    excluded = bool(article.get("collection") == "SKIP" and exclusion_valid and not article.get("parser_error"))
    eligible = bool(semantic_eligible and article.get("collection") != "SKIP" and not article.get("parser_error"))
    check = {
        "url": article["url"],
        "eligible": eligible,
        "fields": [asdict(item) for item in [*base.fields, *evidence_checks]],
        "page_evidence": evidence,
        "automatic_resolutions": resolutions,
        "excluded": excluded,
        "exclusion_reason": article.get("exclusion_reason"),
        "exclusion_evidence": article.get("exclusion_evidence"),
        "exclusion_resolution": exclusion_resolution,
    }
    issues: list[dict] = []
    if excluded:
        bucket = "skipped"
        issues.append({
            "url": article["url"], "source": source, "stage": "source_policy",
            "category": "collection", "code": article.get("exclusion_reason"),
            "severity": "info", "status": "excluded",
            "observed": article.get("exclusion_evidence"), "attempt": 1,
        })
    elif eligible:
        bucket = "passed"
        for resolution in resolutions:
            issues.append({
                "url": article["url"], "source": source, "stage": "validation",
                "category": resolution["field"], "code": resolution["code"],
                "severity": "info", "status": "resolved", "observed": resolution, "attempt": 1,
            })
    else:
        bucket = "quarantined"
        for item in [*base.fields, *evidence_checks]:
            for code in item.codes:
                issues.append({
                    "url": article["url"], "source": source, "stage": "derived_validation",
                    "category": item.field, "code": code, "severity": "error",
                    "status": "open", "observed": item.evidence, "attempt": 1,
                })
    return check, issues, bucket


def derive(parent: Path, output_root: Path) -> Path:
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    counts = manifest.get("counts") or {}
    if manifest.get("source") != "nation.com.pk":
        raise ValueError("This derivation accepts only nation.com.pk")
    if manifest.get("status") != "observed" or counts.get("selected") != counts.get("discovered"):
        raise ValueError("Parent must be a completed full observation")
    if counts.get("exceptions"):
        raise ValueError("Parent has parse exceptions")
    source_file = Path(manifest["source_file"])
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != manifest.get("source_sha256"):
        raise ValueError("Adapter changed after the parent observation")
    if adapter_bundle_sha256(source_file) != manifest.get("source_bundle_sha256"):
        raise ValueError("Adapter bundle changed after the parent observation")

    urls = read_jsonl(parent / "urls.jsonl")
    articles = read_jsonl(parent / "articles.jsonl")
    old_checks = read_jsonl(parent / "field_checks.jsonl")
    if len(urls) != len(articles) or len(urls) != len(old_checks) or len(urls) != counts["selected"]:
        raise ValueError("Parent artifacts do not account for every selected URL")
    article_by_url = {row["url"]: row for row in articles}
    check_by_url = {row["url"]: row for row in old_checks}
    start = date.fromisoformat(manifest["requested_start"])
    end = date.fromisoformat(manifest["requested_end"])

    targets: list[str] = []
    for row in urls:
        url = row["url"]
        article = article_by_url[url]
        recorded = check_by_url[url]
        parsed = datetime.fromisoformat(str(article.get("date_publish"))).date() if article.get("date_publish") else None
        if parsed and start <= parsed <= end and not recorded.get("eligible") and not recorded.get("excluded"):
            base = validate_article(article, start, end, manifest["source"])
            evidence = article.get("field_evidence") or {}
            evidence_checks = compare_required_evidence(article, evidence)
            now_eligible, _ = resolve_with_page_evidence(base, evidence_checks, evidence)
            if not now_eligible:
                targets.append(url)

    repairs: list[dict] = []
    for url in targets:
        article = article_by_url[url]
        soup = BeautifulSoup(fetch(url).content, "html.parser")
        repaired = apply_image_only_exclusion(article, soup)
        if repaired is None:
            raise ValueError(f"In-window quarantine does not match verified Nation image structure: {url}")
        repairs.append({"url": url, **repaired})

    new_articles: list[dict] = []
    new_checks: list[dict] = []
    issues: list[dict] = []
    new_counts = {"discovered": len(urls), "selected": len(urls), "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}
    unresolved_in_window: list[str] = []
    for row in urls:
        article = article_by_url[row["url"]]
        check, row_issues, bucket = validate_one(article, source=manifest["source"], start=start, end=end)
        new_articles.append(article)
        new_checks.append(check)
        issues.extend(row_issues)
        new_counts[bucket] += 1
        parsed = datetime.fromisoformat(str(article.get("date_publish"))).date() if article.get("date_publish") else None
        if parsed and start <= parsed <= end and bucket == "quarantined":
            unresolved_in_window.append(article["url"])
    if unresolved_in_window:
        raise ValueError(f"Derived observation retains in-window quarantines: {unresolved_in_window}")

    run_id = f"observe-derived-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(parent / "urls.jsonl", run_dir / "urls.jsonl")
    write_jsonl(run_dir / "articles.jsonl", new_articles)
    write_jsonl(run_dir / "field_checks.jsonl", new_checks)
    write_jsonl(run_dir / "issues.jsonl", issues)
    now = datetime.now(timezone.utc).isoformat()
    child = dict(manifest)
    child.update({
        "run_id": run_id,
        "started_at": now,
        "finished_at": now,
        "runtime_seconds": 0.0,
        "counts": new_counts,
        "derived_from_run_id": manifest["run_id"],
        "derivation": "verified_nation_image_policy_repair",
        "repairs": repairs,
    })
    (run_dir / "manifest.json").write_text(json.dumps(child, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(
        f"# Nation verified policy repair\n\n- Parent: `{manifest['run_id']}`\n"
        f"- Verified image-only exclusions: {len(repairs)}\n"
        f"- Passed / skipped / quarantined: {new_counts['passed']} / {new_counts['skipped']} / {new_counts['quarantined']}\n",
        encoding="utf-8",
    )
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    print(derive(args.parent_run, args.output_root))


if __name__ == "__main__":
    main()
