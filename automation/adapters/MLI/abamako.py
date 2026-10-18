"""Read-only aBamako observer with an audited numeric article-ID frontier.

The publisher has no usable sitemap and its ``/h/`` listing denies access. Its
article IDs increase with publication, as documented in the Mali handoff and
confirmed on current pages. We enumerate the ID range instead of guessing
URLs from a category page or trusting a fixed high-water ID.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from adapters.MLI.common import clean, parse_datetime


class Config:
    source_name = "news.abamako.com"
    country_code = "MLI"
    is_env = False


BASE = "http://news.abamako.com"
SEED_PATHS = (
    "/", "/rubriques/politique.asp", "/rubriques/economie.asp",
    "/rubriques/sport.asp", "/rubriques/societe.asp",
    "/rubriques/faitsdivers.asp", "/rubriques/artculture.asp",
    "/rubriques/multimedia.asp", "/rubriques/region.asp",
    "/rubriques/afrique.asp", "/rubriques/diaspora.asp",
    "/rubriques/depeches.asp", "/rubriques/parjournaux.asp",
)
ARTICLE_PATH = re.compile(r"^/h/(\d+)\.html$", re.I)
VISIBLE_DATE = re.compile(
    r"\bPubli[ée]\s+le\s+(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+)?"
    r"(\d{1,2}\s+[A-Za-zÀ-ÿ]+\s+20\d{2})\b",
    re.I,
)
BLOCK_SIZE = 64
WORKERS = 4
OLDER_BLOCKS_REQUIRED = 2
LOWER_DATE_PAD_DAYS = 14
UPPER_EMPTY_REQUIRED = 32
UPPER_PROBE_LIMIT = 256
MAX_IDS_PER_RUN = 12000


def _request(url: str) -> requests.Response:
    """GET public publisher HTML; a 404 is an ID outcome, not a retry error."""
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(
                url, timeout=25,
                headers={"User-Agent": "Mozilla/5.0 (compatible; MLP-MLEED-observation/1.0)"},
            )
            if response.status_code in (200, 404):
                return response
            if response.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"publisher returned HTTP {response.status_code} for {url}")
            last_error = RuntimeError(f"publisher returned HTTP {response.status_code}")
        except requests.RequestException as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(attempt + 1)
    raise RuntimeError(f"publisher request failed for {url}: {last_error}")


def _article_url(identifier: int) -> str:
    return f"{BASE}/h/{identifier}.html"


def _article_id(raw: str, base: str = BASE) -> int | None:
    parsed = urlparse(urljoin(base, raw))
    if (parsed.hostname or "").casefold() != Config.source_name:
        return None
    match = ARTICLE_PATH.fullmatch(parsed.path)
    return int(match.group(1)) if match else None


def _published(soup: BeautifulSoup) -> tuple[datetime | None, str | None]:
    # The article-scoped date is distinct from dates in recommendations and
    # quoted story text. Do not use a footer, clock, or modification timestamp.
    node = soup.select_one("#module_10 .FontArticleSource")
    raw = clean(node.get_text(" ", strip=True)) if node else None
    match = VISIBLE_DATE.search(raw or "")
    value = match.group(1) if match else None
    return (parse_datetime(value) if value else None), value


def _probe(identifier: int) -> dict:
    url = _article_url(identifier)
    response = _request(url)
    if response.status_code == 404:
        return {"id": identifier, "status": "http_404"}
    soup = BeautifulSoup(response.content, "html.parser")
    published, _ = _published(soup)
    title = soup.select_one("#module_10 .FontArticleMainTitle")
    return {
        "id": identifier,
        "status": "dated_article" if published else "undated_http_200",
        "date": published.date().isoformat() if published else None,
        "has_visible_title": bool(title and clean(title.get_text(" ", strip=True))),
    }


def _seed_ids(evidence: dict) -> list[int]:
    ids = set()
    evidence["seed_pages"] = []
    for path in SEED_PATHS:
        url = urljoin(BASE, path)
        response = _request(url)
        if response.status_code != 200:
            raise RuntimeError(f"aBamako seed page returned HTTP {response.status_code}: {url}")
        soup = BeautifulSoup(response.content, "html.parser")
        page_ids = {
            identifier for link in soup.select("a[href]")
            if (identifier := _article_id(link.get("href") or "", url)) is not None
        }
        evidence["seed_pages"].append({
            "url": url, "linked_article_ids": len(page_ids),
            "minimum_id": min(page_ids) if page_ids else None,
            "maximum_id": max(page_ids) if page_ids else None,
        })
        ids.update(page_ids)
    if not ids:
        raise RuntimeError("aBamako seed pages contain no article IDs")
    return sorted(ids)


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end:
        raise ValueError("start must not exceed end")
    evidence.update({
        "method": "publisher_numeric_id_enumeration",
        "article_route": BASE + "/h/{id}.html",
        "date_source": "#module_10 .FontArticleSource, Publié le publication label",
        "id_policy": "every ID in the bounded range receives a recorded 200/404 outcome",
        "coverage_complete": False,
        "bounded_id_range_complete": False,
        "coverage_scope": "enumerated_numeric_interval_only",
        "coverage_limitation": (
            "The publisher exposes no exhaustive index. Consecutive 404s and older-date blocks "
            "bound observed IDs but cannot exclude a longer unpublished gap or an out-of-order "
            "publication date beyond the lower boundary."
        ),
        "id_outcomes": [],
        "blocks": [],
    })
    seed_ids = _seed_ids(evidence)
    seed_max = max(seed_ids)
    evidence["seed_maximum_id"] = seed_max

    # A current listing can lag a just-published article. Continue above its
    # largest linked ID until a full consecutive-empty run proves the frontier.
    upper_outcomes = []
    empty_run = 0
    upper_id = seed_max
    for identifier in range(seed_max + 1, seed_max + UPPER_PROBE_LIMIT + 1):
        outcome = _probe(identifier)
        upper_outcomes.append(outcome)
        if outcome["status"] == "http_404":
            empty_run += 1
        else:
            empty_run = 0
            upper_id = identifier
        if empty_run >= UPPER_EMPTY_REQUIRED:
            break
    else:
        raise RuntimeError("aBamako upper ID probe did not reach the consecutive-404 boundary")
    evidence["upper_probe"] = {
        "seed_maximum_id": seed_max, "newest_existing_id": upper_id,
        "last_probed_id": upper_outcomes[-1]["id"],
        "consecutive_http_404": empty_run,
        "outcomes": upper_outcomes,
    }

    lower_threshold = start - timedelta(days=LOWER_DATE_PAD_DAYS)
    selected = {}
    older_blocks = 0
    next_high = upper_id
    inspected = 0
    while next_high >= 1 and inspected < MAX_IDS_PER_RUN:
        next_low = max(1, next_high - BLOCK_SIZE + 1)
        identifiers = list(range(next_low, next_high + 1))
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            outcomes = list(pool.map(_probe, identifiers))
        inspected += len(outcomes)
        evidence["id_outcomes"].extend(outcomes)
        dates = [date.fromisoformat(item["date"]) for item in outcomes if item.get("date")]
        undated = [item["id"] for item in outcomes if item["status"] == "undated_http_200"]
        missing = [item["id"] for item in outcomes if item["status"] == "http_404"]
        block_old = bool(dates and max(dates) < lower_threshold and not undated)
        older_blocks = older_blocks + 1 if block_old else 0
        evidence["blocks"].append({
            "first_id": next_low, "last_id": next_high,
            "dated": len(dates), "undated_ids": undated, "http_404_ids": missing,
            "oldest_date": min(dates).isoformat() if dates else None,
            "newest_date": max(dates).isoformat() if dates else None,
            "entire_block_before_padded_start": block_old,
        })
        for item in outcomes:
            published = date.fromisoformat(item["date"]) if item.get("date") else None
            if item["status"] == "undated_http_200" or published and start <= published <= end:
                selected[item["id"]] = _article_url(item["id"])
        if older_blocks >= OLDER_BLOCKS_REQUIRED:
            break
        next_high = next_low - 1
    else:
        raise RuntimeError("aBamako lower ID scan reached its cap without a dated boundary")
    evidence["lower_boundary"] = {
        "oldest_inspected_id": evidence["blocks"][-1]["first_id"],
        "padded_start": lower_threshold.isoformat(),
        "consecutive_older_blocks": older_blocks,
        "block_size": BLOCK_SIZE,
    }
    evidence["ids_accounted_for"] = len({
        item["id"] for item in [*evidence["id_outcomes"], *upper_outcomes]
    })
    evidence["selected_undated_ids"] = [
        item["id"] for item in evidence["id_outcomes"]
        if item["status"] == "undated_http_200"
    ]
    dated_by_id = sorted(
        (item for item in evidence["id_outcomes"] if item.get("date")),
        key=lambda item: item["id"],
    )
    inversions = [
        {"lower_id": earlier["id"], "lower_date": earlier["date"],
         "higher_id": later["id"], "higher_date": later["date"]}
        for earlier, later in zip(dated_by_id, dated_by_id[1:])
        if later["date"] < earlier["date"]
    ]
    evidence["date_order_audit"] = {
        "dated_ids_checked": len(dated_by_id),
        "adjacent_inversions": len(inversions),
        "inversion_examples": inversions[:25],
        "policy": "diagnostic_only; monotonicity inside observed IDs is not proof outside them",
    }
    evidence["discovered_urls_after_filter"] = len(selected)
    evidence["bounded_id_range_complete"] = True
    return [selected[identifier] for identifier in sorted(selected)]


def scrape_url(url: str) -> dict:
    identifier = _article_id(url)
    if identifier is None:
        raise ValueError(f"not an aBamako article URL: {url}")
    canonical_url = _article_url(identifier)
    response = _request(canonical_url)
    if response.status_code != 200:
        raise RuntimeError(f"aBamako article became HTTP {response.status_code}: {canonical_url}")
    soup = BeautifulSoup(response.content, "html.parser")
    title_node = soup.select_one("#module_10 .FontArticleMainTitle")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else None
    published, raw_date = _published(soup)
    body_node = soup.select_one("span.FullArticleTexte")
    body = clean(body_node.get_text(" ", strip=True)) if body_node else None
    if body:
        body = re.sub(r"\s+Voir moins\s*$", "", body, flags=re.I)
    og_title_node = soup.select_one('meta[property="og:title"][content]')
    og_url_node = soup.select_one('meta[property="og:url"][content]')
    og_title = clean(og_title_node.get("content")) if og_title_node else None
    og_url = clean(og_url_node.get("content")) if og_url_node else None
    canonical_id = _article_id(og_url) if og_url else None
    missing = [name for name, value in (
        ("title", title), ("date_publish", published), ("maintext", body),
    ) if not value]
    parser_error = f"missing:{','.join(missing)}" if missing else None
    if og_url and canonical_id != identifier:
        parser_error = "publisher_canonical_id_mismatch"
    evidence = {
        "http_status": response.status_code,
        "titles": [{"kind": "og_title", "source": 'meta[property="og:title"]', "raw": og_title}] if og_title else (
            [{"kind": "visible_title", "source": "#module_10 .FontArticleMainTitle", "raw": title}] if title else []
        ),
        "dates": [{"kind": "visible_time", "source": "#module_10 .FontArticleSource", "raw": raw_date}] if raw_date else [],
        "bodies": [{"kind": "visible_article_body", "source": "span.FullArticleTexte", "text": body[:20000]}] if body else [],
        "publisher_canonical_url": og_url,
    }
    return {
        "url": canonical_url, "source_domain": Config.source_name,
        "country_code": Config.country_code, "title": title,
        "date_publish": published, "maintext": body,
        "parser": "abamako_visible_title_publication_label_article_span",
        "parser_error": parser_error, "field_evidence": evidence,
    }
