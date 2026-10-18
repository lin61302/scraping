from __future__ import annotations

"""Read Mali Actu's article publication date, excluding its live topbar date."""

import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def _normal_url(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "maliactu.net":
        raise ValueError("unexpected Mali Actu URL")
    return parsed.hostname, parsed.path.rstrip("/") or "/"


def _newsarticle_dates(node) -> list[str]:
    found = []
    if isinstance(node, dict):
        kind = node.get("@type")
        if kind == "NewsArticle" or isinstance(kind, list) and "NewsArticle" in kind:
            value = node.get("datePublished")
            if isinstance(value, str):
                found.append(value)
        for value in node.values():
            found.extend(_newsarticle_dates(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_newsarticle_dates(value))
    return found


def parse_publisher_date(html: str, url: str) -> datetime:
    """Return the exact publisher timestamp as naive UTC or fail closed.

    Mali Actu's ``time.topbar-date`` is today's site clock. NewsPlease can
    select it as the article date; only the article-scoped, agreeing publisher
    fields are accepted here.
    """

    expected = _normal_url(url)
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical is None or _normal_url(canonical.get("href") or "") != expected:
        raise ValueError("Mali Actu canonical URL mismatch")
    article_time = soup.select_one('time.meta-date[itemprop="datePublished"]')
    published_meta = soup.select_one('meta[property="article:published_time"]')
    if article_time is None or published_meta is None:
        raise ValueError("Mali Actu article publication fields missing")
    value = article_time.get("datetime")
    if not value or published_meta.get("content") != value:
        raise ValueError("Mali Actu article publication fields disagree")
    schema_dates = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            schema_dates.extend(_newsarticle_dates(json.loads(tag.string or tag.get_text())))
        except (TypeError, ValueError):
            continue
    if not schema_dates or any(item != value for item in schema_dates):
        raise ValueError("Mali Actu NewsArticle publication date disagrees")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid Mali Actu article publication date") from exc
    if parsed.tzinfo is None:
        raise ValueError("Mali Actu article date lacks timezone")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)
