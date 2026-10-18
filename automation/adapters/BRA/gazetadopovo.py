"""Observation-only Gazeta do Povo adapter. No database imports or writes."""
from __future__ import annotations

import re
from datetime import date, timedelta

from adapters.BRA.common import extract_article, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "gazetadopovo.com.br"
    country_code = "BRA"
    is_env = False


INDEX = "https://www.gazetadopovo.com.br/sitemap.xml"
DAILY = re.compile(r"/(20\d{2})/(\d{2})/(20\d{2}-\d{2}-\d{2})\.xml$")
DENY = (
    "/conteudo-publicitario/", "/esportes/", "/vozes/", "/saber/", "/opiniao/editoriais/", "/ideias/",
    "/cultura/", "/videos/", "/tudo-sobre/saber-forca-da-familia/", "/tudo-sobre/saber-cultura-woke/",
    "/tudo-sobre/frases-da-semana/", "/tudo-sobre/saber-icones-do-conservadorismo/", "/opiniao/bom-dia/",
    "/tudo-sobre/saber-defesa-da-vida/", "/cursos/", "/newsletter/", "/opiniao/",
)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    requested = set()
    cursor = start
    while cursor <= end:
        requested.add(cursor)
        cursor += timedelta(days=1)
    selected = {}
    for url, _ in sitemap_entries(xml_document(INDEX)):
        match = DAILY.search(url)
        if not match:
            continue
        day = date.fromisoformat(match.group(3))
        if day in requested:
            selected[day] = url
    missing = sorted(requested - set(selected))
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
    for day, sitemap_url in sorted(selected.items()):
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
    return extract_article(
        url,
        source=Config.source_name,
        body_selectors=("#tp-post-content", "[class*='postBodyContainer']", "article"),
        visible_date_selectors=(),
        visible_date_pattern=r"\b\d{2}/\d{2}/20\d{2}\s+às\s+\d{2}:\d{2}\b",
        evidence_body_selectors=("main #tp-post-content", "main", "article"),
    )

