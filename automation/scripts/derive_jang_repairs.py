#!/usr/bin/env python3
"""Derive Jang alternate-template repairs and verified empty numeric slots."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.PAK.common import HEADERS, _body, clean, fetch, parse_datetime
from adapters.PAK.jang import scrape_url
from scraper_control.evidence import compare_required_evidence
from scraper_control.observe import adapter_bundle_sha256
from scraper_control.validation import resolve_policy_exclusion, resolve_with_page_evidence, validate_article


EMPTY_ERROR = "ValueError: empty response; ValueError: empty response; ValueError: empty response"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _jang_body(soup: BeautifulSoup) -> tuple[str, str]:
    for selector in (".detail_view_content", ".description-area"):
        node = soup.select_one(selector)
        if not node:
            continue
        direct = [clean(item.get_text(" ", strip=True)) for item in node.find_all("p", recursive=False)]
        paragraphs = [item for item in direct if item]
        if paragraphs:
            return "\n\n".join(paragraphs), selector
    return _body(soup, (".detail_view_content", ".description-area"))


def _refresh_page_fields(article: dict, soup: BeautifulSoup) -> dict:
    repairs: dict[str, object] = {}
    evidence = article.setdefault("field_evidence", {})
    evidence["http_status"] = 200

    title = clean(article.get("title"))
    if title:
        evidence["titles"] = [{"kind": "visible_h1", "source": "h1", "raw": title}]

    if not article.get("date_publish"):
        date_raw = date_source = None
        for node in soup.select(".detail-w-t > .detail-time"):
            raw = clean(node.get_text(" ", strip=True))
            if raw and parse_datetime(raw):
                date_raw, date_source = raw, ".detail-w-t > .detail-time"
                break
        if not date_raw:
            raw_html = str(soup)
            date_match = re.search(r"['\"]publish_date['\"]\s*:\s*['\"]([^'\"]+)", raw_html, re.I)
            time_match = re.search(r"['\"]publish_time['\"]\s*:\s*['\"]([^'\"]+)", raw_html, re.I)
            if date_match:
                date_raw = date_match.group(1)
                if time_match:
                    date_raw = f"{date_raw} {time_match.group(1)}"
                date_source = "publisher analytics publish_date/publish_time"
        parsed = parse_datetime(date_raw)
        if parsed:
            article["date_publish"] = parsed
            article.setdefault("field_sources", {})["date_publish"] = date_source
            evidence["dates"] = [{"kind": "published", "source": date_source, "raw": date_raw}]
            repairs["date_publish"] = parsed.isoformat()

    body, body_source = _jang_body(soup)
    old_body = clean(article.get("maintext")) or ""
    if body and (not old_body or len(body) > len(old_body)):
        article["maintext"] = body
        article.setdefault("field_sources", {})["maintext"] = body_source
        repairs["maintext_characters"] = len(body)
    body = clean(article.get("maintext"))
    if body:
        evidence["bodies"] = [{"kind": body_source or "selected_body", "text": body[:20000]}]
    return repairs


def _empty_slot_article(url: str, empty_observations: int) -> dict:
    slot = {
        "publisher_url": url,
        "http_status": 200,
        "response_bytes": 0,
        "empty_observations": empty_observations,
    }
    return {
        "url": url,
        "source_domain": "jang.com.pk",
        "country_code": "PAK",
        "title": None,
        "date_publish": None,
        "maintext": None,
        "language": "ur",
        "parser": "pure_adapter",
        "collection": "SKIP",
        "parser_error": None,
        "field_sources": {"title": None, "date_publish": None, "maintext": None},
        "field_evidence": {
            "titles": [], "dates": [], "bodies": [], "http_status": 200,
            "publisher_empty_numeric_slots": [slot],
        },
        "exclusion_reason": "publisher_unallocated_numeric_slot",
        "exclusion_evidence": {"policy_type": "publisher_unallocated_numeric_slot", **slot},
    }


def _probe_error(url: str, parent_empty_observations: int) -> tuple[str, str, int]:
    required = max(1, 4 - parent_empty_observations)
    empty_observations = parent_empty_observations
    for _ in range(required):
        response = None
        last_error = None
        for _attempt in range(3):
            try:
                response = requests.get(url, headers=HEADERS, timeout=45)
                break
            except requests.RequestException as exc:
                last_error = exc
        if response is None:
            raise RuntimeError(f"three reprobe attempts failed: {last_error}")
        if response.status_code != 200:
            response.raise_for_status()
        if response.content:
            return url, "available", empty_observations
        empty_observations += 1
    return url, "empty", empty_observations


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


def derive(parent: Path, output_root: Path, workers: int) -> Path:
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    counts = manifest.get("counts") or {}
    if manifest.get("source") != "jang.com.pk":
        raise ValueError("This derivation accepts only jang.com.pk")
    if manifest.get("status") != "observed" or counts.get("selected") != counts.get("discovered"):
        raise ValueError("Parent must be a completed full observation")
    source_file = Path(manifest["source_file"])
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != manifest.get("source_sha256"):
        raise ValueError("Adapter changed after the parent observation")
    if adapter_bundle_sha256(source_file) != manifest.get("source_bundle_sha256"):
        raise ValueError("Adapter bundle changed after the parent observation")

    urls = read_jsonl(parent / "urls.jsonl")
    articles = read_jsonl(parent / "articles.jsonl")
    old_checks = read_jsonl(parent / "field_checks.jsonl")
    errors = read_jsonl(parent / "errors.jsonl")
    if len(urls) != counts["selected"] or len(articles) != len(old_checks):
        raise ValueError("Parent artifacts are internally inconsistent")
    if len(articles) + len(errors) != len(urls):
        raise ValueError("Parent articles and exceptions do not account for every URL")
    error_by_url = {row["url"]: row for row in errors}
    for row in errors:
        if row.get("type") != "RuntimeError" or not str(row.get("message", "")).startswith(f"fetch failed for {row.get('url')}:"):
            raise ValueError(f"Unexpected Jang exception: {row}")

    probes: dict[str, tuple[str, int]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_probe_error, url, str(row.get("message", "")).count("ValueError: empty response")): url
            for url, row in error_by_url.items()
        }
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                _, result, observations = future.result()
            except Exception as exc:
                raise RuntimeError(f"Jang exception reprobe failed for {url}: {exc}") from exc
            probes[url] = (result, observations)

    article_by_url = {row["url"]: row for row in articles}
    check_by_url = {row["url"]: row for row in old_checks}
    for url in error_by_url:
        result, observations = probes[url]
        article_by_url[url] = _empty_slot_article(url, observations) if result == "empty" else scrape_url(url)

    start = date.fromisoformat(manifest["requested_start"])
    end = date.fromisoformat(manifest["requested_end"])
    repairs: list[dict] = []
    for row in urls:
        url = row["url"]
        if url in error_by_url and probes[url][0] == "empty":
            continue
        article = article_by_url[url]
        recorded = check_by_url.get(url, {})
        parsed = datetime.fromisoformat(str(article.get("date_publish"))).date() if article.get("date_publish") else None
        should_refresh = bool(
            not recorded.get("eligible") and not recorded.get("excluded")
            and (parsed is None or start <= parsed <= end)
        )
        if not should_refresh:
            continue
        soup = BeautifulSoup(fetch(url).content, "html.parser")
        changed = _refresh_page_fields(article, soup)
        if changed:
            repairs.append({"url": url, **changed})

    new_articles: list[dict] = []
    new_checks: list[dict] = []
    issues: list[dict] = []
    new_counts = {"discovered": len(urls), "selected": len(urls), "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}
    unresolved: list[dict] = []
    for row in urls:
        article = article_by_url[row["url"]]
        check, row_issues, bucket = validate_one(article, source=manifest["source"], start=start, end=end)
        new_articles.append(article)
        new_checks.append(check)
        issues.extend(row_issues)
        new_counts[bucket] += 1
        parsed = datetime.fromisoformat(str(article.get("date_publish"))).date() if article.get("date_publish") else None
        if bucket == "quarantined" and (parsed is None or start <= parsed <= end):
            unresolved.append({"url": article["url"], "date_publish": str(article.get("date_publish"))})
    if unresolved:
        raise ValueError(f"Derived observation retains unresolved current items: {unresolved[:10]}")

    run_id = f"observe-derived-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(parent / "urls.jsonl", run_dir / "urls.jsonl")
    write_jsonl(run_dir / "articles.jsonl", new_articles)
    write_jsonl(run_dir / "field_checks.jsonl", new_checks)
    write_jsonl(run_dir / "issues.jsonl", issues)
    write_jsonl(run_dir / "errors.jsonl", [])
    now = datetime.now(timezone.utc).isoformat()
    child = dict(manifest)
    child.update({
        "run_id": run_id, "started_at": now, "finished_at": now, "runtime_seconds": 0.0,
        "counts": new_counts, "derived_from_run_id": manifest["run_id"],
        "derivation": "jang_alternate_template_and_empty_numeric_slot_repairs",
        "repairs": repairs,
        "verified_unallocated_numeric_slots": sum(result == "empty" for result, _ in probes.values()),
        "recovered_transient_fetch_failures": sum(result == "available" for result, _ in probes.values()),
    })
    (run_dir / "manifest.json").write_text(json.dumps(child, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(
        "# Jang derived repair\n\n"
        f"- Parent: `{manifest['run_id']}`\n"
        f"- Alternate-template repairs: {len(repairs)}\n"
        f"- Repeated empty numeric slots: {sum(result == 'empty' for result, _ in probes.values())}\n"
        f"- Recovered transient fetch failures: {sum(result == 'available' for result, _ in probes.values())}\n"
        f"- Passed / skipped / quarantined: {new_counts['passed']} / {new_counts['skipped']} / {new_counts['quarantined']}\n",
        encoding="utf-8",
    )
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=24)
    args = parser.parse_args()
    print(derive(args.parent_run, args.output_root, args.workers))


if __name__ == "__main__":
    main()
