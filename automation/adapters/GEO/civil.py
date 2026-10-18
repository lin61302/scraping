"""Pure network observation adapter for civil.ge."""
from __future__ import annotations

from datetime import date, timedelta

from bs4 import BeautifulSoup

from adapters.GEO.common import (
    article_body, fetch, first_text, normalize_url, page_evidence,
    parse_datetime, parser_error, published_signal, sitemap_entries, xml_document,
)


class Config:
    source_name = "civil.ge"
    country_code = "GEO"
    is_env = False


SITEMAP = "https://civil.ge/post-sitemap{suffix}.xml"
MAX_SITEMAPS = 100


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    overlap = start - timedelta(days=2)
    evidence.update({"method": "numbered_post_sitemaps", "overlap_start": overlap.isoformat(),
                     "sitemaps_checked": [], "sitemap_failures": []})
    candidates = []
    boundary = False
    for number in range(1, MAX_SITEMAPS + 1):
        url = SITEMAP.format(suffix="" if number == 1 else number)
        try:
            entries = sitemap_entries(xml_document(url))
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": url, "error": str(exc)})
            break
        dated = [(raw, modified) for raw, modified in entries if modified]
        dates = [modified for _, modified in dated]
        accepted = []
        for raw, modified in dated:
            normalized = normalize_url(raw, Config.source_name, ("/category/", "/tag/", "/author/"))
            if normalized and overlap <= modified <= end + timedelta(days=1):
                accepted.append((modified, normalized))
        evidence["sitemaps_checked"].append({
            "number": number, "url": url, "entries": len(entries), "candidates": len(accepted),
            "newest_lastmod": max(dates).isoformat() if dates else None,
            "oldest_lastmod": min(dates).isoformat() if dates else None,
        })
        candidates.extend(accepted)
        if dates and max(dates) < overlap:
            boundary = True
            evidence["coverage_boundary"] = {"number": number, "reason": "sitemap_before_overlap",
                                             "newest_lastmod": max(dates).isoformat()}
            break
    evidence["coverage_complete"] = boundary and not evidence["sitemap_failures"]
    if not evidence["coverage_complete"]:
        raise RuntimeError("Civil.ge sitemap walk did not reach a clean lower date boundary")
    candidates.sort(reverse=True)
    result = list(dict.fromkeys(url for _, url in candidates))
    evidence["discovered_urls_before_dedup"] = len(candidates)
    evidence["discovered_urls_after_dedup"] = len(result)
    return result


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_selector = first_text(soup, ("h1.post-title", "article h1", "h1"))
    body, body_selector = article_body(soup, ("div.entry-content", "article .entry-content", "article"))
    date_raw, date_source = published_signal(soup)
    published = parse_datetime(date_raw)
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "maintext": body, "date_publish": published,
        "parser": "civil_h1_published_metadata_entry_content",
        "parser_error": parser_error(title, body, published),
        "field_evidence": page_evidence(soup, title=title, title_selector=title_selector,
                                         date_raw=date_raw, date_source=date_source, body=body,
                                         body_selector=body_selector, status=response.status_code),
    }
