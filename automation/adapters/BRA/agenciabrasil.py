"""Read-only Agência Brasil environment archive observation candidate.

The orchestration identity exists, but the legacy production source record
needs reconciliation. Keep this adapter local until that identity and
discovery coverage are resolved. The archive's dated cards bound discovery; page-zero
featured stories are checked against their own scoped article headers.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from adapters.BRA.common import clean, fetch, normalize_url


class Config:
    source_name = "agenciabrasil.ebc.com.br"
    country_code = "BRA"
    is_env = True


BASE = "https://agenciabrasil.ebc.com.br"
ARCHIVE = BASE + "/meio-ambiente"
MAX_PAGES = 250
MAX_WINDOW_DAYS = 180
CARD_DATE = re.compile(r"\b(\d{2})/(\d{2})/(20\d{2})\s*-\s*(\d{1,2}):(\d{2})\b")
PUBLISHED = re.compile(r"\bPublicado\s+em\s+(\d{2})/(\d{2})/(20\d{2})\s*-\s*(\d{1,2}):(\d{2})\b", re.I)
ARTICLE_PATH = re.compile(r"^/[^/]+/noticia/20\d{2}-\d{2}/[^/]+/?$")

# Listing publication dates are independent evidence for article headers.
_listing_dates: dict[str, datetime] = {}


def _article_url(raw: str) -> str | None:
    normalized = normalize_url(urljoin(BASE, raw), Config.source_name)
    if not normalized or not ARTICLE_PATH.fullmatch(urlparse(normalized).path):
        return None
    return normalized


def _timestamp(raw: str, pattern: re.Pattern[str]) -> datetime | None:
    match = pattern.search(raw)
    if not match:
        return None
    try:
        day, month, year, hour, minute = map(int, match.groups())
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _scoped_header(soup: BeautifulSoup) -> tuple[str | None, datetime | None, str | None]:
    nodes = soup.select(".node-conteudo")
    if len(nodes) != 1:
        return None, None, None
    node = nodes[0]
    title_node = node.select_one("h1.titulo-materia") or node.select_one("h1")
    date_node = node.select_one(".header-noticia .data") or node.select_one(".data")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    raw_date = clean(date_node.get_text(" ", strip=True)) if date_node else None
    published = _timestamp(raw_date or "", PUBLISHED)
    return title, published, raw_date


def _archive_cards(soup: BeautifulSoup, page: int) -> list[tuple[str, datetime]]:
    cards = soup.select("section.ultimas-noticias .ultima-noticia")
    if not cards:
        raise ValueError(f"environment archive page {page} has no dated news cards")
    rows = []
    for card in cards:
        link = card.select_one("a.capa-noticia[href]") or card.select_one("a.titulo-noticia[href]")
        url = _article_url(link.get("href", "")) if link else None
        date_node = card.select_one(".data-publicacao .data")
        raw = clean(date_node.get_text(" ", strip=True)) if date_node else None
        published = _timestamp(raw or "", CARD_DATE)
        if not url or not published:
            raise ValueError(f"environment archive page {page} has a malformed article card: {raw!r}")
        rows.append((url, published))
    if len({url for url, _ in rows}) != len(rows):
        raise ValueError(f"environment archive page {page} repeats an article card")
    if any(rows[index][1] < rows[index + 1][1] for index in range(len(rows) - 1)):
        raise ValueError(f"environment archive page {page} has a publication-date inversion")
    return rows


def _featured(soup: BeautifulSoup) -> list[str]:
    urls = []
    for link in soup.select("section.destaques-pagina a[href]"):
        url = _article_url(link.get("href", ""))
        if url and url not in urls:
            urls.append(url)
    return urls


def _page_link(soup: BeautifulSoup, next_page: int) -> bool:
    return any(
        urlparse(urljoin(BASE, link.get("href", ""))).path == "/meio-ambiente"
        and urlparse(urljoin(BASE, link.get("href", ""))).query == f"page={next_page}"
        for link in soup.select("a[href]")
        if "Ver mais" in link.get_text(" ", strip=True)
    )


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError(f"invalid or excessive Agência Brasil window (max {MAX_WINDOW_DAYS + 1} days)")
    _listing_dates.clear()
    evidence.update({
        "method": "environment_archive_page_chain_with_featured_header_checks",
        "archive": ARCHIVE,
        "coverage_complete": False,
        "coverage_scope": "publisher_environment_archive_cards_and_featured_articles",
        "archive_window_complete": False,
        "coverage_blocker": "lower_boundary_not_yet_reached",
        "pages": [],
        "featured": [],
    })
    selected: list[str] = []
    seen = set()
    seen_cards = set()
    previous_oldest: datetime | None = None
    lower_reached = False
    for page in range(MAX_PAGES):
        page_url = ARCHIVE if page == 0 else f"{ARCHIVE}?page={page}"
        response = fetch(page_url)
        soup = BeautifulSoup(response.content, "html.parser")
        cards = _archive_cards(soup, page)
        if previous_oldest and cards[0][1] > previous_oldest:
            raise ValueError(f"environment archive page {page} crosses a date-order boundary")
        previous_oldest = cards[-1][1]
        if page == 0:
            featured = _featured(soup)
            if not featured:
                raise ValueError("environment archive page zero has no featured article links")
            for url in featured:
                article_response = fetch(url)
                _, published, raw = _scoped_header(BeautifulSoup(article_response.content, "html.parser"))
                if not published:
                    raise ValueError(f"featured article lacks scoped original publication date: {url} ({raw!r})")
                evidence["featured"].append({"url": url, "date_publish": published.isoformat(),
                                             "page_sha256": hashlib.sha256(article_response.content).hexdigest()})
                if start <= published.date() <= end and url not in seen:
                    selected.append(url)
                    seen.add(url)
                    _listing_dates[url] = published
        all_section_urls = {
            url for link in soup.select(".container-ultimas-noticias a[href]")
            if (url := _article_url(link.get("href", "")))
        }
        accounted = {url for url, _ in cards} | (set(featured) if page == 0 else set())
        unaccounted = sorted(all_section_urls - accounted)
        if unaccounted:
            raise ValueError(f"environment archive page {page} has unaccounted article links: {unaccounted[:3]}")
        duplicates = sorted({url for url, _ in cards if url in seen_cards})
        if duplicates:
            raise ValueError(f"environment archive shifted or repeated URLs on page {page}: {duplicates[:3]}")
        for url, published in cards:
            seen_cards.add(url)
            if url in _listing_dates and _listing_dates[url].date() != published.date():
                raise ValueError(f"featured/card publication dates disagree: {url}")
            _listing_dates[url] = published
            if start <= published.date() <= end and url not in seen:
                selected.append(url)
                seen.add(url)
        has_next = _page_link(soup, page + 1)
        evidence["pages"].append({
            "url": page_url, "sha256": hashlib.sha256(response.content).hexdigest(),
            "bytes": len(response.content), "cards": len(cards),
            "first_date": cards[0][1].isoformat(), "last_date": cards[-1][1].isoformat(),
            "next_link": has_next, "section_links_accounted": len(all_section_urls),
        })
        if cards[-1][1].date() < start:
            lower_reached = True
            break
        if not has_next:
            raise ValueError(f"environment archive page {page} ends before the lower date boundary")
    evidence["selected_count"] = len(selected)
    evidence["lower_boundary_reached"] = lower_reached
    evidence["archive_window_complete"] = lower_reached
    if not lower_reached:
        raise ValueError(f"environment archive exceeded {MAX_PAGES} pages before lower boundary")
    # This is exhaustive for the publisher's environment archive surface: all
    # article links in each page's news container are accounted for, adjacent
    # pages cannot overlap or invert, and traversal continues past the lower
    # date boundary. The scope does not claim coverage of unrelated sections.
    evidence["coverage_complete"] = True
    evidence["coverage_blocker"] = None
    return selected


def _body(soup: BeautifulSoup) -> tuple[str | None, int, list[str]]:
    node = soup.select_one(".node-conteudo .conteudo-noticia")
    if node is None:
        return None, 0, []
    paragraphs = []
    for item in node.select("p, h2, blockquote"):
        if item.find_parent(class_=re.compile(r"noticias-relacionadas|compartilhamento|publicidade")):
            continue
        if item.name == "blockquote" and item.select("p"):
            continue
        raw = clean(item.get_text(" ", strip=True))
        if raw and raw not in paragraphs:
            paragraphs.append(raw)
    return "\n\n".join(paragraphs) or None, len(paragraphs), paragraphs


def scrape_url(url: str) -> dict:
    canonical = _article_url(url)
    if canonical != url:
        raise ValueError(f"not a canonical Agência Brasil article URL: {url}")
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    title, published, date_raw = _scoped_header(soup)
    body, paragraph_count, paragraphs = _body(soup)
    og = soup.select_one('meta[property="og:title"]')
    og_title = clean(og.get("content")) if og else None
    meta = soup.select_one('meta[property="article:published_time"]')
    meta_raw = clean(meta.get("content")) if meta else None
    listed = _listing_dates.get(url)
    errors = []
    if not title:
        errors.append("scoped_title_missing")
    if not published:
        errors.append("scoped_original_publication_missing")
    if not body or len(body) < 200:
        errors.append("scoped_maintext_missing_or_short")
    if listed and published and listed.date() != published.date():
        errors.append("archive_article_publication_date_disagreement")
    if og_title and title and clean(og_title).casefold() != clean(title).casefold():
        errors.append("visible_title_vs_og_title")
    meta_disagreement_minutes = None
    if meta_raw and published:
        try:
            metadata = datetime.fromisoformat(meta_raw.replace("Z", "+00:00")).replace(tzinfo=None)
            meta_disagreement_minutes = int((metadata - published).total_seconds() // 60)
        except ValueError:
            pass
    title_evidence = [{"kind": "visible_h1", "source": ".node-conteudo h1", "raw": title}] if title else []
    if og_title:
        title_evidence.append({"kind": "og_title", "source": 'meta[property="og:title"]', "raw": og_title})
    date_evidence = ([{"kind": "visible_time", "source": ".node-conteudo .data Published em",
                       "raw": published.isoformat()}] if published else [])
    if listed:
        date_evidence.append({"kind": "visible_time", "source": "environment_archive_card_or_featured_header", "raw": listed.isoformat()})
    if meta_raw:
        date_evidence.append({"kind": "metadata_conflict", "source": 'meta[property="article:published_time"]', "raw": meta_raw})
    bodies = ([{"kind": "scoped_visible_paragraphs", "source": ".node-conteudo .conteudo-noticia",
                "text": body[:20000]}] if body else [])
    return {
        "url": url, "source_domain": Config.source_name, "country_code": Config.country_code,
        "title": title, "date_publish": published, "maintext": body,
        "language": "pt", "parser": "pure_adapter", "collection": None,
        "parser_error": ",".join(errors) or None,
        "field_sources": {"title": ".node-conteudo h1", "date_publish": ".node-conteudo .data Publicado em",
                          "maintext": ".node-conteudo .conteudo-noticia"},
        "field_evidence": {
            "titles": title_evidence, "dates": date_evidence, "bodies": bodies,
            "http_status": response.status_code, "visible_publication_raw": date_raw,
            "archive_publication": listed.isoformat() if listed else None,
            "meta_published_raw": meta_raw, "meta_wall_clock_delta_minutes": meta_disagreement_minutes,
            "paragraph_count": paragraph_count, "first_paragraph": paragraphs[0][:200] if paragraphs else None,
            "last_paragraph": paragraphs[-1][:200] if paragraphs else None,
            "meta_is_not_authoritative_publication": True,
        },
    }
