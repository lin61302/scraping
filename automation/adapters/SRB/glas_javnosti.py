"""Environmental observation adapter for Glas Javnosti's Okruženje feed."""
from __future__ import annotations

from datetime import date

from bs4 import BeautifulSoup

from adapters.SRB.common import (
    clean,
    fetch,
    first_text,
    jsonld_objects,
    normalize_url,
    parse_datetime,
    sitemap_rows,
    xml_document,
)


class Config:
    source_name = "glas-javnosti.rs"
    country_code = "SRB"
    is_env = True


INDEX = "https://www.glas-javnosti.rs/sitemap.xml"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    index_rows = sitemap_rows(xml_document(INDEX))
    selected = {}
    checked = []
    reached_cutoff = False
    # Sitemap index has no lastmods. Shards are chronological and the terminal
    # shard is current, so inspect backwards until a shard is wholly old.
    for sitemap_url, _ in reversed(index_rows):
        rows = sitemap_rows(xml_document(sitemap_url))
        dated = [stamp.date() for _, stamp in rows if stamp]
        checked.append({"url": sitemap_url, "rows": len(rows), "oldest": min(dated).isoformat() if dated else None, "newest": max(dated).isoformat() if dated else None})
        for raw, stamp in rows:
            if "/vesti/okruzenje/" not in raw:
                continue
            article_url = normalize_url(raw, Config.source_name)
            if article_url and (stamp is None or start <= stamp.date() <= end):
                selected[article_url] = stamp.date() if stamp else None
        if dated and max(dated) < start:
            reached_cutoff = True
            break
    evidence.update({
        "strategy": "reverse_chronological_sitemap_shards_category_filter",
        "publisher_category": "okruzenje",
        "sitemaps_checked": checked,
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": reached_cutoff,
    })
    return sorted(selected, key=lambda url: (selected[url] or date.min, url))


def scrape_url(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, title_source = first_text(soup, ("h1.naslovtitle", "h1"))

    article_ld = None
    for item in jsonld_objects(soup):
        value = item.get("@type")
        types = set(value if isinstance(value, list) else [value])
        if types & {"Article", "NewsArticle", "ReportageNewsArticle", "BlogPosting"}:
            article_ld = item
            break

    date_signals = []
    published_raw = clean(article_ld.get("datePublished")) if article_ld else None
    modified_raw = clean(article_ld.get("dateModified")) if article_ld else None
    if published_raw:
        date_signals.append((published_raw, "published", "jsonld.datePublished"))
    for node in soup.select("time")[:4]:
        raw = clean(node.get("datetime") or node.get("content") or node.get_text(" ", strip=True))
        if raw and all(signal[0] != raw for signal in date_signals):
            date_signals.append((raw, "published", "time"))
    if modified_raw and all(signal[0] != modified_raw for signal in date_signals):
        date_signals.append((modified_raw, "modified", "jsonld.dateModified"))
    published = published_source = None
    for raw, kind, source_name in date_signals:
        if kind == "modified":
            continue
        parsed = parse_datetime(raw)
        if parsed:
            published, published_source = parsed, source_name
            break

    body_node = soup.select_one("#bodycontent")
    paragraphs = []
    lead = soup.select_one(".newsok h2.lead")
    if lead:
        value = clean(lead.get_text(" ", strip=True))
        if value:
            paragraphs.append(value)
    if body_node:
        clone = BeautifulSoup(str(body_node), "html.parser")
        # The publisher places real story paragraphs inside a wrapper whose
        # class contains `recommended`. Remove only the embedded recommendation
        # card; deleting the wrapper truncates the article after its first line.
        for bad in clone.select("#mytext, .pov-news, script, style, iframe, form, nav, aside, footer, figure, figcaption"):
            bad.decompose()
        for node in clone.select("p"):
            if node.find("p"):
                continue
            value = clean(node.get_text(" ", strip=True))
            if value and len(value) >= 20 and value not in paragraphs:
                paragraphs.append(value)
    body = "\n\n".join(paragraphs) or None

    titles = []
    if title:
        titles.append({"kind": "visible_h1", "source": title_source, "raw": title})
    og = soup.select_one('meta[property="og:title"]')
    for raw, kind, source_name in (
        (clean(og.get("content")) if og else None, "og_title", 'meta[property="og:title"]'),
        (clean(article_ld.get("headline")) if article_ld else None, "jsonld_headline", "jsonld.headline"),
    ):
        if raw and all(item["raw"] != raw for item in titles):
            titles.append({"kind": kind, "source": source_name, "raw": raw})

    return {
        "url": url,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": "sr",
        "environmental": True,
        "parser": "pure_adapter",
        "collection": None,
        "parser_error": None,
        "field_sources": {
            "title": title_source,
            "date_publish": published_source,
            "maintext": "#bodycontent+ .newsok h2.lead",
        },
        "field_evidence": {
            "titles": titles,
            "dates": [
                {"kind": kind, "source": source_name, "raw": raw}
                for raw, kind, source_name in date_signals
            ],
            "bodies": ([{"kind": "publisher_story_body", "text": body[:20000]}] if body else []),
            "http_status": response.status_code,
        },
    }
