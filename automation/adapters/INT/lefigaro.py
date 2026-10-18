"""Inventory-only, network-only adapter for Le Figaro daily sitemaps.

The historical updater mixed daily sitemap discovery with direct MongoDB
mutations.  This module keeps only the publisher sitemap inventory.  It does
not expose an article scraper: ``scrape_url`` fails closed, and article pages
are fetched only through the explicitly bounded ``sample_article_pages``
function or the CLI ``--sample-count`` option.

Sitemap file dates, URL suffix dates, and sitemap ``lastmod`` values are
inventory evidence.  None is authoritative for publication coverage.  A
future full observation must obtain the publication timestamp from the
publisher article page after the access and content-class policies have been
approved.

This module has no database imports, credentials, cache mutation, output-file
writer, or production-write path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from html import unescape
from typing import Iterable
from urllib.parse import urlparse, urlunparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


class Config:
    source_name = "lefigaro.fr"
    country_code = "INT"
    is_env = False
    inventory_only = True
    access_block_circuit_threshold = 1


FIRST_PASS_START = date(2026, 8, 1)
FIRST_PASS_END = date(2026, 9, 30)
SITEMAP_TEMPLATE = "https://sitemaps.lefigaro.fr/lefigaro.fr/articles/{day}.xml"
MAX_INVENTORY_DAYS = 62
MAX_SITEMAP_BYTES = 2_000_000
MAX_SAMPLE_PAGES = 12
MAX_SAMPLE_BYTES = 3_000_000
USER_AGENT = "mlp-mleed-source-inventory/1.0"
DATE_SUFFIX_RE = re.compile(r"-(20\d{6})(?:$|/)")
MEDIA_RE = re.compile(r"\.(?:avif|gif|jpe?g|mp[34]|pdf|png|svg|webp)$", re.I)

# The current publisher robots notice says automated browsing/navigation is
# not authorized.  The sitemap inventory route is prepared locally, but page
# samples require a deliberate acknowledgement and remain capped.
ARTICLE_PAGE_ACCESS_REQUIRES_APPROVAL = True
INVENTORY_ONLY = True

_DISCOVERY_ROWS: dict[str, dict] = {}


def _request(url: str, *, timeout: int = 30) -> requests.Response:
    response = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/xml,text/html;q=0.9,*/*;q=0.1",
        },
    )
    response.raise_for_status()
    if not response.content:
        raise RuntimeError("Le Figaro returned an empty response")
    return response


def _days(start: date, end: date) -> list[date]:
    if start > end:
        raise ValueError("Le Figaro inventory start must not follow end")
    length = (end - start).days + 1
    if not 1 <= length <= MAX_INVENTORY_DAYS:
        raise ValueError("Le Figaro inventory must contain 1 to 62 days")
    return [start + timedelta(days=offset) for offset in range(length)]


def _publisher_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == Config.source_name or host.endswith("." + Config.source_name)


def _canonicalize_url(raw: str) -> str | None:
    """Return a conservative publisher URL without query/fragment state."""
    try:
        parsed = urlparse(unescape(raw.strip()))
    except Exception:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not _publisher_host(host)
        or host == "sitemaps.lefigaro.fr"
        or parsed.username
        or parsed.password
        or not parsed.path
        or parsed.path == "/"
        or MEDIA_RE.search(parsed.path)
    ):
        return None
    path = re.sub(r"/{2,}", "/", parsed.path)
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def _content_class(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "video.lefigaro.fr":
        return "video"
    if host in {Config.source_name, "www.lefigaro.fr"}:
        return "story" if parsed.path.startswith("/story/") else "www_article"
    return "other_publisher_subdomain"


def _url_suffix_day(url: str) -> tuple[str | None, str]:
    match = DATE_SUFFIX_RE.search(urlparse(url).path)
    if not match:
        return None, "absent"
    raw = match.group(1)
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return raw, "invalid"
    return parsed.isoformat(), "valid"


def _url_set_sha256(urls: Iterable[str]) -> str:
    payload = "".join("%s\n" % item for item in sorted(set(urls)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_sitemap(content: bytes, *, sitemap_day: date) -> list[dict]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise RuntimeError("Le Figaro returned malformed sitemap XML") from exc
    if _local_name(root.tag) != "urlset":
        raise RuntimeError("Le Figaro daily sitemap root is not urlset")

    rows = []
    for position, node in enumerate(list(root), start=1):
        if _local_name(node.tag) != "url":
            continue
        loc = None
        lastmod = None
        for child in list(node):
            name = _local_name(child.tag)
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod" and child.text:
                lastmod = child.text.strip()
        if not loc:
            raise RuntimeError("Le Figaro daily sitemap contains a URL row without loc")
        canonical = _canonicalize_url(loc)
        if not canonical:
            raise RuntimeError("Le Figaro daily sitemap contains a non-publisher URL: %s" % loc)
        suffix_day, suffix_state = _url_suffix_day(canonical)
        rows.append({
            "url": canonical,
            "sitemap_day": sitemap_day.isoformat(),
            "sitemap_position": position,
            "sitemap_lastmod": lastmod,
            "sitemap_lastmod_authoritative": False,
            "content_class": _content_class(canonical),
            "url_suffix_day": suffix_day,
            "url_suffix_state": suffix_state,
            "url_suffix_day_authoritative": False,
        })
    if not rows:
        raise RuntimeError("Le Figaro daily sitemap is empty")
    return rows


def _read_day(sitemap_day: date) -> tuple[list[dict], dict]:
    url = SITEMAP_TEMPLATE.format(day=sitemap_day.isoformat())
    response = _request(url)
    content = response.content
    if len(content) > MAX_SITEMAP_BYTES:
        raise RuntimeError("Le Figaro daily sitemap exceeds the 2 MB safety cap")
    rows = _parse_sitemap(content, sitemap_day=sitemap_day)
    counts = Counter(row["content_class"] for row in rows)
    urls = [row["url"] for row in rows]
    duplicates = Counter(urls)
    repeated = {url: count for url, count in duplicates.items() if count > 1}
    return rows, {
        "day": sitemap_day.isoformat(),
        "url": url,
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "response_bytes": len(content),
        "response_sha256": hashlib.sha256(content).hexdigest(),
        "raw_entries": len(rows),
        "unique_entries": len(duplicates),
        "duplicate_extra_entries": sum(count - 1 for count in repeated.values()),
        "content_class_raw_counts": dict(sorted(counts.items())),
        "url_set_sha256": _url_set_sha256(urls),
    }


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    """Inventory bounded daily sitemaps without fetching article pages."""
    days = _days(start, end)
    all_rows: list[dict] = []
    endpoints: list[dict] = []
    month_urls: dict[str, list[str]] = defaultdict(list)
    month_raw = Counter()
    for sitemap_day in days:
        rows, summary = _read_day(sitemap_day)
        all_rows.extend(rows)
        endpoints.append(summary)
        month = sitemap_day.strftime("%Y-%m")
        month_urls[month].extend(row["url"] for row in rows)
        month_raw[month] += len(rows)

    appearances: dict[str, list[dict]] = defaultdict(list)
    for row in all_rows:
        appearances[row["url"]].append(row)
    urls = sorted(appearances)
    if not urls:
        raise RuntimeError("Le Figaro bounded inventory is empty")

    inventory_rows = []
    relation_counts = Counter()
    for url in urls:
        rows = appearances[url]
        suffix_day = rows[0]["url_suffix_day"]
        suffix_state = rows[0]["url_suffix_state"]
        per_appearance_relations = []
        for row in rows:
            if suffix_state != "valid":
                relation = "no_valid_suffix"
            elif suffix_day == row["sitemap_day"]:
                relation = "same_file_day"
            elif suffix_day > row["sitemap_day"]:
                relation = "after_file_day"
            else:
                relation = "before_file_day"
            relation_counts[relation] += 1
            per_appearance_relations.append(relation)
        inventory_rows.append({
            "url": url,
            "content_class": rows[0]["content_class"],
            "appearance_count": len(rows),
            "sitemap_days": [row["sitemap_day"] for row in rows],
            "sitemap_positions": [row["sitemap_position"] for row in rows],
            "sitemap_lastmods": [row["sitemap_lastmod"] for row in rows],
            "sitemap_lastmod_authoritative": False,
            "url_suffix_day": suffix_day,
            "url_suffix_state": suffix_state,
            "url_suffix_day_authoritative": False,
            "sitemap_day_relations": per_appearance_relations,
        })

    duplicates = {url: len(rows) for url, rows in appearances.items() if len(rows) > 1}
    unique_class_counts = Counter(row["content_class"] for row in inventory_rows)
    raw_class_counts = Counter(row["content_class"] for row in all_rows)
    monthly = {}
    for month in sorted(month_urls):
        monthly[month] = {
            "daily_sitemaps": sum(day.strftime("%Y-%m") == month for day in days),
            "raw_entries": month_raw[month],
            "unique_within_month": len(set(month_urls[month])),
            "duplicate_extra_entries": len(month_urls[month]) - len(set(month_urls[month])),
        }

    _DISCOVERY_ROWS.clear()
    _DISCOVERY_ROWS.update({row["url"]: row for row in inventory_rows})
    evidence.update({
        "mode": "inventory_only_no_article_pages_no_production_writes",
        "method": "bounded_daily_publisher_sitemaps",
        "sitemap_template": SITEMAP_TEMPLATE,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "daily_sitemap_count": len(days),
        "daily_sitemaps": endpoints,
        "monthly_counts": monthly,
        "raw_entries": len(all_rows),
        "unique_urls": len(urls),
        "repeated_canonical_urls": len(duplicates),
        "duplicate_extra_entries": sum(count - 1 for count in duplicates.values()),
        "duplicate_examples": [
            {"url": url, "occurrences": count}
            for url, count in sorted(duplicates.items())[:20]
        ],
        "content_class_raw_counts": dict(sorted(raw_class_counts.items())),
        "content_class_unique_counts": dict(sorted(unique_class_counts.items())),
        "sitemap_day_relation_raw_counts": dict(sorted(relation_counts.items())),
        "url_set_sha256": _url_set_sha256(urls),
        "inventory_rows": inventory_rows,
        "inventory_complete": True,
        "coverage_complete": False,
        "strict_verified_through": None,
        "first_pass_verified_through": None,
        "article_pages_fetched": 0,
        "article_page_samples_require_explicit_bounded_argument": True,
        "article_page_access_requires_approved_route": ARTICLE_PAGE_ACCESS_REQUIRES_APPROVAL,
        "sitemap_day_is_publication_date": False,
        "sitemap_lastmod_used_as_publication_date": False,
        "url_suffix_day_used_as_publication_date": False,
        "authoritative_publication_date_rule": "publisher article datePublished after access approval",
        "host_content_class_policy_resolved": False,
        "legacy_database_writer_executed": False,
        "production_writes": False,
    })
    return urls


def _deterministic_sample(urls: list[str], count: int) -> list[str]:
    ordered = sorted(set(urls))
    if count > len(ordered):
        count = len(ordered)
    if count <= 0:
        return []
    if count == 1:
        return [ordered[0]]
    indices = [round(index * (len(ordered) - 1) / (count - 1)) for index in range(count)]
    return [ordered[index] for index in indices]


def _jsonld_signals(soup: BeautifulSoup) -> dict:
    types = []
    published = []
    accessible = []
    article_body_lengths = []
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(node.string or node.get_text() or "")
        except (TypeError, ValueError):
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value)
                continue
            if not isinstance(value, dict):
                continue
            kinds = value.get("@type")
            kinds = kinds if isinstance(kinds, list) else ([kinds] if kinds else [])
            types.extend(str(kind) for kind in kinds)
            if value.get("datePublished"):
                published.append(str(value["datePublished"]))
            if "isAccessibleForFree" in value:
                accessible.append(value["isAccessibleForFree"])
            if value.get("articleBody"):
                article_body_lengths.append(len(str(value["articleBody"])))
            stack.extend(item for item in value.values() if isinstance(item, (dict, list)))
    return {
        "types": sorted(set(types)),
        "date_published": sorted(set(published)),
        "is_accessible_for_free": accessible,
        "article_body_lengths": article_body_lengths,
    }


def sample_article_pages(
    urls: list[str],
    *,
    sample_count: int,
    acknowledge_access_policy: bool = False,
) -> list[dict]:
    """Fetch at most twelve deterministic metadata-only page samples."""
    if not 1 <= sample_count <= MAX_SAMPLE_PAGES:
        raise ValueError("Le Figaro sample_count must be between 1 and 12")
    if ARTICLE_PAGE_ACCESS_REQUIRES_APPROVAL and not acknowledge_access_policy:
        raise RuntimeError("Le Figaro page samples require explicit access-policy acknowledgement")
    results = []
    for url in _deterministic_sample(urls, sample_count):
        response = _request(url)
        if len(response.content) > MAX_SAMPLE_BYTES:
            raise RuntimeError("Le Figaro article sample exceeds the 3 MB safety cap")
        soup = BeautifulSoup(response.content, "html.parser")
        canonical_node = soup.select_one('link[rel="canonical"][href]')
        published_node = soup.select_one('meta[property="article:published_time"][content]')
        modified_node = soup.select_one('meta[property="article:modified_time"][content]')
        h1 = soup.select_one("h1")
        results.append({
            "url": url,
            "http_status": response.status_code,
            "response_bytes": len(response.content),
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
            "content_class": _content_class(url),
            "canonical": _canonicalize_url(canonical_node.get("href")) if canonical_node else None,
            "has_h1": bool(h1 and h1.get_text(" ", strip=True)),
            "article_published_time": published_node.get("content") if published_node else None,
            "article_modified_time": modified_node.get("content") if modified_node else None,
            "jsonld": _jsonld_signals(soup),
            "body_not_persisted": True,
        })
    return results


def scrape_url(url: str) -> dict:
    raise RuntimeError(
        "Le Figaro is inventory-only; article scraping is disabled pending access and content-class policy"
    )


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date, default=FIRST_PASS_START)
    parser.add_argument("--end", type=_parse_date, default=FIRST_PASS_END)
    parser.add_argument("--sample-count", type=int, default=0)
    parser.add_argument("--acknowledge-access-policy", action="store_true")
    parser.add_argument("--include-urls", action="store_true")
    args = parser.parse_args(argv)
    evidence: dict = {}
    urls = discover_urls(args.start, args.end, evidence)
    payload = {"evidence": evidence}
    if args.include_urls:
        payload["urls"] = urls
    if args.sample_count:
        payload["article_page_samples"] = sample_article_pages(
            urls,
            sample_count=args.sample_count,
            acknowledge_access_policy=args.acknowledge_access_policy,
        )
        evidence["article_pages_fetched"] = len(payload["article_page_samples"])
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
