"""Observation adapter for N1 Serbia under its legacy database identity."""
from __future__ import annotations

from datetime import date

from adapters.SRB.common import extract_article, normalize_url, sitemap_rows, xml_document


class Config:
    source_name = "rs.n1info.com"
    country_code = "SRB"
    is_env = False


SITEMAP = "https://n1info.rs/sitemap/sitemap_post_{}.xml"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    selected = {}
    checked = []
    reached_cutoff = False
    for number in range(1, 250):
        url = SITEMAP.format(number)
        rows = sitemap_rows(xml_document(url))
        if not rows:
            break
        dated = [stamp.date() for _, stamp in rows if stamp]
        checked.append({"url": url, "rows": len(rows), "oldest": min(dated).isoformat() if dated else None, "newest": max(dated).isoformat() if dated else None})
        for raw, stamp in rows:
            article_url = normalize_url(raw, "n1info.rs", deny=("/video/", "/author/", "/tag/"))
            if article_url and (stamp is None or start <= stamp.date() <= end):
                selected[article_url] = stamp.date() if stamp else None
        if dated and max(dated) < start:
            reached_cutoff = True
            break
    evidence.update({
        "strategy": "newest_first_numbered_post_sitemaps",
        "live_domain": "n1info.rs",
        "database_identity": Config.source_name,
        "sitemaps_checked": checked,
        "discovered_urls_after_filter": len(selected),
        "coverage_complete": reached_cutoff,
        "date_authority": "article JSON-LD datePublished; sitemap lastmod is discovery-only",
    })
    return sorted(selected, key=lambda url: (selected[url] or date.min, url))


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        # The page-level ``h1.title`` is the story title.  Long N1 explainers
        # can contain additional ``h1`` nodes inside the article body, so an
        # ``article h1`` selector can silently substitute an in-story heading.
        title_selectors=("h1.title", "h1"),
        # N1 renders each paragraph group in a separate
        # ``.article-content-wrapper``.  Selecting the first wrapper truncates
        # most stories; the enclosing article aggregates every content block.
        body_selectors=("article.article-wrapper",),
        visible_date_selectors=('meta[property="article:published_time"]',),
        evidence_body_selectors=("article.article-wrapper",),
    )
    body = " ".join(str(article.get("maintext") or "").split())
    cta = "Pridružite se diskusiji ili pročitajte komentare"
    if (
        url.startswith("https://n1info.rs/najave-emisija/")
        and str(article.get("title") or "").casefold().startswith("gledajte")
        and body.endswith(cta)
        and len(body) < 80
        and len(body.split()) <= 12
        and article.get("date_publish")
    ):
        declared = {
            "publisher_url": url,
            "publisher_dom_selector": "article.article-wrapper",
            "publisher_body_text": body,
            "publisher_cta": cta,
            "publisher_item_type": "live_program_announcement",
        }
        article["field_evidence"]["publisher_program_announcements"] = [declared]
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_program_announcement"
        article["exclusion_evidence"] = {
            "policy_type": "publisher_program_announcement",
            **declared,
        }
    return article
