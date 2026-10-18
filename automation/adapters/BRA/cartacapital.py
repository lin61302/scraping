"""Read-only CartaCapital observation via the publisher's WordPress API and sitemap.

The API supplies publication-date bounded, count-reconciled discovery. Yoast's
post sitemap is an independent URL inventory; its ``lastmod`` is never treated
as publication time. Article fields come from the rendered publisher page.
"""
from __future__ import annotations

import html
import math
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup
from dateutil.tz import gettz

from adapters.BRA.common import SESSION, clean, fetch, jsonld_objects, normalize_url, sitemap_entries, xml_document


class Config:
    source_name = "cartacapital.com.br"
    country_code = "BRA"
    is_env = False


API = "https://www.cartacapital.com.br/wp-json/wp/v2/posts"
SITEMAP_INDEX = "https://www.cartacapital.com.br/sitemap_index.xml"
POST_SITEMAP = re.compile(r"/post-sitemap\d+\.xml$", re.I)
VISIBLE_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(20\d{2})\s+(\d{1,2})h(\d{2})\b")
LOCAL_ZONE = gettz("America/Sao_Paulo")
if LOCAL_ZONE is None:
    raise RuntimeError("America/Sao_Paulo timezone unavailable")
PAGE_SIZE = 100
LEGACY_BLACKLIST = (
    "/cultura/", "/jogos/", "/blogs/augusto-diniz/", "/blogs/", "/opiniao/",
    "/podcasts/", "/do-micro-ao-macro/", "/toquetec/", "/astrologia/",
)

# Only metadata needed for independent article-page checks is held in memory.
# Nothing is persisted or sent to the production database by this adapter.
_api_posts: dict[str, dict] = {}


def _discard_post_view_cookies() -> None:
    """Prevent publisher per-article view cookies from exceeding its header limit.

    CartaCapital sets a distinct ``eltd-post-views_<id>`` cookie for every
    article. Keeping them in the shared HTTP session caused all article reads
    after roughly 263 pages to fail with HTTP 400. Preserve PHPSESSID and any
    access cookies; remove only this publisher's per-article counters.
    """
    for cookie in list(SESSION.cookies):
        if cookie.name.startswith("eltd-post-views_") and cookie.domain.lstrip(".").endswith("cartacapital.com.br"):
            SESSION.cookies.clear(domain=cookie.domain, path=cookie.path, name=cookie.name)


