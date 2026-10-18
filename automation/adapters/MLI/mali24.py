"""Observation-only Mali24 adapter using the live Yoast sitemap frontier."""
from __future__ import annotations

from datetime import date

from adapters.MLI.common import extract_article, wordpress_sitemap_window


class Config:
    source_name = "mali24.info"
    country_code = "MLI"
    is_env = False


INDEX = "https://mali24.info/sitemap_index.xml"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    return wordpress_sitemap_window(
        index_url=INDEX,
        domain=Config.source_name,
        start=start,
        end=end,
        evidence=evidence,
    )


def scrape_url(url: str) -> dict:
    article, soup = extract_article(
        url=url,
        domain=Config.source_name,
        title_selectors=("article h1", "main h1", "h1"),
        body_selectors=("article .cm-entry-summary", ".cm-entry-summary"),
        visible_date_selectors=("article time[datetime]",),
        parser="mali24_visible_h1_time_entry_summary",
    )
    newsletter = "Subscribe to get the latest posts sent to your email."
    body = article.get("maintext") or ""
    if body.endswith("\n\n" + newsletter):
        article["maintext"] = body[: -len(newsletter)].rstrip()
        article["body_cleanup"] = {"removed": "publisher_newsletter_footer"}
    categories = [node.get_text(" ", strip=True) for node in soup.select("article .cm-post-categories a")]
    if not categories:
        entry = soup.select_one("article.post")
        categories = [value[len("category-"):] for value in (entry.get("class") if entry else []) if value.startswith("category-")]
    article["categories"] = categories
    if any("sport" in value.casefold() for value in categories):
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_sport_category"
        article["exclusion_evidence"] = {"publisher_categories": categories}
    return article
