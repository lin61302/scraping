"""Environmental Danas category adapter kept separate from the MLP stream."""
from __future__ import annotations

from datetime import date

from adapters.SRB.common import discover_category_pages
from adapters.SRB.danas import Config as GeneralConfig, scrape_url as scrape_general


class Config:
    source_name = "danas.rs"
    country_code = "SRB"
    is_env = True


BASE = "https://www.danas.rs/rubrika/vesti/klimatske-promene/"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    urls = discover_category_pages(
        base_url=BASE,
        page_url=lambda page: BASE if page == 1 else f"{BASE}page/{page}/",
        link_selector="h3.article-post-title a[href]",
        domains="danas.rs",
        start=start,
        end=end,
        evidence=evidence,
        card_selector="article.post",
        max_pages=250,
    )
    evidence["environmental_scope"] = "publisher climate-change category"
    return urls


def scrape_url(url: str) -> dict:
    article = scrape_general(url)
    article["environmental"] = True
    return article

