"""Exact three row CartaCapital supplement using the current source adapter.

This adapter delegates all network, parsing, and evidence logic to the current
CartaCapital adapter.  Discovery is bounded to the three recorded August 2026
quarantines so the repaired rows can be captured in the standard observation
artifact contract without rerunning the verified monthly payload.
"""
from __future__ import annotations

import hashlib
import html
from datetime import date
from pathlib import Path

from adapters.BRA import cartacapital as base
from scraper_control.observe import adapter_bundle_sha256


class Config(base.Config):
    pass


TARGETS = (
    "https://www.cartacapital.com.br/agencia/respostas-a-crise-climatica-exigem-inovacao-defende-setor-eletrico",
    "https://www.cartacapital.com.br/agencia/mega-sena-acumula-e-proximo-concurso-deve-pagar-r-50-milhoes",
    "https://www.cartacapital.com.br/politica/como-esta-a-disputa-pela-presidencia-na-bahia-goias-e-outros-4-estados-segundo-a-quaest",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    base._api_posts.clear()
    evidence.update({
        "discovery": "exact_recorded_quarantine_urls_plus_publisher_wordpress_day_evidence",
        "api_endpoint": base.API,
        "api_days": [],
        "coverage_complete": False,
    })
    target_days = (date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 27))
    posts = []
    for day in target_days:
        posts.extend(base._api_day(day, evidence))
    found = set()
    for post in posts:
        url = base.normalize_url(post["link"], Config.source_name)
        if url not in TARGETS:
            continue
        raw_title = html.unescape(post.get("title", {}).get("rendered", ""))
        title = base.clean(base.BeautifulSoup(raw_title, "html.parser").get_text(" ", strip=True)) if "<" in raw_title else base.clean(raw_title)
        rendered = post.get("content", {}).get("rendered", "")
        rendered_soup = base.BeautifulSoup(rendered, "html.parser")
        base._api_posts[url] = {
            "id": post.get("id"),
            "date": post["date"],
            "title": title,
            "body": base.clean(rendered_soup.get_text(" ", strip=True)),
            "images": [base.urljoin(url, image["src"]) for image in rendered_soup.select("img[src]")],
            "protected": bool(post.get("content", {}).get("protected")),
        }
        found.add(url)
    missing = sorted(set(TARGETS) - found)
    base_file = Path(base.__file__).resolve()
    evidence["supplemental_exact_target_mode"] = True
    evidence["supplemental_exact_targets"] = list(TARGETS)
    evidence["supplemental_publisher_posts_scanned"] = len(posts)
    evidence["supplemental_missing_targets"] = missing
    evidence["delegated_parser_source_file"] = str(base_file)
    evidence["delegated_parser_source_sha256"] = _sha256(base_file)
    evidence["delegated_parser_bundle_sha256"] = adapter_bundle_sha256(base_file)
    if missing:
        raise RuntimeError(f"CartaCapital exact targets missing from publisher discovery: {missing}")
    evidence["coverage_complete"] = True
    return list(TARGETS)


def scrape_url(url: str) -> dict:
    if url not in TARGETS:
        raise ValueError(f"URL outside exact supplemental target set: {url}")
    return base.scrape_url(url)
