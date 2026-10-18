"""Observation-only Malijet adapter using its dated section frontiers."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from bs4 import BeautifulSoup

from adapters.MLI.common import clean, extract_article, fetch, normalize_url, parse_datetime


class Config:
    source_name = "malijet.com"
    country_code = "MLI"
    is_env = False


BASE = "https://malijet.com"
SECTIONS = (
    "actualte_dans_les_regions_du_mali",
    "actualite-politique-au-mali",
    "actualite_internationale",
    "actualite_economique_du_mali",
    "a_la_une_du_mali",
    "la_societe_malienne_aujourdhui",
    "communiques-de-presse",
)
MAX_PAGES_PER_SECTION = 100
RELATIVE_DATE = re.compile(r"il y a\s+\d+\s+(?:minute|heure|jour|semaine|mois|an)s?", re.I)
DENY = ("/actualite_sportive_au_mali/", "/videos/", "/jt-ortm/")


def _article_id(url: str) -> int | None:
    match = re.search(r"/(\d{5,})-", url)
    return int(match.group(1)) if match else None


def _card_date(card, relative_base: datetime) -> date | None:
    text_value = clean(card.get_text(" ", strip=True)) or ""
    match = RELATIVE_DATE.search(text_value)
    if match:
        parsed = parse_datetime(match.group(0), relative_base=relative_base)
        return parsed.date() if parsed else None
    # This handles a possible future return to an absolute date label.
    for node in card.select("time, .date, .entry-date"):
        parsed = parse_datetime(node.get("datetime") or node.get_text(" ", strip=True), relative_base=relative_base)
        if parsed:
            return parsed.date()
    return None


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    relative_base = datetime.combine(end, time(23, 59, 59))
    boundary_pad = timedelta(days=7)
    evidence.update(
        {
            "method": "dated_section_pagination_plus_numeric_sitemap_frontier",
            "sections": list(SECTIONS),
            "pages_checked": [],
            "coverage_boundaries": {},
            "relative_date_policy": "French relative listing labels parsed against requested end with seven-day boundary pad",
            "article_date_policy": "article time/meta/JSON-LD datePublished is authoritative",
        }
    )
    by_section: dict[str, list[str]] = {}
    for section in SECTIONS:
        section_urls = []
        reached_boundary = False
        for page in range(1, MAX_PAGES_PER_SECTION + 1):
            listing_url = f"{BASE}/{section}/?page={page}"
            soup = BeautifulSoup(fetch(listing_url).content, "html.parser")
            cards = soup.select("article")
            rows = []
            for card in cards:
                link = card.select_one("h2 a[href]")
                if not link:
                    continue
                normalized = normalize_url(link.get("href"), Config.source_name, DENY)
                listed = _card_date(card, relative_base)
                if normalized:
                    rows.append((normalized, listed))
            dates = [listed for _, listed in rows if listed]
            row_evidence = {"section": section, "page": page, "url": listing_url, "articles": len(rows)}
            if dates:
                row_evidence.update({"newest_approx": max(dates).isoformat(), "oldest_approx": min(dates).isoformat()})
            evidence["pages_checked"].append(row_evidence)
            if not rows:
                evidence["coverage_boundaries"][section] = {"page": page, "reason": "empty"}
                reached_boundary = True
                break
            # The pad is only a conservative pagination stop condition. Do not
            # send known out-of-window listing dates to article validation.
            section_urls.extend(url for url, listed in rows if listed is None or start <= listed <= end + timedelta(days=1))
            if dates and max(dates) < start - boundary_pad:
                evidence["coverage_boundaries"][section] = {
                    "page": page,
                    "reason": "newest_relative_label_before_padded_start",
                    "newest_approx": max(dates).isoformat(),
                }
                reached_boundary = True
                break
        if not reached_boundary:
            raise RuntimeError(f"Malijet section {section!r} reached page cap without a date boundary")
        by_section[section] = list(dict.fromkeys(section_urls))

    # Interleave sections so bounded canaries check several independent paths.
    discovered = []
    for index in range(max((len(urls) for urls in by_section.values()), default=0)):
        for section in SECTIONS:
            if index < len(by_section[section]):
                discovered.append(by_section[section][index])
    result = list(dict.fromkeys(discovered))
    # Listing pagination can shift while a run is in progress. Reconcile the
    # same allowed sections against the newest numeric article sitemap, using
    # the oldest padded listing candidate as a monotonic ID frontier. Sitemap
    # lastmod is intentionally ignored because Malijet rewrites it on old URLs.
    ids = [_article_id(url) for url in result]
    frontier_id = min(value for value in ids if value is not None) if any(value is not None for value in ids) else None
    supplemental = []
    sitemap_url = None
    sitemap_entries = 0
    if frontier_id is not None:
        index = BeautifulSoup(fetch(f"{BASE}/sitemap.xml").content, "xml")
        article_sitemaps = []
        for node in index.find_all("loc"):
            raw = node.get_text(strip=True)
            match = re.search(r"sitemap-articles-(\d+)\.xml$", raw)
            if match:
                article_sitemaps.append((int(match.group(1)), raw))
        if article_sitemaps:
            sitemap_url = max(article_sitemaps)[1]
            sitemap = BeautifulSoup(fetch(sitemap_url, timeout=120).content, "xml")
            raw_urls = [node.get_text(strip=True) for node in sitemap.find_all("loc")]
            sitemap_entries = len(raw_urls)
            allowed_paths = tuple(f"/{section}/" for section in SECTIONS)
            for raw in raw_urls:
                identifier = _article_id(raw)
                if identifier is None or identifier < frontier_id or not any(path in raw for path in allowed_paths):
                    continue
                normalized = normalize_url(raw, Config.source_name, DENY)
                if normalized:
                    supplemental.append(normalized)
    result = list(dict.fromkeys([*result, *supplemental]))
    evidence["urls_by_section"] = {section: len(urls) for section, urls in by_section.items()}
    evidence["numeric_sitemap_frontier"] = {
        "frontier_article_id": frontier_id,
        "sitemap": sitemap_url,
        "entries": sitemap_entries,
        "supplemental_allowed_section_urls": len(set(supplemental) - set(discovered)),
        "lastmod_policy": "ignored_as_unusable",
    }
    evidence["discovered_urls_after_dedup"] = len(result)
    return result


def scrape_url(url: str) -> dict:
    article, _ = extract_article(
        url=url,
        domain=Config.source_name,
        title_selectors=("article[data-mj-article] h1", "article h1", "h1"),
        body_selectors=("article[data-mj-article]",),
        visible_date_selectors=("article[data-mj-article] time[datetime]",),
        parser="malijet_article_h1_time_prose",
    )
    return article
