#!/usr/bin/env python3
"""Derive narrow NST title repairs and publisher-empty exclusions."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.MYS import common as mys_common
from adapters.MYS.common import clean, extract_article, fetch
from scraper_control.evidence import compare_required_evidence
from scraper_control.observe import adapter_bundle_sha256
from scraper_control.validation import resolve_policy_exclusion, resolve_with_page_evidence, validate_article


COPYRIGHT_BODY = "© New Straits Times Press (M) Bhd"


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def normalize(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def article_payload(soup: BeautifulSoup) -> dict:
    node = soup.find("article-component")
    raw = node.get(":article") if node else None
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        raise ValueError("NST page has no usable embedded article payload")
    return payload


def recover_false_challenge(url: str) -> dict:
    """Parse a real NST article whose headline happens to mention Cloudflare."""
    def raw_fetch(target: str, timeout: int = 45) -> requests.Response:
        response = requests.get(target, headers=mys_common.HEADERS, timeout=timeout)
        response.raise_for_status()
        if not response.content:
            raise ValueError(f"empty response for {target}")
        return response

    original_fetch = mys_common.fetch
    mys_common.fetch = raw_fetch
    try:
        article = extract_article(
            url,
            source="nst.com.my",
            language="en",
            title_selectors=("h1.article-title", "article h1", "main h1", "h1"),
            body_selectors=("[itemprop='articleBody']", ".article-content", ".field-name-body", "article"),
            visible_date_selectors=("[itemprop='datePublished']", "article time", "time"),
            evidence_body_selectors=("[itemprop='articleBody']", ".article-content", "article", "main"),
            embedded_body=("article-component", ":article", "body"),
        )
    finally:
        mys_common.fetch = original_fetch
    title = normalize(article.get("title"))
    if "cloudflare" not in title.casefold():
        raise ValueError(f"NST false-challenge recovery title changed for {url}")
    article.setdefault("field_evidence", {}).setdefault("publisher_response_recoveries", []).append({
        "kind": "literal_cloudflare_headline_false_positive",
        "url": url,
        "http_status": article.get("field_evidence", {}).get("http_status"),
        "title": title,
    })
    article["parser"] = "pure_adapter_derived_recovery"
    return article


def repair_title(article: dict) -> dict | None:
    if normalize(article.get("title")):
        return None
    evidence = article.get("field_evidence") or {}
    for signal in evidence.get("titles") or []:
        raw = html.unescape(normalize(signal.get("raw"))) if isinstance(signal, dict) else ""
        title = re.sub(r"\s*\|\s*New Straits Times\s*$", "", raw, flags=re.I).strip()
        if len(title) >= 8:
            article["title"] = title
            article.setdefault("field_sources", {})["title"] = f"recovered:{signal.get('kind', 'publisher_title')}"
            evidence.setdefault("titles", []).append({
                "kind": "recovered_publisher_title",
                "source": signal.get("source") or signal.get("kind"),
                "raw": title,
            })
            return {"repair": "publisher_title_signal", "title": title}
    raise ValueError(f"No publisher title signal for {article['url']}")


def repair_maintext_failure(article: dict) -> dict | None:
    check = validate_article(article, date(1900, 1, 1), date(2100, 1, 1), article.get("source_domain"))
    body_check = next(item for item in check.fields if item.field == "maintext")
    if body_check.status == "pass":
        return None
    if normalize(article.get("maintext")) != COPYRIGHT_BODY:
        return None
    response = fetch(article["url"])
    soup = BeautifulSoup(response.content, "html.parser")
    payload = article_payload(soup)
    body_soup = BeautifulSoup(str(payload.get("body") or ""), "html.parser")
    inline_soup = BeautifulSoup(str(payload.get("body_with_inline") or ""), "html.parser")
    body_text = normalize(body_soup.get_text(" ", strip=True))
    inline_text = normalize(inline_soup.get_text(" ", strip=True))
    match = re.search(r"/20\d{2}/\d{2}/(\d+)/", article["url"])
    nid = int(match.group(1)) if match else None
    identity_matches = bool(
        payload.get("nid") == nid
        and payload.get("status") == 1
        and html.unescape(normalize(payload.get("title"))) == html.unescape(normalize(article.get("title")))
    )
    if not identity_matches:
        raise ValueError(f"NST publisher-payload identity changed for {article['url']}")
    word_count = int(payload.get("word_count") or 0)
    if word_count == 0 and body_text == COPYRIGHT_BODY and not inline_text:
        record = {
            "publisher_url": article["url"],
            "embedded_nid": nid,
            "embedded_status": payload.get("status"),
            "embedded_word_count": word_count,
            "embedded_body_text": body_text,
            "embedded_inline_body_empty": True,
        }
        evidence = article.setdefault("field_evidence", {})
        evidence["publisher_empty_articles"] = [record]
        evidence["http_status"] = response.status_code
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_publisher_empty_article"
        article["exclusion_evidence"] = {"policy_type": "publisher_empty_article", **record}
        return {"policy": "publisher_empty_article", **record}

    images = [node.get("src") for node in inline_soup.select("img[src]") if node.get("src")]
    body_without_ids = normalize(re.sub(r"\[\[gn4id:\d+\]\]", "", body_text or ""))
    if word_count <= 5 and images and not inline_text and body_without_ids == COPYRIGHT_BODY:
        evidence = article.setdefault("field_evidence", {})
        evidence["publisher_images"] = [
            {"url": url, "selector": "article-component[:article].body_with_inline img[src]"}
            for url in images
        ]
        evidence["http_status"] = response.status_code
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_image_only_publisher_item"
        article["exclusion_evidence"] = {
            "policy_type": "publisher_image_only",
            "publisher_dom_selector": "article-component[:article].body_with_inline img[src]",
            "publisher_image_url": images[0],
            "structured_article_body_empty": True,
            "embedded_nid": nid,
            "embedded_word_count": word_count,
        }
        return {"policy": "publisher_image_only", "image_url": images[0], "embedded_word_count": word_count}

    candidates = [
        normalize(re.sub(r"\[\[gn4id:\d+\]\]", "", value or ""))
        for value in (body_text, inline_text)
    ]
    candidate = max(candidates, key=lambda value: len(re.findall(r"\b\w+\b", value or "")), default=None)
    candidate_words = len(re.findall(r"\b\w+\b", candidate or "", re.UNICODE))
    if candidate and candidate_words >= 10 and word_count >= 10:
        article["maintext"] = candidate
        article.setdefault("field_sources", {})["maintext"] = "article-component[:article] current publisher payload"
        evidence = article.setdefault("field_evidence", {})
        evidence["bodies"] = [{
            "kind": "article_component_current_payload",
            "source": "article-component[:article]",
            "text": candidate[:20000],
        }]
        evidence["http_status"] = response.status_code
        evidence["publisher_payload_repairs"] = [{
            "embedded_nid": nid, "embedded_status": payload.get("status"),
            "embedded_word_count": word_count, "recovered_words": candidate_words,
        }]
        return {
            "repair": "publisher_current_embedded_body",
            "embedded_nid": nid, "embedded_word_count": word_count,
            "recovered_words": candidate_words,
        }
    raise ValueError(
        f"Unclassified NST maintext failure for {article['url']}: "
        f"embedded_words={word_count} recovered_words={candidate_words} images={len(images)}"
    )


def validate_one(article: dict, source: str, start: date, end: date) -> tuple[dict, list[dict], str]:
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
    issues = []
    if excluded:
        bucket = "skipped"
        issues.append({"url": article["url"], "source": source, "stage": "source_policy", "category": "collection", "code": article.get("exclusion_reason"), "severity": "info", "status": "excluded", "observed": article.get("exclusion_evidence"), "attempt": 1})
    elif eligible:
        bucket = "passed"
        for resolution in resolutions:
            issues.append({"url": article["url"], "source": source, "stage": "validation", "category": resolution["field"], "code": resolution["code"], "severity": "info", "status": "resolved", "observed": resolution, "attempt": 1})
    else:
        bucket = "quarantined"
        for item in [*base.fields, *evidence_checks]:
            for code in item.codes:
                issues.append({"url": article["url"], "source": source, "stage": "derived_validation", "category": item.field, "code": code, "severity": "error", "status": "open", "observed": item.evidence, "attempt": 1})
    return check, issues, bucket


def derive(parent: Path, output_root: Path) -> Path:
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    counts = manifest.get("counts") or {}
    if manifest.get("status") != "observed" or counts.get("selected") != counts.get("discovered"):
        raise ValueError("Parent must be a complete full observation")
    source_file = Path(manifest["source_file"])
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != manifest.get("source_sha256"):
        raise ValueError("Adapter changed after the parent observation")
    if adapter_bundle_sha256(source_file) != manifest.get("source_bundle_sha256"):
        raise ValueError("Adapter bundle changed after the parent observation")
    urls = read_jsonl(parent / "urls.jsonl")
    articles = {row["url"]: row for row in read_jsonl(parent / "articles.jsonl")}
    if len(urls) != counts["selected"]:
        raise ValueError("Parent URL artifact does not account for every selected URL")
    missing_urls = {row["url"] for row in urls} - set(articles)
    exception_urls = {
        row["url"] for row in read_jsonl(parent / "issues.jsonl")
        if row.get("stage") == "parse" and row.get("code") == "source_exception" and row.get("status") == "open"
    }
    if missing_urls != exception_urls or len(missing_urls) != int(counts.get("exceptions") or 0):
        raise ValueError("Parent parse-exception artifacts are inconsistent")
    start, end = date.fromisoformat(manifest["requested_start"]), date.fromisoformat(manifest["requested_end"])
    new_articles, new_checks, new_issues, repairs = [], [], [], []
    new_counts = {"discovered": len(urls), "selected": len(urls), "passed": 0, "quarantined": 0, "skipped": 0, "exceptions": 0}
    for url_row in urls:
        url = url_row["url"]
        if url in articles:
            article = dict(articles[url])
        else:
            article = recover_false_challenge(url)
            repairs.append({"url": url, "repair": "literal_cloudflare_headline_false_positive"})
        title_repair = repair_title(article)
        if title_repair:
            repairs.append({"url": article["url"], **title_repair})
        body_repair = repair_maintext_failure(article)
        if body_repair:
            repairs.append({"url": article["url"], **body_repair})
        check, issues, bucket = validate_one(article, manifest["source"], start, end)
        new_counts[bucket] += 1
        new_articles.append(article); new_checks.append(check); new_issues.extend(issues)
    if not repairs:
        raise ValueError("No NST repairs were applied")
    run_id = f"observe-derived-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(parent / "urls.jsonl", run_dir / "urls.jsonl")
    write_jsonl(run_dir / "articles.jsonl", new_articles)
    write_jsonl(run_dir / "field_checks.jsonl", new_checks)
    write_jsonl(run_dir / "issues.jsonl", new_issues)
    now = datetime.now(timezone.utc).isoformat()
    child = dict(manifest)
    child.update({"run_id": run_id, "started_at": now, "finished_at": now, "runtime_seconds": 0.0, "counts": new_counts, "derived_from_run_id": manifest["run_id"], "derivation": "repair_nst_publisher_titles_and_exclude_verified_empty_records", "repairs": repairs})
    (run_dir / "manifest.json").write_text(json.dumps(child, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(f"# Derived NST repairs\n\n- Parent: `{manifest['run_id']}`\n- Repairs: {len(repairs)}\n- Passed / skipped / quarantined: {new_counts['passed']} / {new_counts['skipped']} / {new_counts['quarantined']}\n", encoding="utf-8")
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    print(derive(args.parent_run, args.output_root))


if __name__ == "__main__":
    main()
