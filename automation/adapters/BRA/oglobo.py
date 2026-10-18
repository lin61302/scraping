"""Observation-only O Globo adapter. No database imports or writes."""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

from adapters.BRA.common import extract_article, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "oglobo.globo.com"
    country_code = "BRA"
    is_env = False


INDEX = "https://oglobo.globo.com/sitemap/oglobo/sitemap.xml"
DAILY = re.compile(r"/sitemap/oglobo/(20\d{2})/(\d{2})/(\d{2})_\d+\.xml$")
DENY = (
    "/esportes/", "/rioshow/", "/blogs/", "/cultura/", "/saude/", "/o-globo-plus/",
    "/playlist/videos/", "/blogs-e-colunas/", "/opiniao/", "/gastronomia/", "/kogut/", "/play/",
    "/ela/", "/boa-viagem/", "/acervo/", "/infograficos/", "/fotogalerias/", "/jogos/", "/patrocinado/",
)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    requested = set()
    cursor = start
    while cursor <= end:
        requested.add(cursor)
        cursor += timedelta(days=1)
    selected = []
    represented = set()
    for url, modified in sitemap_entries(xml_document(INDEX)):
        match = DAILY.search(url)
        if not match:
            continue
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if day in requested:
            selected.append((day, url, modified))
            represented.add(day)
    missing = sorted(requested - represented)
    evidence.update({
        "sitemap_index": INDEX,
        "requested_days": [day.isoformat() for day in sorted(requested)],
        "missing_days_in_index": [day.isoformat() for day in missing],
        "sitemaps_checked": [],
        "sitemap_failures": [],
    })
    if missing:
        raise RuntimeError(f"daily sitemaps missing from live index: {missing}")
    entries = []
    for day, sitemap_url, modified in sorted(selected):
        try:
            items = sitemap_entries(xml_document(sitemap_url))
            evidence["sitemaps_checked"].append({"day": day.isoformat(), "url": sitemap_url, "entries": len(items)})
            entries.extend(items)
        except Exception as exc:
            evidence["sitemap_failures"].append({"day": day.isoformat(), "url": sitemap_url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} requested daily sitemaps failed")
    urls = unique_urls(entries, Config.source_name, DENY)
    evidence["sitemap_entries"] = len(entries)
    evidence["discovered_urls_after_filter"] = len(urls)
    return urls


def scrape_url(url: str) -> dict:
    def parse() -> dict:
        return extract_article(
            url,
            source=Config.source_name,
            body_selectors=("#mc-article-body article", "#mc-article-body", ".content-text", "article"),
            visible_date_selectors=("time[datetime]", "article time", "main time"),
            visible_date_pattern=r"\b\d{2}/\d{2}/20\d{2}\s+\d{1,2}h\d{2}\b",
            evidence_body_selectors=("#mc-article-body", "article", "main"),
        )

    article = parse()
    if not article.get("title") and not article.get("date_publish") and not article.get("maintext"):
        # O Globo occasionally returns a 200 shell containing only
        # "Carregando..." on the first request. A bounded second fetch has the
        # server-rendered article and avoids misclassifying that transient as a
        # publisher tombstone.
        time.sleep(1)
        article = parse()
    return article
