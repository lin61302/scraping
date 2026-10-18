#!/usr/bin/env python3
"""Derive Utusan body-selector repairs and verified image-only exclusions."""

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
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.MYS.common import _jsonld_article, clean, fetch
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


def widget_parts(widget) -> list[str]:
    container = widget.select_one(".elementor-widget-container") or widget
    clone = BeautifulSoup(str(container), "html.parser")
    for bad in clone.select("script, style, iframe, form, .ad, .ads, .related, [class*='related']"):
        bad.decompose()
    root = clone.select_one(".elementor-widget-container") or clone
    parts: list[str] = []
    for node in root.find_all(recursive=False):
        value = clean(node.get_text(" ", strip=True))
        if value and len(value) >= 20 and value not in parts:
            parts.append(value)
    if not parts:
        for node in root.select("p, h2, h3, div"):
            value = clean(node.get_text(" ", strip=True))
            if value and len(value) >= 20 and value not in parts:
                parts.append(value)
    return parts


def wordpress_rest_repair(article: dict) -> dict:
    """Recover an exact post when Utusan's HTML cache serves only a PHP warning."""
    parsed = urlparse(article["url"])
    slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    endpoint = (
        "https://www.utusan.com.my/wp-json/wp/v2/posts"
        f"?slug={quote(slug)}&_embed=1"
    )
    response = fetch(endpoint)
    payload = response.json()
    if not isinstance(payload, list) or len(payload) != 1:
        raise ValueError(f"Utusan REST lookup did not return one exact post for {article['url']}")
    post = payload[0]
    canonical = str(post.get("link") or "").rstrip("/")
    if not (
        post.get("status") == "publish"
        and post.get("slug") == slug
        and canonical == article["url"].rstrip("/")
    ):
        raise ValueError(f"Utusan REST identity evidence disagrees for {article['url']}")
    rendered_title = ((post.get("title") or {}).get("rendered") or "")
    title = clean(html.unescape(BeautifulSoup(rendered_title, "html.parser").get_text(" ", strip=True)))
    rendered_body = ((post.get("content") or {}).get("rendered") or "")
    body_soup = BeautifulSoup(rendered_body, "html.parser")
    for bad in body_soup.select("script, style, iframe, form, .ad, .ads, .related, [class*='related']"):
        bad.decompose()
    parts = []
    for node in body_soup.select("p, h2, h3"):
        value = clean(node.get_text(" ", strip=True))
        if value and len(value) >= 20 and value not in parts:
            parts.append(value)
    body = "\n\n".join(parts) or clean(body_soup.get_text(" ", strip=True))
    raw_date = str(post.get("date_gmt") or "")
    try:
        published = datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Utusan REST publication date is invalid for {article['url']}") from exc
    if not title or not body:
        raise ValueError(f"Utusan REST required fields are empty for {article['url']}")
    article.update({"title": title, "maintext": body, "date_publish": published})
    article.setdefault("field_sources", {}).update({
        "title": "wordpress_rest.title.rendered",
        "date_publish": "wordpress_rest.date_gmt",
        "maintext": "wordpress_rest.content.rendered",
    })
    evidence = article.setdefault("field_evidence", {})
    evidence.update({
        "titles": [{"kind": "wordpress_rest_title", "source": endpoint, "raw": title}],
        "dates": [{"kind": "published", "source": "wordpress_rest.date_gmt", "raw": raw_date + "Z"}],
        "bodies": [{"kind": "wordpress_rest_content", "source": endpoint, "text": body[:20000]}],
        "http_status": response.status_code,
        "publisher_api_records": [{
            "kind": "wordpress_rest_exact_slug", "endpoint": endpoint,
            "post_id": post.get("id"), "slug": slug, "status": post.get("status"),
            "canonical_url": post.get("link"),
        }],
    })
    return {
        "repair": "publisher_wordpress_rest_exact_slug",
        "post_id": post.get("id"), "characters": len(body),
        "words": len(re.findall(r"\b\w+\b", body, re.UNICODE)),
    }


