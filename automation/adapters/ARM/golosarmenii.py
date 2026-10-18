"""Pure Golos Armenii adapter using its first-party WordPress REST feed."""
from __future__ import annotations

from datetime import date, timedelta

from adapters.ARM.common import apply_youtube_only_exclusion, extract_article, fetch, normalize_url


class Config:
    source_name = "golosarmenii.am"
    country_code = "ARM"
    is_env = False


API = "https://golosarmenii.am/wp-json/wp/v2/posts"


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end
    result, seen, pages = [], set(), []
    for offset in range(0, 20000, 100):
        url = (
            f"{API}?per_page=100&offset={offset}&orderby=date&order=desc"
            f"&after={lower.isoformat()}T00:00:00&before={upper.isoformat()}T23:59:59"
            "&_fields=id,date,link"
        )
        rows = fetch(url).json()
        if not isinstance(rows, list):
            raise RuntimeError("Golos Armenii REST response is not a list")
        pages.append({"offset": offset, "rows": len(rows)})
        for row in rows:
            normalized = normalize_url(row.get("link", ""), Config.source_name)
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        if len(rows) < 100:
            break
    else:
        raise RuntimeError("Golos Armenii REST pagination exceeded safety cap")
    evidence.update({
        "strategy": "publisher_rest_date_window",
        "endpoint": API,
        "candidate_date_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "pages_checked": pages,
        "coverage_complete": True,
        "language_scope": "combined WordPress feed contains Russian and /arm/ Armenian URLs",
        "discovered_urls_after_filter": len(result),
    })
    return result


def scrape_url(url: str) -> dict:
    language = "hy" if "/arm/" in url else "ru"
    article = extract_article(
        url,
        source=Config.source_name,
        language=language,
        title_selectors=("h1.elementor-heading-title", "article h1", "h1"),
        body_selectors=(".elementor-widget-theme-post-content .elementor-widget-container", "article"),
        visible_date_selectors=("[itemprop='datePublished']", "article time", "time"),
        prefer_visible_date=True,
        evidence_body_selectors=(".elementor-widget-theme-post-content .elementor-widget-container",),
    )
    apply_youtube_only_exclusion(article)
    return article
