"""Read-only Jornal do Brasil observer.

The publisher exposes an archive index and a rolling current sitemap.  Sitemap
``lastmod`` is only a discovery lower bound; publication is checked against the
visible timestamp and NewsArticle JSON-LD on each candidate page.  This module
has no database import, side effect on import, or production write path.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from urllib.parse import urlparse

from dateutil.tz import gettz

from bs4 import BeautifulSoup

from adapters.BRA.common import clean, fetch, jsonld_objects, normalize_url, sitemap_entries, xml_document


class Config:
    source_name = "jb.com.br"
    country_code = "BRA"
    is_env = False


INDEX = "https://www.jb.com.br/sitemap/2.xml"
LATEST = "https://www.jb.com.br/sitemap.xml"
MAP = "https://www.jb.com.br/sitemap/map/{number}.xml"
MAP_NUMBER = re.compile(r"/sitemap/map/(\d+)\.xml$")
URL_MONTH = re.compile(r"/(20\d\d)/(0[1-9]|1[0-2])/\d+-[^/]+\.html$")
ARTICLE_PATH = re.compile(r"^(?P<section>/.+)/(?P<year>20\d\d)/(?P<month>0[1-9]|1[0-2])/(?P<id>\d+)-[^/]+\.html$")
VISIBLE_DATE = re.compile(r"Publicado\s+em\s+(\d{2})/(\d{2})/(20\d{2})\s+(?:às|as)\s+(\d{1,2}):(\d{2})", re.I)
AGENCY_LINES = {"reuters", "efe", "agência efe", "afp", "ap", "estadão conteúdo", "o globo"}
SAO_PAULO = gettz("America/Sao_Paulo")
if SAO_PAULO is None:
    raise RuntimeError("America/Sao_Paulo timezone is unavailable")
_articles: dict[str, dict] = {}


def _repair_mojibake(raw: str | None) -> str | None:
    if not raw:
        return raw
    if not any(marker in raw for marker in ("Ã", "Â", "â€")):
        return raw
    # The publisher occasionally decodes UTF-8 as Windows-1252.  Latin-1
    # cannot re-encode the resulting smart-quote bytes (â€˜ / â€™).
    marker_count = lambda value: sum(value.count(marker) for marker in ("Ã", "Â", "â€"))
    for encoding in ("latin1", "cp1252"):
        try:
            repaired = raw.encode(encoding).decode("utf-8")
        except (UnicodeError, ValueError):
            continue
        if "\ufffd" not in repaired and marker_count(repaired) < marker_count(raw):
            return repaired
    return raw


def _same_article_slug_alias(requested: str, canonical: str) -> bool:
    """Accept only a publisher slug revision within the same section/month/ID."""
    request_url, canonical_url = urlparse(requested), urlparse(canonical)
    if (request_url.hostname or "").lower() != (canonical_url.hostname or "").lower():
        return False
    request_path, canonical_path = ARTICLE_PATH.fullmatch(request_url.path), ARTICLE_PATH.fullmatch(canonical_url.path)
    return bool(request_path and canonical_path and request_path.groupdict() == canonical_path.groupdict())


def _month(raw: str) -> tuple[int, int] | None:
    match = URL_MONTH.search(urlparse(raw).path)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _map_number(raw: str) -> int | None:
    match = MAP_NUMBER.search(urlparse(raw).path)
    return int(match.group(1)) if match else None


def _publication(raw: str | None) -> datetime | None:
    if not raw:
        return None
    # JB emits this Unix-epoch fallback on unavailable articles.  It is not
    # a publication date and must never send a 2026 sitemap URL outside range.
    if raw.startswith("1969-12-31T21:00:00"):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(SAO_PAULO).replace(tzinfo=None) if parsed.tzinfo else parsed


def _is_article_ld(item: dict) -> bool:
    raw = item.get("@type")
    types = raw if isinstance(raw, list) else [raw]
    return bool({"NewsArticle", "Article"} & set(value for value in types if isinstance(value, str)))


def _media_only(body_node, article_node) -> dict | None:
    """Identify a publisher media item only when no article prose remains."""
    if body_node is None or article_node is None:
        return None
    body = BeautifulSoup(str(body_node), "html.parser")
    for node in body.select(
        "script, style, iframe, img, video, "
        "blockquote.twitter-tweet, blockquote.instagram-media, blockquote.instagram-media-registered"
    ):
        node.decompose()
    if clean(body.get_text(" ", strip=True)):
        return None
    media = None
    for selector, provider in (
        ("div.materia-corpo iframe[src*='youtube.com/embed/']", "youtube"),
        ("article iframe[src*='youtube.com/embed/']", "youtube"),
        ("div.materia-corpo iframe[src*='instagram.com/']", "instagram"),
        ("div.materia-corpo blockquote.twitter-tweet a[href*='/status/']", "twitter"),
    ):
        node = article_node.select_one(selector.replace("article ", "", 1)) if selector.startswith("article ") else article_node.select_one(selector)
        if node is None:
            continue
        raw_url = node.get("src") or node.get("href") or ""
        parsed = urlparse(raw_url)
        if provider == "youtube":
            if parsed.scheme != "https" or parsed.netloc not in {"youtube.com", "www.youtube.com"} or not parsed.path.startswith("/embed/"):
                continue
            # JB emits both YouTube embed host variants.  Canonicalize the
            # bare host so the existing strict policy validator can verify it.
            parsed = parsed._replace(netloc="www.youtube.com")
        if provider == "instagram" and parsed.netloc not in {"www.instagram.com", "instagram.com"}:
            continue
        if provider == "twitter" and parsed.netloc not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            continue
        media = {"provider": provider, "selector": selector, "media_url": parsed._replace(query="", fragment="").geturl(),
                 "body_text_after_media": "", "body_only_media": True}
        break
    return media


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end:
        raise ValueError("start must be on or before end")
    _articles.clear()
    evidence.update({
        "strategy": "indexed_archive_plus_rolling_sitemap_with_page_date_filter",
        "index": INDEX, "rolling_sitemap": LATEST, "coverage_complete": False,
        "sitemaps_checked": [], "sitemap_failures": [], "page_failures": [],
        "publication_outside_window": [], "publication_filter_unresolved": [],
        "candidate_urls_without_lastmod": [],
    })
    archive_index = sitemap_entries(xml_document(INDEX))
    maps = sorted((_map_number(url), url) for url, _ in archive_index if _map_number(url) is not None)
    if not maps or any(number != maps[0][0] + offset for offset, (number, _) in enumerate(maps)):
        raise RuntimeError("JB archive index is empty or map numbers are not contiguous")
    if maps[0][0] <= 1:
        raise RuntimeError("JB archive index has no older boundary map")

    # The immediately preceding archive shard verifies that this window starts
    # after the earlier index.  Older windows need their own indexed scan.
    predecessor = MAP.format(number=maps[0][0] - 1)
    older_entries = sitemap_entries(xml_document(predecessor))
    older_months = [_month(url) for url, _ in older_entries]
    older_max = max((value for value in older_months if value), default=None)
    evidence["older_index_boundary"] = {
        "url": predecessor, "entries": len(older_entries), "max_url_month": older_max,
        "before_window": bool(older_max and older_max < (start.year, start.month)),
    }
    all_entries: dict[str, date | None] = {}
    latest_map_urls: set[str] = set()
    for number, url in maps:
        try:
            entries = sitemap_entries(xml_document(url))
        except Exception as exc:
            evidence["sitemap_failures"].append({"url": url, "error": str(exc)})
            continue
        normalized = [normalize_url(raw, Config.source_name) for raw, _ in entries]
        evidence["sitemaps_checked"].append({"url": url, "entries": len(entries), "map_number": number})
        for (raw, modified), normalized_url in zip(entries, normalized):
            if normalized_url and _month(normalized_url):
                all_entries[normalized_url] = modified
        if number == maps[-1][0]:
            latest_map_urls = {item for item in normalized if item and _month(item)}

    rolling_entries = sitemap_entries(xml_document(LATEST))
    rolling_urls: set[str] = set()
    for raw, modified in rolling_entries:
        normalized_url = normalize_url(raw, Config.source_name)
        if normalized_url and _month(normalized_url):
            rolling_urls.add(normalized_url)
            all_entries[normalized_url] = modified
    evidence["rolling_sitemap_entries"] = len(rolling_entries)
    evidence["rolling_archive_overlap"] = len(rolling_urls & latest_map_urls)
    evidence["latest_archive_urls_missing_from_rolling"] = len(latest_map_urls - rolling_urls)
    evidence["publisher_urls_in_inventory"] = len(all_entries)
    evidence["archive_map_count"] = len(maps)
    evidence["lastmod_is_not_publication_date"] = True

    candidates = []
    for url, modified in sorted(all_entries.items()):
        if modified is not None and modified < start:
            continue
        if modified is None:
            evidence["candidate_urls_without_lastmod"].append(url)
        candidates.append(url)
    evidence["lastmod_lower_bound_candidates"] = len(candidates)
    selected = []
    for url in candidates:
        try:
            article = _parse_page(url)
        except Exception as exc:
            evidence["page_failures"].append({"url": url, "error": str(exc)})
            selected.append(url)  # The normal observer records the exact failure.
            continue
        published = article.get("date_publish")
        date_conflict = "visible_vs_jsonld_publication_disagreement" in (article.get("parser_error") or "")
        url_month = _month(url)
        url_month_in_window = bool(url_month and (start.year, start.month) <= url_month <= (end.year, end.month))
        unresolved = published is None or date_conflict or bool(article.get("parser_error"))
        if unresolved or start <= published.date() <= end:
            selected.append(url)
            _articles[url] = article
            if unresolved and url_month_in_window:
                evidence["publication_filter_unresolved"].append({
                    "url": url, "date_publish": published.isoformat() if published else None,
                    "parser_error": article.get("parser_error"),
                })
        else:
            evidence["publication_outside_window"].append({
                "url": url, "date_publish": published.isoformat(),
                "non_date_parser_error": article.get("parser_error"),
            })
    evidence["selected_after_page_date_filter"] = len(selected)
    evidence["coverage_complete"] = bool(
        evidence["older_index_boundary"]["before_window"]
        and not evidence["sitemap_failures"]
        and not evidence["page_failures"]
        and latest_map_urls
        and latest_map_urls <= rolling_urls
        and rolling_urls
        and not evidence["publication_filter_unresolved"]
    )
    return selected


def _parse_page(url: str) -> dict:
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    # Column pages have an author-name h1 before the article's own h1.
    h1 = soup.select_one("article h1:not(.author-name)") or soup.select_one("h1:not(.author-name)")
    visible_h1_raw = clean(html.unescape(h1.get_text(" ", strip=True))) if h1 else None
    title = _repair_mojibake(visible_h1_raw)
    og = soup.select_one('meta[property="og:title"]')
    og_title = _repair_mojibake(clean(html.unescape(og.get("content", "")))) if og else None
    publication_node = soup.select_one("p.publicacao")
    visible_raw = clean(_repair_mojibake(publication_node.get_text(" ", strip=True))) if publication_node else None
    match = VISIBLE_DATE.search(visible_raw or "")
    try:
        visible = datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)), int(match.group(4)), int(match.group(5))) if match else None
    except ValueError:
        visible = None
    ld_article = next((item for item in jsonld_objects(soup) if _is_article_ld(item)), {})
    ld_date = _publication(ld_article.get("datePublished"))
    ld_title = _repair_mojibake(clean(html.unescape(ld_article.get("headline", ""))))
    body_node = soup.select_one("div.materia-corpo")
    paragraphs = []
    body_source = "div.materia-corpo p"
    if body_node:
        body = BeautifulSoup(str(body_node), "html.parser")
        for bad in body.select("script, style, iframe, aside, figure, figcaption, nav, form, .publicidade, .related, .share, .social, .texto-foto"):
            bad.decompose()
        for node in body.select("p"):
            value = clean(_repair_mojibake(node.get_text(" ", strip=True)))
            if value and value.casefold() not in AGENCY_LINES and value not in paragraphs:
                paragraphs.append(value)
        # The publisher's /jogos-online/ advertorials put genuine long-form
        # prose in left-aligned divs and use p only for image captions.
        if urlparse(url).path.startswith("/jogos-online/"):
            div_paragraphs = []
            body_copy = body.select_one("div.materia-corpo")
            for node in body_copy.find_all("div", recursive=False) if body_copy else []:
                if "text-align: left" not in (node.get("style") or "").lower():
                    continue
                for br in node.select("br"):
                    # The publisher sometimes nests all following text inside
                    # a malformed <br>. Unwrap it so that text is not lost.
                    br.insert_before("\n")
                    br.unwrap()
                for fragment in re.split(r"\n+", node.get_text(" ", strip=True)):
                    value = clean(_repair_mojibake(fragment))
                    if value and value not in div_paragraphs:
                        div_paragraphs.append(value)
            if sum(len(item) for item in div_paragraphs) >= 500:
                paragraphs = div_paragraphs
                body_source = "div.materia-corpo > div[style*='text-align: left']"
    maintext = "\n\n".join(paragraphs) or None
    archival_scan = None
    if (
        urlparse(url).path.startswith("/acervo/deu-no-zozimo/")
        and maintext and len(maintext) < 700
        and "Clique aqui para ler a" in maintext
        and "licenciar acervo do JORNAL DO BRASIL" in maintext
        and body_node is not None
    ):
        image_urls = [node.get("src") for node in body_node.select("img[src]")
                      if (node.get("src") or "").startswith("https://midias.jb.com.br/_midias/jpg/")]
        pdf_urls = [node.get("href") for node in body_node.select("a[href]")
                    if (node.get("href") or "").startswith("https://midias.jb.com.br/_midias/pdf/")]
        if image_urls or pdf_urls:
            issue_date = re.search(r"\b(\d{1,2})/(\d{1,2})/(19\d{2})\b", maintext)
            archival_scan = {"image_urls": image_urls, "pdf_urls": pdf_urls,
                             "landing_page_text": maintext,
                             "original_issue_date_hint_raw": issue_date.group(0) if issue_date else None,
                             "publisher_page_date_is_not_original_issue_date": True}
            maintext = None  # Click/zoom/licensing instructions are not news prose.
    canonical_node = soup.select_one('link[rel="canonical"]')
    canonical = normalize_url(canonical_node.get("href", ""), Config.source_name) if canonical_node else None
    issues = []
    if (ld_article.get("datePublished") or "").startswith("1969-12-31T21:00:00"):
        issues.append("publisher_placeholder_publication_date")
    if archival_scan:
        issues.append("archival_scan_without_maintext")
    if visible and ld_date and abs((visible - ld_date).total_seconds()) > 120:
        issues.append("visible_vs_jsonld_publication_disagreement")
    if title and og_title and title.casefold() != og_title.casefold():
        issues.append("h1_vs_og_title_disagreement")
    if title and ld_title and title.casefold() != ld_title.casefold():
        issues.append("h1_vs_jsonld_title_disagreement")
    normalized_requested = normalize_url(url, Config.source_name)
    canonical_alias = bool(
        canonical and canonical != normalized_requested
        and normalized_requested and _same_article_slug_alias(normalized_requested, canonical)
        and title and og_title and ld_title
        and title.casefold() == og_title.casefold() == ld_title.casefold()
        and visible and ld_date and abs((visible - ld_date).total_seconds()) <= 120
        and maintext and len(maintext) >= 180
    )
    if canonical and canonical != normalized_requested and not canonical_alias:
        issues.append("canonical_url_disagreement")
    if not title or not (visible or ld_date) or not maintext:
        issues.append("required_field_missing")
    if maintext and (len(maintext) < 180 or (len(paragraphs) < 2 and len(maintext) < 1000)):
        issues.append("body_short_or_single_paragraph")
    date_publish = visible or ld_date
    dates = []
    if visible:
        dates.append({"kind": "visible_time", "source": "p.publicacao", "raw": visible.isoformat()})
    if ld_date:
        dates.append({"kind": "published", "source": "jsonld.datePublished", "raw": ld_date.isoformat()})
    meta = soup.select_one('meta[property="article:published_time"]')
    article = {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "date_publish": date_publish, "title": title, "maintext": maintext, "language": "pt",
        "parser": "pure_adapter", "collection": None,
        "parser_error": ",".join(issues) if issues else None,
        "field_sources": {"title": "h1", "date_publish": "p.publicacao" if visible else "jsonld.datePublished", "maintext": body_source},
        "field_evidence": {
            "http_status": response.status_code,
            "titles": [item for item in [
                {"kind": "visible_h1", "raw": title} if title else None,
                {"kind": "og_title", "raw": og_title} if og_title else None,
                {"kind": "jsonld_headline", "raw": ld_title} if ld_title else None,
            ] if item],
            "dates": dates, "bodies": [{"kind": body_source, "text": maintext[:20000]}] if maintext else [],
            "visible_publication_raw": visible_raw,
            "jsonld_date_published_raw": ld_article.get("datePublished"),
            "jsonld_date_modified": ld_article.get("dateModified"),
            "ambiguous_article_published_time_meta": meta.get("content") if meta else None,
            "paragraph_count": len(paragraphs), "canonical": canonical, "disagreements": issues,
            "archival_scan": archival_scan,
            "visible_h1_raw": visible_h1_raw if visible_h1_raw != title else None,
            "canonical_slug_alias": {
                "requested_url": normalized_requested, "publisher_canonical": canonical,
                "same_section_month_article_id": True,
                "title_signals_agree": True, "publication_signals_agree": True,
            } if canonical_alias else None,
        },
    }
    media = _media_only(body_node, soup.select_one("article"))
    if media and set(issues) <= {"required_field_missing", "body_short_or_single_paragraph"}:
        article["collection"] = "SKIP"
        article["parser_error"] = None
        article["field_evidence"]["publisher_media"] = media
        article["exclusion_reason"] = "excluded_embedded_media_without_article_prose"
        article["exclusion_evidence"] = {
            "policy_type": "jb_embedded_media_without_article_prose",
            "publisher_url": url, **media,
        }
    return article


def scrape_url(url: str) -> dict:
    return dict(_articles.get(url) or _parse_page(url))
