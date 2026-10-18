"""Pure Macaranga environmental source adapter."""
from __future__ import annotations

from datetime import date

from adapters.MYS.common import extract_article, wordpress_sitemap_window


class Config:
    source_name = "macaranga.org"
    country_code = "MYS"
    is_env = True


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    return wordpress_sitemap_window(
        index_url="https://www.macaranga.org/sitemap_index.xml",
        domain=Config.source_name,
        start=start,
        end=end,
        evidence=evidence,
        child_pattern=r"/post-sitemap\.xml$",
        newest_overlap=1,
    )


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        language="en",
        title_selectors=("article h1.entry-title", "h1.entry-title", "article h1", "main h1"),
        body_selectors=("[data-elementor-type='wp-post']", "article .entry-content", ".entry-content", "article"),
        visible_date_selectors=("article time.entry-date", "article time", "time"),
        evidence_body_selectors=("[data-elementor-type='wp-post']", "article .entry-content", ".entry-content", "article"),
        body_drop_selectors=(".elementor-posts-container",),
    )
    # On the publisher's two September 2026 blue-carbon stories this prompt
    # appears after the legitimate reporting-method notes and before a
    # related-post carousel. Keep the notes, remove only the orphan prompt.
    prompt = "\n\nThe journalist used t he following models and tools to produce these reports:"
    body = article.get("maintext") or ""
    if body.endswith(prompt):
        article["maintext"] = body[: -len(prompt)].rstrip()
        for signal in article.get("field_evidence", {}).get("bodies", []):
            if signal.get("text", "").endswith(prompt):
                signal["text"] = signal["text"][: -len(prompt)].rstrip()
        article["body_cleanup"] = {"removed": "publisher_related_posts_widget"}
    return article
