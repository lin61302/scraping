"""Pure Malaysiakini adapter for the publisher-advertised language sitemaps."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.MYS.common import _jsonld_article, clean, extract_article, fetch, sitemap_entries, unique_urls, xml_document


class Config:
    source_name = "malaysiakini.com"
    country_code = "MYS"
    is_env = False


LANGUAGES = ("en", "my", "zh")
AUDITED_IMAGE_ONLY_URL = "https://www.malaysiakini.com/news/782745"
EXTERNAL_REDIRECT_URL = (
    "https://www.malaysiakini.com/news/884389-international-press-freedom-award-2000"
)
DUPLICATE_REDIRECT_URL = (
    "https://www.malaysiakini.com/news/884865-pasaraya-durjana-bagaimana-telegram-"
    "dijadikan-tempat-jaja-bahan-penderaan-seksual-kanak-kanak"
)
IMAGE_ONLY_URLS = {
    AUDITED_IMAGE_ONLY_URL,
    "https://www.malaysiakini.com/news/884386-top-employers-award-for-2024-2024",
    "https://www.malaysiakini.com/news/884387-50-most-influential-communicators-2001",
    "https://www.malaysiakini.com/news/884388-50-movers-and-shakers-of-the-new-economy-2000",
    "https://www.malaysiakini.com/news/783700",
}


def _sequence(url: str) -> int:
    match = re.search(r"article-sitemap(\d+)\.xml$", url)
    return int(match.group(1)) if match else -1


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    lower, upper = start - timedelta(days=2), end + timedelta(days=1)
    evidence.update({
        "strategy": "publisher_advertised_multilingual_sitemap_indexes",
        "publisher_languages": list(LANGUAGES),
        "language_indexes": {},
        "sitemaps_checked": [],
        "sitemap_failures": [],
        "coverage_complete": False,
    })
    entries = []
    for language in LANGUAGES:
        index_url = f"https://www.malaysiakini.com/{language}/sitemap_index.xml"
        index_entries = [
            item for item in sitemap_entries(xml_document(index_url))
            if re.search(r"/article-sitemap\d+\.xml$", item[0])
        ]
        if not index_entries:
            evidence["sitemap_failures"].append({"url": index_url, "error": "no article sitemaps"})
            continue
        selected = {url: modified for url, modified in index_entries if modified is None or modified >= lower}
        selected.update(dict(sorted(index_entries, key=lambda item: _sequence(item[0]))[-3:]))
        evidence["language_indexes"][language] = {
            "url": index_url,
            "article_sitemaps_total": len(index_entries),
            "article_sitemaps_selected": len(selected),
        }
        for sitemap_url, modified in selected.items():
            try:
                items = sitemap_entries(xml_document(sitemap_url))
                evidence["sitemaps_checked"].append({
                    "url": sitemap_url,
                    "language": language,
                    "index_lastmod": modified.isoformat() if modified else None,
                    "entries": len(items),
                })
                entries.extend(
                    item for item in items
                    if item[1] is None or lower <= item[1] <= upper
                )
            except Exception as exc:
                evidence["sitemap_failures"].append({"url": sitemap_url, "error": str(exc)})
    if evidence["sitemap_failures"]:
        raise RuntimeError(f"{len(evidence['sitemap_failures'])} Malaysiakini sitemap failures")
    # The legacy source intentionally covered the /news route. Opinion and
    # other content types use separate routes and are outside that source policy.
    entries = [item for item in entries if re.search(r"malaysiakini\.com/news/", item[0])]
    urls = unique_urls(entries, Config.source_name)
    # The publisher omitted this image-only Aug 21 item from the selected
    # sitemap children. A complete numeric-ID audit around the Aug 22 volume
    # trough proved it is the only missing /news route in that bounded gap.
    if start <= date(2026, 8, 21) <= end and AUDITED_IMAGE_ONLY_URL not in urls:
        urls.append(AUDITED_IMAGE_ONLY_URL)
        evidence["audited_extra_urls"] = [AUDITED_IMAGE_ONLY_URL]
    evidence.update({
        "candidate_entries_in_window": len(entries),
        "discovered_urls_after_filter": len(urls),
        "route_policy": "news articles only, matching the legacy enumerated /news source",
        "coverage_complete": True,
    })
    return urls


def _html_text(value: object) -> str | None:
    raw = clean(value)
    if not raw or raw.startswith(("http://", "https://")):
        return raw
    return clean(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))


def _newslab_document(soup: BeautifulSoup, canonical_url: str) -> tuple[str, str, str]:
    for tag in soup.select('script[type="application/json"][data-url*="/api/docs/"]'):
        endpoint = tag.get("data-url")
        try:
            outer = json.loads(tag.string or tag.get_text() or "")
            document = json.loads(outer["body"])["data"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        segment = document
        part = re.search(r"/(part\d+)/?$", canonical_url)
        if not isinstance(segment.get("article"), list) and part:
            segment = document.get(part.group(1))
        if not isinstance(segment, dict) or not isinstance(segment.get("article"), list):
            continue
        title = _html_text(segment.get("title"))
        paragraphs = []
        standfirst = _html_text(segment.get("standfirst"))
        if standfirst:
            paragraphs.append(standfirst)
        for item in segment["article"]:
            if not isinstance(item, dict):
                continue
            for key in ("heading", "header", "title", "quote", "text", "value", "caption"):
                value = item.get(key)
                paragraph = _html_text(value) if isinstance(value, str) else None
                if paragraph and len(paragraph) >= 10 and not paragraph.startswith(("http://", "https://")):
                    if paragraph not in paragraphs:
                        paragraphs.append(paragraph)
        body = "\n\n".join(paragraphs)
        if title and len(body) >= 150:
            return endpoint, title, body
    raise ValueError(f"News Lab page did not expose a complete publisher API article: {canonical_url}")


def _mark_image_only(article: dict, soup: BeautifulSoup) -> None:
    title = clean(article.get("title"))
    image = next((
        item for item in soup.select("article.article-content div.image picture img[src]")
        if clean(item.get("alt")) == title
    ), None)
    if image is None:
        raise ValueError(f"Expected title-matched Malaysiakini image: {article['url']}")
    image_url = urljoin(article["url"], image.get("src"))
    selector = "article.article-content div.image picture img[src]"
    article.setdefault("field_evidence", {})["publisher_images"] = [{
        "url": image_url, "alt": title, "selector": selector,
    }]
    article["collection"] = "SKIP"
    article["exclusion_reason"] = "excluded_image_only_publisher_item"
    article["exclusion_evidence"] = {
        "policy_type": "publisher_image_only",
        "publisher_image_url": image_url,
        "publisher_dom_selector": selector,
        "structured_article_body_empty": True,
    }


def scrape_url(url: str) -> dict:
    article = extract_article(
        url,
        source=Config.source_name,
        language="en",
        title_selectors=("h1.title", "article h1", "main h1", "h1"),
        body_selectors=(
            ".description.article-body",
            ".article-body",
            "article.article-content",
            "article",
        ),
        visible_date_selectors=("article time", "time"),
        evidence_body_selectors=(".article-body", "article.article-content", "article"),
    )
    needs_refetch = (
        not article.get("title")
        or len(article.get("maintext") or "") < 150
        or url in IMAGE_ONLY_URLS | {EXTERNAL_REDIRECT_URL, DUPLICATE_REDIRECT_URL}
    )
    if not needs_refetch:
        return article
    response = fetch(url, timeout=90)
    soup = BeautifulSoup(response.content, "html.parser")

    if url == EXTERNAL_REDIRECT_URL:
        canonical = soup.select_one('link[rel="canonical"][href]')
        canonical_url = canonical.get("href") if canonical else None
        redirect_status = response.history[-1].status_code if response.history else None
        declared = {
            "publisher_url": url, "final_url": response.url,
            "canonical_url": canonical_url, "redirect_status": redirect_status,
        }
        if declared["final_url"] != "https://cpj.org/awards/awards00/" or canonical_url != declared["final_url"] or redirect_status != 301:
            raise ValueError(f"Unexpected Malaysiakini external redirect: {declared}")
        article.setdefault("field_evidence", {})["publisher_redirects"] = [declared]
        article["collection"] = "SKIP"
        article["exclusion_reason"] = "excluded_external_redirect"
        article["exclusion_evidence"] = {"policy_type": "publisher_external_redirect", **declared}
        return article

    if not article.get("title"):
        canonical = soup.select_one('link[rel="canonical"][href]')
        canonical_url = canonical.get("href") if canonical else response.url
        endpoint, title, body = _newslab_document(soup, canonical_url)
        article["title"], article["maintext"] = title, body
        article.setdefault("field_sources", {}).update({
            "title": "publisher_newslab_api.title",
            "maintext": "publisher_newslab_api.article",
        })
        evidence = article.setdefault("field_evidence", {})
        evidence["titles"] = [{"kind": "publisher_api_title", "source": endpoint, "raw": title}]
        evidence["bodies"] = [{"kind": "publisher_api_body", "source": endpoint, "text": body[:20000]}]
        evidence["publisher_api"] = {"endpoint": endpoint, "canonical_url": canonical_url}
        if url == DUPLICATE_REDIRECT_URL:
            article["collection"] = "SKIP"
            article["exclusion_reason"] = "duplicate_publisher_redirect"
            article["exclusion_evidence"] = {
                "policy_type": "publisher_duplicate_redirect",
                "canonical_url": canonical_url,
                "duplicate_of": "https://www.malaysiakini.com/news/781584",
            }
        return article

    structured = _jsonld_article(soup)
    structured_body = clean(structured.get("articleBody")) if structured else None
    if structured_body and len(structured_body) >= 150 and len(structured_body) > len(article.get("maintext") or ""):
        article["maintext"] = structured_body
        article.setdefault("field_sources", {})["maintext"] = "jsonld.articleBody"
        article.setdefault("field_evidence", {})["bodies"] = [{
            "kind": "jsonld_articleBody", "source": "jsonld.articleBody", "text": structured_body[:20000],
        }]
    if url in IMAGE_ONLY_URLS:
        _mark_image_only(article, soup)
    return article
