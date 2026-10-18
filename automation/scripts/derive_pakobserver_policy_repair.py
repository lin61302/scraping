#!/usr/bin/env python3
"""Derive verified media/link exclusions for Pakistan Observer non-articles.

The parent discovery set and successful article records remain immutable. Only
in-window quarantines are fetched again, and every repair must match one of two
narrow publisher structures: a Daily Cartoon featured image with no body, or a
single Pakistan Observer e-paper link with no independent body text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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


def repair_policy_item(article: dict, soup: BeautifulSoup) -> dict | None:
    evidence = article.setdefault("field_evidence", {})
    title = clean(article.get("title")) or ""
    category = clean(evidence.get("publisher_category"))
    content = soup.select_one("div.single__content.brxe-post-content, div.single__content")
    content_text = clean(content.get_text(" ", strip=True)) if content else None

    if category == "Daily Cartoon" and title.casefold().startswith("daily cartoon") and not content_text:
        image = soup.select_one("main img.single__img[src], article img.single__img[src]")
        if not image:
            return None
        image_url = urljoin(article["url"], image.get("src"))
        if (urlparse(image_url).hostname or "").casefold() not in {"pakobserver.net", "www.pakobserver.net"}:
            return None
        image_evidence = {
            "url": image_url,
            "alt": clean(image.get("alt")),
            "selector": "main img.single__img[src]",
        }
        evidence["publisher_images"] = [image_evidence]
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_image_only_publisher_item"
        article["exclusion_evidence"] = {
            "policy_type": "publisher_image_only",
            "publisher_image_url": image_url,
            "publisher_dom_selector": "main img.single__img[src]",
            "publisher_category": category,
            "structured_article_body_empty": True,
        }
        return {"policy": "publisher_image_only", "image_url": image_url}

    if not content:
        return None
    links = content.select("a[href]")
    if len(links) != 1:
        return None
    link = links[0]
    target_url = urljoin(article["url"], link.get("href"))
    if (urlparse(target_url).hostname or "").casefold() != "epaper.pakobserver.net":
        return None
    clone = BeautifulSoup(str(content), "html.parser")
    for node in clone.select("a[href]"):
        node.decompose()
    if clean(clone.get_text(" ", strip=True)):
        return None
    link_text = clean(link.get_text(" ", strip=True))
    if not link_text:
        return None
    link_evidence = {
        "target_url": target_url,
        "selector": "div.single__content a[href]",
        "link_text": link_text,
        "content_text_empty": True,
    }
    evidence["publisher_links"] = [link_evidence]
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_link_only_publisher_item"
    article["exclusion_evidence"] = {"policy_type": "publisher_link_only", **link_evidence}
    return {"policy": "publisher_link_only", "target_url": target_url}


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
        "url": article["url"], "eligible": eligible,
        "fields": [asdict(item) for item in [*base.fields, *evidence_checks]],
        "page_evidence": evidence, "automatic_resolutions": resolutions,
        "excluded": excluded, "exclusion_reason": article.get("exclusion_reason"),
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
    if manifest.get("source") != "pakobserver.net":
        raise ValueError("This derivation accepts only pakobserver.net")
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
    checks = read_jsonl(parent / "field_checks.jsonl")
    if len(urls) != len(articles) or len(urls) != len(checks) or len(urls) != counts["selected"]:
        raise ValueError("Parent artifacts do not account for every selected URL")
    article_by_url = {row["url"]: row for row in articles}
    check_by_url = {row["url"]: row for row in checks}
    start = date.fromisoformat(manifest["requested_start"])
    end = date.fromisoformat(manifest["requested_end"])

    targets = []
    for row in urls:
        url = row["url"]
        article = article_by_url[url]
        check = check_by_url[url]
        parsed = datetime.fromisoformat(str(article.get("date_publish"))).date() if article.get("date_publish") else None
        if parsed and start <= parsed <= end and not check.get("eligible") and not check.get("excluded"):
            targets.append(url)
    repairs = []
    for url in targets:
        article = article_by_url[url]
        soup = BeautifulSoup(fetch(url).content, "html.parser")
        repaired = repair_policy_item(article, soup)
        if repaired is None:
            raise ValueError(f"In-window quarantine does not match a verified repair policy: {url}")
        repairs.append({"url": url, **repaired})

    new_articles, new_checks, issues = [], [], []
    new_counts = {"discovered": len(urls), "selected": len(urls), "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}
    unresolved_in_window = []
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
        "run_id": run_id, "started_at": now, "finished_at": now, "runtime_seconds": 0.0,
        "counts": new_counts, "derived_from_run_id": manifest["run_id"],
        "derivation": "verified_pakobserver_media_and_link_policy_repair", "repairs": repairs,
    })
    (run_dir / "manifest.json").write_text(json.dumps(child, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(
        f"# Pakistan Observer verified policy repair\n\n- Parent: {manifest['run_id']}\n"
        f"- Repaired exclusions: {len(repairs)}\n- Passed / skipped / quarantined: "
        f"{new_counts['passed']} / {new_counts['skipped']} / {new_counts['quarantined']}\n",
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
