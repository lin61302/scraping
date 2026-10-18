#!/usr/bin/env python3
"""Derive verified Malaysiakini API/body repairs and narrow exclusions.

The parent discovery set and successful rows remain immutable.  Only the 24
in-window quarantines are fetched again.  News Lab stories are recovered from
the publisher API embedded in the final page, two truncated pages use their
publisher JSON-LD articleBody, and true image/redirect/duplicate items receive
evidenced exclusions.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import sys
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.MYS.common import HEADERS, _jsonld_article, clean, fetch
from adapters.MYS.malaysiakini import scrape_url
from scraper_control.evidence import compare_required_evidence
from scraper_control.observe import adapter_bundle_sha256
from scraper_control.validation import resolve_policy_exclusion, resolve_with_page_evidence, validate_article


EXTERNAL_REDIRECT = (
    "https://www.malaysiakini.com/news/884389-international-press-freedom-award-2000"
)
DUPLICATE_REDIRECT = (
    "https://www.malaysiakini.com/news/884865-pasaraya-durjana-bagaimana-telegram-"
    "dijadikan-tempat-jaja-bahan-penderaan-seksual-kanak-kanak"
)
DUPLICATE_OF = "https://www.malaysiakini.com/news/781584"
JSONLD_REPAIRS = {
    "https://www.malaysiakini.com/news/780704",
    "https://www.malaysiakini.com/news/783069",
}
IMAGE_ONLY = {
    "https://www.malaysiakini.com/news/884386-top-employers-award-for-2024-2024",
    "https://www.malaysiakini.com/news/884387-50-most-influential-communicators-2001",
    "https://www.malaysiakini.com/news/884388-50-movers-and-shakers-of-the-new-economy-2000",
    "https://www.malaysiakini.com/news/783700",
}
GAP_START_ID = 782745
GAP_END_ID = 782783
GAP_IMAGE_ONLY = "https://www.malaysiakini.com/news/782745"


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def html_text(value: object) -> str | None:
    raw = clean(value)
    if not raw:
        return None
    return clean(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))


def embedded_api_document(soup: BeautifulSoup) -> tuple[str, dict]:
    seen = set()
    for tag in soup.select('script[type="application/json"][data-url*="/api/docs/"]'):
        endpoint = tag.get("data-url")
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        try:
            outer = json.loads(tag.string or tag.get_text() or "")
            payload = json.loads(outer["body"])
            data = payload["data"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return endpoint, data
    raise ValueError("News Lab page did not expose a usable publisher API document")


def article_segment(document: dict, canonical_url: str) -> dict:
    if isinstance(document.get("article"), list):
        return document
    part = re.search(r"/(part\d+)/?$", canonical_url)
    key = part.group(1) if part else None
    if key and isinstance(document.get(key), dict) and isinstance(document[key].get("article"), list):
        return document[key]
    candidates = [value for value in document.values() if isinstance(value, dict) and isinstance(value.get("article"), list)]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(f"Cannot select News Lab article segment for {canonical_url}")


def article_paragraphs(segment: dict) -> list[str]:
    result = []
    standfirst = html_text(segment.get("standfirst"))
    if standfirst:
        result.append(standfirst)
    for item in segment.get("article") or []:
        if not isinstance(item, dict):
            continue
        for key in ("heading", "header", "title", "quote", "text", "value", "caption"):
            value = item.get(key)
            if not isinstance(value, str):
                continue
            paragraph = html_text(value)
            if not paragraph or len(paragraph) < 10 or re.match(r"^https?://", paragraph):
                continue
            if paragraph not in result:
                result.append(paragraph)
    return result


def repair_newslab(article: dict, response, soup: BeautifulSoup) -> dict:
    canonical = soup.select_one('link[rel="canonical"][href]')
    canonical_url = canonical.get("href") if canonical else response.url
    if not str(canonical_url).startswith("https://newslab.malaysiakini.com/"):
        raise ValueError(f"Unexpected News Lab canonical URL: {canonical_url}")
    endpoint, document = embedded_api_document(soup)
    segment = article_segment(document, canonical_url)
    title = html_text(segment.get("title"))
    paragraphs = article_paragraphs(segment)
    body = "\n\n".join(paragraphs)
    if not title or len(body) < 150:
        raise ValueError(f"Incomplete News Lab API document for {article['url']}")
    article["title"] = title
    article["maintext"] = body
    article.setdefault("field_sources", {}).update({
        "title": "publisher_newslab_api.title",
        "maintext": "publisher_newslab_api.article",
    })
    evidence = article.setdefault("field_evidence", {})
    evidence["titles"] = [{"kind": "publisher_api_title", "source": endpoint, "raw": title}]
    evidence["bodies"] = [{"kind": "publisher_api_body", "source": endpoint, "text": body[:20000]}]
    evidence["publisher_api"] = {
        "endpoint": endpoint,
        "canonical_url": canonical_url,
        "article_items": len(segment.get("article") or []),
    }
    if article["url"] == DUPLICATE_REDIRECT:
        expected = "https://newslab.malaysiakini.com/ai-child-harm/regulation/ms/"
        if canonical_url != expected:
            raise ValueError(f"Duplicate redirect changed canonical: {canonical_url}")
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "duplicate_publisher_redirect"
        article["exclusion_evidence"] = {
            "policy_type": "publisher_duplicate_redirect",
            "canonical_url": canonical_url,
            "duplicate_of": DUPLICATE_OF,
            "reason": "publisher URL redirects to the same canonical article as an already observed URL",
        }
        return {"policy": "publisher_duplicate_redirect", "canonical_url": canonical_url, "duplicate_of": DUPLICATE_OF}
    return {"repair": "publisher_newslab_api", "endpoint": endpoint, "canonical_url": canonical_url, "characters": len(body)}


def repair_jsonld(article: dict, soup: BeautifulSoup) -> dict:
    structured = _jsonld_article(soup)
    body = clean(structured.get("articleBody")) if structured else None
    if not body or len(body) < 150 or len(body) <= len(article.get("maintext") or ""):
        raise ValueError(f"No longer JSON-LD body available for {article['url']}")
    article["maintext"] = body
    article.setdefault("field_sources", {})["maintext"] = "jsonld.articleBody"
    evidence = article.setdefault("field_evidence", {})
    evidence["bodies"] = [{"kind": "jsonld_articleBody", "source": "jsonld.articleBody", "text": body[:20000]}]
    return {"repair": "publisher_jsonld_article_body", "characters": len(body)}


def repair_image_only(article: dict, soup: BeautifulSoup) -> dict:
    title = clean(article.get("title"))
    image = None
    for candidate in soup.select("article.article-content div.image picture img[src]"):
        if clean(candidate.get("alt")) == title:
            image = candidate
            break
    if image is None:
        raise ValueError(f"No title-matched publisher image for {article['url']}")
    image_url = urljoin(article["url"], image.get("src"))
    image_evidence = {
        "url": image_url,
        "alt": clean(image.get("alt")),
        "selector": "article.article-content div.image picture img[src]",
    }
    evidence = article.setdefault("field_evidence", {})
    evidence["publisher_images"] = [image_evidence]
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_image_only_publisher_item"
    article["exclusion_evidence"] = {
        "policy_type": "publisher_image_only",
        "publisher_image_url": image_url,
        "publisher_dom_selector": image_evidence["selector"],
        "structured_article_body_empty": True,
    }
    return {"policy": "publisher_image_only", "image_url": image_url}


def repair_external_redirect(article: dict, response, soup: BeautifulSoup) -> dict:
    canonical = soup.select_one('link[rel="canonical"][href]')
    canonical_url = canonical.get("href") if canonical else None
    redirect_status = response.history[-1].status_code if response.history else None
    declared = {
        "publisher_url": article["url"],
        "final_url": response.url,
        "canonical_url": canonical_url,
        "redirect_status": redirect_status,
    }
    if declared != {
        "publisher_url": EXTERNAL_REDIRECT,
        "final_url": "https://cpj.org/awards/awards00/",
        "canonical_url": "https://cpj.org/awards/awards00/",
        "redirect_status": 301,
    }:
        raise ValueError(f"Unexpected external redirect evidence: {declared}")
    evidence = article.setdefault("field_evidence", {})
    evidence["publisher_redirects"] = [declared]
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_external_redirect"
    article["exclusion_evidence"] = {"policy_type": "publisher_external_redirect", **declared}
    return {"policy": "publisher_external_redirect", **declared}


def audit_numeric_gap(existing_urls: set[str]) -> tuple[dict, list[str]]:
    """Prove the Aug 22 low volume against the publisher's numeric ID space."""
    def probe(identifier: int) -> dict:
        url = f"https://www.malaysiakini.com/news/{identifier}"
        response = requests.get(url, headers=HEADERS, timeout=90)
        return {
            "id": identifier,
            "requested_url": url,
            "status": response.status_code,
            "final_url": response.url,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(probe, range(GAP_START_ID, GAP_END_ID + 1)))
    expected_news = {782745, 782770, 782782, 782783}
    expected_column = 782747
    for item in results:
        identifier = item["id"]
        if identifier in expected_news:
            valid = item["status"] == 200 and item["final_url"].startswith(
                f"https://www.malaysiakini.com/news/{identifier}"
            )
        elif identifier == expected_column:
            valid = item["status"] == 200 and item["final_url"].startswith(
                f"https://www.malaysiakini.com/columns/{identifier}"
            )
        else:
            valid = item["status"] == 404
        if not valid:
            raise ValueError(f"Malaysiakini numeric-gap audit changed: {item}")
    newly_discovered = [
        item["requested_url"] for item in results
        if item["id"] in expected_news and item["requested_url"] not in existing_urls
    ]
    if newly_discovered != [GAP_IMAGE_ONLY]:
        raise ValueError(f"Unexpected new URLs in numeric-gap audit: {newly_discovered}")
    evidence = {
        "reason": "explain 2026-08-22 volume-low anomaly and test sitemap exhaustiveness",
        "id_range": {"start": GAP_START_ID, "end": GAP_END_ID, "count": len(results)},
        "publisher_news_ids": sorted(expected_news),
        "publisher_non_news_ids": [expected_column],
        "publisher_missing_ids": [item["id"] for item in results if item["status"] == 404],
        "existing_news_urls": sorted(url for url in existing_urls if any(f"/{item}" in url for item in expected_news)),
        "newly_discovered_urls": newly_discovered,
        "coverage_complete": True,
        "results": results,
    }
    return evidence, newly_discovered


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
    if manifest.get("source") != "malaysiakini.com":
        raise ValueError("This derivation accepts only malaysiakini.com")
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
        if not check.get("eligible") and not check.get("excluded") and (
            (parsed and start <= parsed <= end) or url == EXTERNAL_REDIRECT
        ):
            targets.append(url)
    if len(targets) != 24:
        raise ValueError(f"Expected 24 in-window repair targets, found {len(targets)}")

    def repair_target(url: str) -> dict:
        article = article_by_url[url]
        response = fetch(url, timeout=90)
        soup = BeautifulSoup(response.content, "html.parser")
        if url == EXTERNAL_REDIRECT:
            repaired = repair_external_redirect(article, response, soup)
        elif url in IMAGE_ONLY:
            repaired = repair_image_only(article, soup)
        elif url in JSONLD_REPAIRS:
            repaired = repair_jsonld(article, soup)
        else:
            repaired = repair_newslab(article, response, soup)
        return {"url": url, **repaired}

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        repairs = list(executor.map(repair_target, targets))

    gap_audit, gap_urls = audit_numeric_gap(set(article_by_url))
    for url in gap_urls:
        article = scrape_url(url)
        response = fetch(url, timeout=90)
        soup = BeautifulSoup(response.content, "html.parser")
        repaired = repair_image_only(article, soup)
        urls.append({"url": url, "discovery_method": "publisher_numeric_id_gap_audit"})
        article_by_url[url] = article
        repairs.append({"url": url, **repaired, "discovered_by": "publisher_numeric_id_gap_audit"})

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
        if bucket == "quarantined" and (parsed is None or start <= parsed <= end):
            unresolved_in_window.append(article["url"])
    if unresolved_in_window:
        raise ValueError(f"Derived observation retains in-window quarantines: {unresolved_in_window}")

    run_id = f"observe-derived-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    write_jsonl(run_dir / "urls.jsonl", urls)
    write_jsonl(run_dir / "articles.jsonl", new_articles)
    write_jsonl(run_dir / "field_checks.jsonl", new_checks)
    write_jsonl(run_dir / "issues.jsonl", issues)
    (run_dir / "errors.jsonl").write_text("", encoding="utf-8")
    now = datetime.now(timezone.utc).isoformat()
    child = dict(manifest)
    child.update({
        "run_id": run_id, "started_at": now, "finished_at": now, "runtime_seconds": 0.0,
        "counts": new_counts, "derived_from_run_id": manifest["run_id"],
        "derivation": "verified_malaysiakini_api_body_and_policy_repairs", "repairs": repairs,
    })
    child.setdefault("discovery_evidence", {})["numeric_id_gap_audit"] = gap_audit
    child["discovery_evidence"]["discovered_urls_after_filter"] = len(urls)
    (run_dir / "manifest.json").write_text(json.dumps(child, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(
        f"# Malaysiakini verified repairs\n\n- Parent: {manifest['run_id']}\n"
        f"- Repairs / exclusions: {len(repairs)}\n- Passed / skipped / quarantined: "
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