def repair_failure(article: dict) -> dict | None:
    check = validate_article(article, date(1900, 1, 1), date(2100, 1, 1), article.get("source_domain"))
    body_check = next(item for item in check.fields if item.field == "maintext")
    if body_check.status == "pass":
        return None
    response = fetch(article["url"])
    soup = BeautifulSoup(response.content, "html.parser")
    widget = soup.select_one(".elementor-widget-theme-post-content")
    if widget is None:
        return wordpress_rest_repair(article)
    structured = _jsonld_article(soup) or {}
    word_count = int(structured.get("wordCount") or 0)
    parts = widget_parts(widget)
    body = "\n\n".join(parts)
    words = re.findall(r"\b\w+\b", body, re.UNICODE)
    # Preserve the exact article widget whenever the publisher independently
    # declares a nonzero article word count. The evidence validator decides
    # whether a brief is substantive enough for promotion.
    if words and word_count > 0:
        article["maintext"] = body
        article.setdefault("field_sources", {})["maintext"] = ".elementor-widget-theme-post-content raw text"
        evidence = article.setdefault("field_evidence", {})
        evidence["bodies"] = [{"kind": "utusan_article_widget", "source": ".elementor-widget-theme-post-content", "text": body[:20000]}]
        evidence["http_status"] = response.status_code
        return {"repair": "publisher_article_widget_text", "characters": len(body), "words": len(words), "jsonld_word_count": word_count}
    images = widget.select("img[src]")
    if not body and images and 0 < word_count <= 12:
        image_url = urljoin(article["url"], images[0].get("src"))
        evidence = article.setdefault("field_evidence", {})
        evidence["publisher_images"] = [{"url": image_url, "alt": clean(images[0].get("alt")), "selector": ".elementor-widget-theme-post-content img[src]"}]
        evidence["http_status"] = response.status_code
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_image_only_publisher_item"
        article["exclusion_evidence"] = {
            "policy_type": "publisher_image_only",
            "publisher_dom_selector": ".elementor-widget-theme-post-content img[src]",
            "publisher_image_url": image_url,
            "structured_article_body_empty": True,
            "jsonld_word_count": word_count,
        }
        return {"policy": "publisher_image_only", "image_url": image_url, "jsonld_word_count": word_count}
    raise ValueError(f"Unclassified Utusan maintext failure for {article['url']}: words={len(words)} jsonld={word_count} images={len(images)}")


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
            article = {
                "url": url, "source_domain": manifest["source"], "country_code": manifest["country"],
                "title": None, "date_publish": None, "maintext": None, "language": "ms",
                "parser": "pure_adapter_derived_recovery", "collection": None, "parser_error": None,
                "field_sources": {}, "field_evidence": {},
            }
            exception_repair = wordpress_rest_repair(article)
            repairs.append({"url": url, "recovered_parent_exception": True, **exception_repair})
        repair = repair_failure(article)
        if repair:
            repairs.append({"url": article["url"], **repair})
        check, issues, bucket = validate_one(article, manifest["source"], start, end)
        new_counts[bucket] += 1
        new_articles.append(article); new_checks.append(check); new_issues.extend(issues)
    if not repairs:
        raise ValueError("No Utusan repairs were applied")
    run_id = f"observe-derived-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(parent / "urls.jsonl", run_dir / "urls.jsonl")
    write_jsonl(run_dir / "articles.jsonl", new_articles)
    write_jsonl(run_dir / "field_checks.jsonl", new_checks)
    write_jsonl(run_dir / "issues.jsonl", new_issues)
    now = datetime.now(timezone.utc).isoformat()
    child = dict(manifest)
    child.update({"run_id": run_id, "started_at": now, "finished_at": now, "runtime_seconds": 0.0, "counts": new_counts, "derived_from_run_id": manifest["run_id"], "derivation": "repair_utusan_article_widgets_and_exclude_verified_image_only_items", "repairs": repairs})
    (run_dir / "manifest.json").write_text(json.dumps(child, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(f"# Derived Utusan repairs\n\n- Parent: `{manifest['run_id']}`\n- Repairs: {len(repairs)}\n- Passed / skipped / quarantined: {new_counts['passed']} / {new_counts['skipped']} / {new_counts['quarantined']}\n", encoding="utf-8")
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    print(derive(args.parent_run, args.output_root))


if __name__ == "__main__":
    main()