def _post_date(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _api_day(day: date, evidence: dict) -> list[dict]:
    lower = f"{day - timedelta(days=1)}T23:59:59"
    upper = f"{day + timedelta(days=1)}T00:00:00"
    params = {"after": lower, "before": upper, "per_page": PAGE_SIZE, "orderby": "date", "order": "asc"}
    for attempt in range(2):
        posts = []
        expected = pages = None
        page_counts = []
        page = 1
        while True:
            response = fetch(f"{API}?{urlencode({**params, 'page': page})}")
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"WordPress API returned non-list for {day} page {page}")
            try:
                total = int(response.headers["X-WP-Total"])
                total_pages = int(response.headers["X-WP-TotalPages"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"WordPress API omitted pagination totals for {day} page {page}") from exc
            if expected is None:
                expected, pages = total, total_pages
                if pages != math.ceil(total / PAGE_SIZE):
                    raise RuntimeError(f"WordPress API inconsistent page count for {day}: {total}, {pages}")
            page_counts.append({"page": page, "count": len(payload), "header_total": total, "header_pages": total_pages})
            posts.extend(payload)
            if page >= pages:
                break
            page += 1
        ids = [post.get("id") for post in posts]
        stable = (
            len(posts) == expected
            and len(set(ids)) == len(posts)
            and all(item["header_total"] == expected and item["header_pages"] == pages for item in page_counts)
        )
        if stable:
            selected = []
            for post in posts:
                if not isinstance(post, dict) or not post.get("date") or not post.get("link"):
                    raise RuntimeError(f"WordPress API item lacks date or URL for {day}")
                if _post_date(post["date"]).date() == day:
                    selected.append(post)
            evidence["api_days"].append({
                "day": day.isoformat(), "header_total": expected, "pages": page_counts,
                "in_day": len(selected), "boundary_spillovers": len(posts) - len(selected),
                "stability_attempt": attempt + 1,
            })
            return selected
    raise RuntimeError(f"WordPress API changed count or duplicated IDs during {day} pagination: {page_counts}")


def _sitemap_inventory(start: date, end: date, evidence: dict) -> tuple[set[str], bool]:
    index = [(url, modified) for url, modified in sitemap_entries(xml_document(SITEMAP_INDEX)) if POST_SITEMAP.search(url)]
    if not index:
        raise RuntimeError("CartaCapital sitemap index has no post sitemaps")
    # Sitemaps are chunked by WordPress post order, not original publication
    # date. Include the predecessor before the first child ending on/after the
    # start and every newer child. CartaCapital sometimes creates a backdated
    # article later; such a URL appears in a newer shard even though its
    # original publication date belongs to this window.
    first = next((i for i, (_, modified) in enumerate(index) if modified is None or modified >= start), len(index) - 1)
    first = max(0, first - 1)
    boundary_last = next(
        (i for i in range(first, len(index)) if index[i][1] is None or index[i][1] >= end),
        len(index) - 1,
    )
    last = len(index) - 1
    selected = index[first:last + 1]
    lower_boundary = first == 0 or index[first][1] is None or index[first][1] < start
    upper_boundary = index[boundary_last][1] is None or index[boundary_last][1] >= end
    evidence["post_sitemaps_in_index"] = len(index)
    evidence["post_sitemaps_checked"] = []
    evidence["sitemap_chunk_selection"] = {
        "first": selected[0][0], "first_index_lastmod": selected[0][1].isoformat() if selected[0][1] else None,
        "last": selected[-1][0], "last_index_lastmod": selected[-1][1].isoformat() if selected[-1][1] else None,
        "end_boundary": index[boundary_last][0],
        "successor_included": last > boundary_last,
        "tail_sitemaps_included": last - boundary_last,
        "lower_boundary_included": lower_boundary, "upper_boundary_reached": upper_boundary,
    }
    urls = set()
    for sitemap_url, modified in selected:
        entries = sitemap_entries(xml_document(sitemap_url))
        evidence["post_sitemaps_checked"].append({
            "url": sitemap_url, "index_lastmod": modified.isoformat() if modified else None,
            "entries": len(entries),
        })
        for raw, _ in entries:
            normalized = normalize_url(raw, Config.source_name)
            if normalized:
                urls.add(normalized)
    evidence["sitemap_urls_in_selected_children"] = len(urls)
    return urls, lower_boundary and upper_boundary


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end:
        raise ValueError("start must be on or before end")
    _api_posts.clear()
    evidence.update({
        "discovery": "publisher_wordpress_rest_by_publication_day_plus_post_sitemap_reconciliation",
        "api_endpoint": API,
        "sitemap_index": SITEMAP_INDEX,
        "api_days": [],
        "api_duplicate_canonical_urls": [],
        "coverage_complete": False,
    })
    posts = []
    day = start
    while day <= end:
        posts.extend(_api_day(day, evidence))
        day += timedelta(days=1)
    normalized = []
    seen = set()
    for post in posts:
        url = normalize_url(post["link"], Config.source_name)
        if not url:
            raise RuntimeError(f"WordPress API returned invalid source URL: {post['link']!r}")
        raw_title = html.unescape(post.get("title", {}).get("rendered", ""))
        title = clean(BeautifulSoup(raw_title, "html.parser").get_text(" ", strip=True)) if "<" in raw_title else clean(raw_title)
        rendered = post.get("content", {}).get("rendered", "")
        rendered_soup = BeautifulSoup(rendered, "html.parser")
        api_body = clean(rendered_soup.get_text(" ", strip=True))
        api_images = [urljoin(url, image["src"]) for image in rendered_soup.select("img[src]")]
        candidate = {
            "id": post.get("id"), "date": post["date"], "title": title,
            "body": api_body, "images": api_images,
            "protected": bool(post.get("content", {}).get("protected")),
        }
        if url in seen:
            previous = _api_posts[url]
            comparable = ("date", "title", "body", "images", "protected")
            if any(previous.get(field) != candidate.get(field) for field in comparable):
                raise RuntimeError(f"WordPress API returned conflicting posts for canonical URL: {url}")
            evidence["api_duplicate_canonical_urls"].append({
                "url": url,
                "post_ids": sorted([previous.get("id"), candidate.get("id")]),
                "date": candidate["date"],
                "fields_identical": True,
            })
            continue
        seen.add(url)
        _api_posts[url] = candidate
        normalized.append((post["date"], post.get("id") or 0, url))
    sitemap_urls, chunk_bounds_complete = _sitemap_inventory(start, end, evidence)
    missing = sorted(seen - sitemap_urls)
    evidence["api_posts_in_window"] = len(posts)
    evidence["api_unique_urls"] = len(seen)
    evidence["api_urls_missing_in_selected_sitemaps"] = len(missing)
    evidence["api_urls_missing_in_selected_sitemaps_sample"] = missing[:20]
    evidence["sitemap_lastmod_is_not_publication_date"] = True
    evidence["coverage_complete"] = chunk_bounds_complete and not missing
    evidence["legacy_blacklist_url_patterns"] = list(LEGACY_BLACKLIST)
    excluded = [
        {"url": url, "matched_patterns": [pattern for pattern in LEGACY_BLACKLIST if pattern in url]}
        for _, _, url in sorted(normalized)
        if any(pattern in url for pattern in LEGACY_BLACKLIST)
    ]
    evidence["legacy_blacklisted_urls"] = excluded
    evidence["legacy_blacklisted_count"] = len(excluded)
    selected = [url for _, _, url in sorted(normalized) if not any(pattern in url for pattern in LEGACY_BLACKLIST)]
    evidence["selected_after_legacy_blacklist"] = len(selected)
    return selected


def _visible_publication(soup: BeautifulSoup) -> tuple[datetime | None, str | None]:
    node = soup.select_one(".s-content__infos")
    raw = clean(node.get_text(" ", strip=True)) if node else None
    match = VISIBLE_DATE.search(raw or "")
    if not match:
        return None, raw
    day, month, year, hour, minute = (int(item) for item in match.groups())
    try:
        return datetime(year, month, day, hour, minute), match.group(0)
    except ValueError:
        return None, raw


def _meta_local(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value.astimezone(LOCAL_ZONE).replace(tzinfo=None) if value.tzinfo else value
    except ValueError:
        return None


def _metadata_wall_clock(raw: str | None) -> datetime | None:
    """Return a publisher timestamp without applying its advertised offset.

    A small subset of CartaCapital's agency posts labels a Sao Paulo wall
    clock as ``+00:00``.  This value is only used as corroboration when the
    visible timestamp, JSON-LD wall clock, and WordPress local timestamp all
    agree; it is never used as the publication date by itself.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _article_body(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """Read CartaCapital article prose and its inline lists in page order.

    Poll results, named people, and product lists are published as ``<li>``
    inside the article body. The shared paragraph-only helper loses those
    substantive facts, so this source needs a list-aware extraction rule.
    """
    original = soup.select_one(".s-content__text")
    if original is None:
        return None, None
    node = BeautifulSoup(str(original), "html.parser")
    for bad in node.select(
        "script, style, iframe, form, nav, aside, footer, figure, figcaption, "
        ".ad, .ads, .advertisement, .related, .share, .social, .tags, .newsletter, "
        ".author, .container-video-noticia, .container-blogs-e-colunas-vale-ler-tambem, "
        ".testeira-assine, [class*='related'], [class*='recommend'], [class*='share']"
    ):
        bad.decompose()
    parts = []
    for item in node.select("p, h2, li"):
        # A paragraph or child list inside an li is already included by its
        # parent item; avoid duplicating that passage.
        if item.find_parent("li"):
            continue
        value = clean(item.get_text(" ", strip=True))
        if value and (
            value.startswith("❗ Se você tem algum posicionamento")
            or value.startswith("✅ Receba as notícias")
        ):
            continue
        minimum = 3 if item.name == "li" else 20
        # Keep repeated list values. Poll stories legitimately repeat the same
        # candidate/result under several state or scenario headings, and
        # global text de-duplication silently removed those published facts.
        if value and len(value) >= minimum:
            parts.append(value)
    body = "\n\n".join(parts)
    return (body, ".s-content__text") if body else (None, None)


def scrape_url(url: str) -> dict:
    _discard_post_view_cookies()
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title_node = soup.select_one("main.single-post h1")
    title_source = "main.single-post h1" if title_node else None
    if title_node is None:
        title_node = soup.select_one("h1")
        title_source = "h1" if title_node else None
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    body, body_source = _article_body(soup)
    published, visible_raw = _visible_publication(soup)
    meta = soup.select_one('meta[property="article:published_time"]')
    meta_raw = clean(meta.get("content")) if meta else None
    meta_local = _meta_local(meta_raw)
    if published is None and meta_local:
        published = meta_local
        date_source = "meta.article:published_time converted to America/Sao_Paulo"
    else:
        date_source = ".s-content__infos visible publication"

    titles = [{"kind": "visible_h1", "source": title_source, "raw": title}] if title else []
    og = soup.select_one('meta[property="og:title"]')
    if og and clean(og.get("content")):
        titles.append({"kind": "og_title", "source": 'meta[property="og:title"]', "raw": clean(og.get("content"))})
    dates = []
    if published and visible_raw:
        dates.append({"kind": "visible_time", "source": ".s-content__infos", "raw": published.isoformat()})
    if meta_raw:
        dates.append({"kind": "published", "source": "meta.article:published_time", "raw": meta_raw})
    structured_body_empty = False
    for item in jsonld_objects(soup):
        types = item.get("@type")
        types = set(types if isinstance(types, list) else [types])
        if not types & {"Article", "NewsArticle", "ReportageNewsArticle"}:
            continue
        headline = clean(item.get("headline"))
        if headline:
            titles.append({"kind": "jsonld_headline", "source": "jsonld.headline", "raw": headline})
        if item.get("datePublished"):
            dates.append({"kind": "published", "source": "jsonld.datePublished", "raw": item["datePublished"]})
        structured_body_empty = "articleBody" in item and not clean(item.get("articleBody"))
        break
    bodies = [{"kind": body_source, "text": body[:20000]}] if body else []
    parser_error = None
    disagreements = []
    api = _api_posts.get(url)
    if api:
        if api["protected"]:
            parser_error = "wordpress_content_protected"
        if title and api["title"] and SequenceMatcher(None, title.casefold(), api["title"].casefold()).ratio() < 0.78:
            disagreements.append("visible_title_vs_wordpress_api")
        if published and abs((published - _post_date(api["date"])).total_seconds()) > 120:
            disagreements.append("visible_date_vs_wordpress_api")
        if body and api["body"]:
            body_words = set(re.findall(r"\w+", body.casefold()))
            api_words = set(re.findall(r"\w+", api["body"].casefold()))
            overlap = len(body_words & api_words) / max(1, len(api_words))
            body_char_ratio = len(clean(body) or "") / max(1, len(api["body"]))
            if overlap < 0.75:
                disagreements.append("page_body_vs_wordpress_api")
            if len(api["body"]) >= 400 and body_char_ratio < 0.60:
                disagreements.append("page_body_shorter_than_wordpress_api")
            bodies.append({"kind": "wordpress_api.content.rendered", "text": api["body"][:20000]})
        else:
            overlap = None
            body_char_ratio = None
        titles.append({"kind": "wordpress_api.title.rendered", "source": "wp-json/wp/v2/posts", "raw": api["title"]})
        dates.append({"kind": "published", "source": "wordpress_api.date", "raw": api["date"]})
    else:
        overlap = None
        body_char_ratio = None
    publication_time_resolution = None
    if published and meta_local and abs((published - meta_local).total_seconds()) > 120:
        meta_wall_clock = _metadata_wall_clock(meta_raw)
        api_wall_clock = _post_date(api["date"]) if api and api.get("date") else None
        jsonld_wall_clocks = [
            _metadata_wall_clock(item.get("raw"))
            for item in dates
            if item.get("source") == "jsonld.datePublished"
        ]
        wall_clock_consensus = (
            meta_wall_clock is not None
            and api_wall_clock is not None
            and abs((published - meta_wall_clock).total_seconds()) <= 120
            and abs((published - api_wall_clock).total_seconds()) <= 120
            and any(
                value is not None and abs((published - value).total_seconds()) <= 120
                for value in jsonld_wall_clocks
            )
        )
        if wall_clock_consensus:
            publication_time_resolution = {
                "code": "publisher_utc_label_inconsistent_wall_clock_consensus",
                "accepted_source": ".s-content__infos visible publication",
                "visible": published.isoformat(),
                "metadata_raw": meta_raw,
                "wordpress_local": api["date"],
                "basis": "visible, JSON-LD wall clock, and WordPress local timestamp agree",
            }
        else:
            disagreements.append("visible_date_vs_published_meta")
    if disagreements:
        parser_error = "publisher_field_disagreement:" + ",".join(disagreements)
    article = {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "date_publish": published, "maintext": body, "language": "pt",
        "parser": "pure_adapter", "collection": None, "parser_error": parser_error,
        "field_sources": {"title": title_source, "date_publish": date_source, "maintext": body_source},
        "field_evidence": {
            "titles": titles, "dates": dates, "bodies": bodies, "http_status": response.status_code,
            "visible_publication_raw": visible_raw, "meta_publication_local": meta_local.isoformat() if meta_local else None,
            "wordpress_post_id": api["id"] if api else None, "wordpress_body_word_overlap": overlap,
            "wordpress_body_char_ratio": body_char_ratio,
            "disagreements": disagreements,
            "publication_time_resolution": publication_time_resolution,
        },
    }
    # This exact publisher series contains picture-only cartoons. Exclude an
    # item only when the rendered page, WordPress body, and NewsArticle schema
    # all confirm there is no article prose and the publisher images match.
    if re.search(r"/venes-2/cartum-\d+$", url) and api and not api["protected"] and not body and not api["body"]:
        container = soup.select_one(".s-content__text")
        page_images = [urljoin(url, image["src"]) for image in container.select("img[src]")] if container else []
        if (
            container and not clean(container.get_text(" ", strip=True))
            and len(page_images) == len(api["images"]) == 1
            and page_images == api["images"] and structured_body_empty
        ):
            article["field_evidence"]["publisher_images"] = [{
                "url": page_images[0], "alt": None, "selector": ".s-content__text img",
            }]
            article["field_evidence"]["wordpress_image_urls"] = api["images"]
            article["field_evidence"]["structured_article_body_empty"] = True
            article["collection"] = "SKIP"
            article["exclusion_reason"] = "excluded_image_only_publisher_item"
            article["exclusion_evidence"] = {
                "policy_type": "publisher_image_only",
                "publisher_item_type": "image_led_cartoon",
                "publisher_dom_selector": ".s-content__text img",
                "publisher_image_url": page_images[0],
                "wordpress_post_id": api["id"],
                "wordpress_image_matches": True,
                "structured_article_body_empty": True,
            }
    return article
