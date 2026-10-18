"""Observation-only Estadão adapter. No database imports or writes."""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup

from adapters.BRA import common as bra_common
from adapters.BRA.common import clean, extract_article, fetch, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "estadao.com.br"
    country_code = "BRA"
    is_env = False


INDEX = "https://www.estadao.com.br/arc/outboundfeeds/sitemap-index-by-day/?outputType=xml"
DAILY = re.compile(r"/sitemap/(20\d{2}-\d{2}-\d{2})/")
LATEST = "/sitemap/latest/"
DENY = (
    "/esportes/", "/paladar/", "/pulsa/", "/copa-do-mundo/", "/blogs-e-colunas/", "/podcasts/",
    "/tv/", "/fotos/", "/emais/", "/onboarding/", "/loterias/", "/minha-serie/", "/cultura/",
    "/viagem/", "/midia-mkt/", "/jogos/",
)
STRICT_CHALLENGE_TITLE = re.compile(
    r"^\s*(?:access denied|captcha|cloudflare|checking your browser|just a moment|verify you are human)\s*[.!-]*\s*$",
    re.I,
)


def _uva_interactive_body(url: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read text from Estadão's server-backed UVA interactive payload."""
    html = fetch(url).text
    match = re.search(r'data-uva-id=\\?"([^"\\]+)', html)
    if not match:
        return None, None
    uva_id = match.group(1)
    endpoint = f"https://arte.estadao.com.br/public/pages/{uva_id}/page.json"
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(endpoint, headers={"User-Agent": "Mozilla/5.0"}, timeout=35)
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    else:
        raise RuntimeError(f"UVA payload fetch failed for {endpoint}: {last_error}")
    paragraphs = []

    def visit(item) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        if item.get("type") == "text" and isinstance(item.get("value"), str):
            value = clean(BeautifulSoup(item["value"], "html.parser").get_text(" ", strip=True))
            if value and len(value) >= 20 and value not in paragraphs:
                paragraphs.append(value)
            return
        for value in item.values():
            if isinstance(value, (dict, list)):
                visit(value)

    visit(payload.get("conteúdo") or [])
    body = "\n\n".join(paragraphs) or None
    evidence = {
        "uva_id": uva_id,
        "endpoint": endpoint,
        "text_blocks": len(paragraphs),
    }
    return body, evidence


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index_entries = sitemap_entries(xml_document(INDEX))
    requested = set()
    cursor = start
    while cursor <= end:
        requested.add(cursor)
        cursor += timedelta(days=1)
    dated = {}
    latest = None
    for url, _ in index_entries:
        match = DAILY.search(url)
        if match:
            day = date.fromisoformat(match.group(1))
            if day in requested:
                dated[day] = url
        elif LATEST in url:
            latest = url
    selected = [(day.isoformat(), dated[day]) for day in sorted(dated)]
    missing = sorted(requested - set(dated))
    # The live index intentionally exposes the in-progress day through `latest`.
    if end in missing and latest:
        selected.append((f"latest_for_{end.isoformat()}", latest))
        missing.remove(end)
    evidence.update({
        "sitemap_index": INDEX,
        "requested_days": [day.isoformat() for day in sorted(requested)],
        "missing_days_in_index": [day.isoformat() for day in missing],
        "latest_sitemap": latest,
        "sitemaps_checked": [],
        "sitemap_failures": [],
    })
    if missing:
        raise RuntimeError(f"daily sitemaps missing from live index: {missing}")
    entries = []
    for label, sitemap_url in selected:
        try:
            items = sitemap_entries(xml_document(sitemap_url))
            evidence["sitemaps_checked"].append({"day": label, "url": sitemap_url, "entries": len(items)})
            entries.extend(items)
        except Exception as exc:
            evidence["sitemap_failures"].append({"day": label, "url": sitemap_url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} requested daily sitemaps failed")
    urls = unique_urls(entries, Config.source_name, DENY)
    evidence["sitemap_entries"] = len(entries)
    evidence["discovered_urls_after_filter"] = len(urls)
    return urls


def scrape_url(url: str) -> dict:
    # A normal TecMundo headline can discuss CAPTCHA. Only a challenge-page
    # title composed of the challenge phrase itself should block the response.
    bra_common.CHALLENGE = STRICT_CHALLENGE_TITLE
    article = extract_article(
        url,
        source=Config.source_name,
        # Blue Studio / sponsored newsroom pages use the same publication
        # metadata as regular articles but a dedicated text wrapper.
        body_selectors=(".news-body", ".content-left-sponsored", "#content .content-wrapper", "article"),
        visible_date_selectors=(".principal-dates time", ".principal-dates", "article time", "main time"),
        visible_date_pattern=r"\b\d{2}/\d{2}/20\d{2}\s*\|\s*\d{1,2}h\d{2}\b",
        evidence_body_selectors=("#content", "article", "main"),
    )
    if article.get("title") and article.get("date_publish") and not article.get("maintext"):
        body, uva_evidence = _uva_interactive_body(url)
        if body:
            article["maintext"] = body
            article["field_sources"]["maintext"] = "uva.page_json"
            article["field_evidence"]["bodies"].append({"kind": "uva_page_json", "text": body[:20000]})
            article["field_evidence"]["uva"] = uva_evidence
    return article
