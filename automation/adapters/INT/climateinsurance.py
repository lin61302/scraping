"""Pure observer for MCII Climate Insurance highlights.

The publisher is a Wix site.  The server-rendered ``/highlights`` page embeds
the currently displayed ``Outputs`` records, including the publisher's
``datePublished`` value and RichContent body.  Discovery repeats that listing
and accepts it only when the ordered inventory is stable and spans both sides
of the requested interval.  Wix sitemap ``lastmod`` values are deliberately
not used as publication dates.

This module has no database imports, credentials, cache mutation, or
production write path.
"""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from adapters.INT.common import (
    clean,
    fetch,
    normalize_url,
    parse_datetime,
    response_sha256,
    url_set_sha256,
)


class Config:
    source_name = "climateinsurance.org"
    country_code = "INT"
    is_env = True


LISTING = "https://www.climateinsurance.org/highlights"
OUTPUTS_MARKER = '"recordsByCollectionId":{"Outputs":'
HIGHLIGHT_PATH = "/highlight/"


def _outputs(response) -> dict:
    """Decode the unescaped Wix Outputs payload from server-rendered HTML."""
    payload = response.text
    offset = payload.find(OUTPUTS_MARKER)
    if offset < 0:
        raise RuntimeError("Climate Insurance page lacks Wix Outputs inventory")
    offset += len(OUTPUTS_MARKER)
    try:
        records, _ = json.JSONDecoder().raw_decode(payload[offset:])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Climate Insurance Wix Outputs inventory is invalid JSON") from exc
    if not isinstance(records, dict) or not records:
        raise RuntimeError("Climate Insurance Wix Outputs inventory is empty")
    return records


def _record_url(record: dict) -> str | None:
    raw = record.get("link-copy-2-of-publications-title")
    if not isinstance(raw, str) or not raw.startswith(HIGHLIGHT_PATH):
        return None
    return normalize_url(urljoin(LISTING, raw), {Config.source_name})


def _highlight_rows(response, *, require_descending: bool = False) -> list[dict]:
    rows = []
    for record_id, record in _outputs(response).items():
        if not isinstance(record, dict) or record.get("generalHighlightOnly") is not True:
            continue
        url = _record_url(record)
        title = clean(record.get("title"))
        date_raw = clean(record.get("datePublished"))
        published = parse_datetime(date_raw)
        if not (url and title and published and date_raw):
            raise RuntimeError(
                "Climate Insurance highlight lacks a canonical URL, title, or publisher date"
            )
        rows.append({
            "record_id": str(record_id),
            "url": url,
            "title": title,
            "date_raw": date_raw,
            "date_publish": published,
            "record": record,
        })
    if not rows:
        raise RuntimeError("Climate Insurance listing has no general highlights")
    if len({row["url"] for row in rows}) != len(rows):
        raise RuntimeError("Climate Insurance listing contains duplicate highlight URLs")
    if require_descending and any(
        rows[index]["date_publish"] < rows[index + 1]["date_publish"]
        for index in range(len(rows) - 1)
    ):
        raise RuntimeError("Climate Insurance listing is not ordered by descending publication date")
    return rows


def _inventory_signature(rows: list[dict]) -> list[tuple[str, str, str, str]]:
    return [
        (row["record_id"], row["url"], row["date_raw"], row["title"])
        for row in rows
    ]


def discover_urls(start: date, end: date, evidence: dict) -> list[str]:
    if start > end or (end - start).days > 180:
        raise ValueError("invalid Climate Insurance window (max 181 days)")

    first_response = fetch(LISTING)
    first_rows = _highlight_rows(first_response, require_descending=True)
    repeat_response = fetch(LISTING)
    repeat_rows = _highlight_rows(repeat_response, require_descending=True)
    stable = _inventory_signature(first_rows) == _inventory_signature(repeat_rows)
    before = any(row["date_publish"].date() < start for row in first_rows)
    after = any(row["date_publish"].date() > end for row in first_rows)
    if not stable:
        raise RuntimeError("Climate Insurance Wix highlight inventory changed between reads")
    if not (before and after):
        raise RuntimeError("Climate Insurance highlight listing did not close both date boundaries")

    selected_rows = [
        row for row in first_rows
        if start <= row["date_publish"].date() <= end
    ]
    selected = [row["url"] for row in selected_rows]
    if not selected:
        raise RuntimeError("Climate Insurance interval contains no publisher-dated highlights")

    evidence.update({
        "method": "stable_wix_outputs_general_highlights_with_publisher_dates",
        "listing": LISTING,
        "listing_response_sha256": response_sha256(first_response),
        "repeat_response_sha256": response_sha256(repeat_response),
        "inventory_stable": stable,
        "inventory_rows": len(first_rows),
        "inventory_order": "publisher datePublished descending",
        "inventory_oldest_publication": first_rows[-1]["date_publish"].date().isoformat(),
        "inventory_newest_publication": first_rows[0]["date_publish"].date().isoformat(),
        "general_highlight_only": True,
        "boundary_before_start": before,
        "boundary_after_end": after,
        "selected": len(selected),
        "selected_rows": [
            {
                "record_id": row["record_id"],
                "url": row["url"],
                "title": row["title"],
                "date_publish": row["date_publish"].date().isoformat(),
            }
            for row in selected_rows
        ],
        "selected_url_set_sha256": url_set_sha256(selected),
        "publication_date_source": "Wix Outputs.datePublished",
        "sitemap_lastmod_used_as_publication_date": False,
        "coverage_complete": True,
    })
    return selected


def _text_parts(value, output: list[str]) -> None:
    if isinstance(value, dict):
        if value.get("type") == "TEXT":
            text_data = value.get("textData")
            text = clean(text_data.get("text")) if isinstance(text_data, dict) else None
            if text:
                output.append(text)
        for child in value.values():
            if isinstance(child, (dict, list)):
                _text_parts(child, output)
    elif isinstance(value, list):
        for child in value:
            _text_parts(child, output)


def _rich_content_body(record: dict) -> tuple[str | None, int]:
    parts: list[str] = []
    _text_parts(record.get("blog"), parts)
    body = clean(" ".join(parts))
    return body, len(parts)


def scrape_url(url: str) -> dict:
    response = fetch(url)
    canonical = normalize_url(response.url, {Config.source_name})
    target = normalize_url(url, {Config.source_name})
    if canonical != target:
        raise RuntimeError("Climate Insurance article redirected outside its canonical highlight URL")

    rows = _highlight_rows(response)
    matches = [row for row in rows if row["url"] == target]
    if len(matches) != 1:
        raise RuntimeError("Climate Insurance article page lacks one matching Wix Outputs record")
    row = matches[0]
    record = row["record"]
    title = row["title"]
    published = row["date_publish"]
    body, text_fragment_count = _rich_content_body(record)

    soup = BeautifulSoup(response.content, "html.parser")
    html_language = clean((soup.html or {}).get("lang") if soup.html else None)
    language = (html_language or "").split("-", 1)[0].lower()
    og = soup.select_one('meta[property="og:title"][content]')
    og_title = clean(og.get("content")) if og else None
    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical_href = normalize_url(canonical_node.get("href"), {Config.source_name}) if canonical_node else None
    if canonical_href != target:
        raise RuntimeError("Climate Insurance page canonical does not match requested highlight URL")

    missing = [
        name for name, value in (
            ("title", title), ("date_publish", published), ("maintext", body), ("language", language)
        ) if not value
    ]
    parser_error = "missing:%s" % ",".join(missing) if missing else None
    if not parser_error and language != "en":
        parser_error = "unexpected_language:%s" % language

    titles = [{
        "kind": "wix_outputs_title",
        "source": "Wix recordsByCollectionId.Outputs.title",
        "raw": title,
    }]
    if og_title and og_title != title:
        titles.append({"kind": "og_title", "source": 'meta[property="og:title"]', "raw": og_title})
    evidence_body = body[:20000] if body else None
    updated = record.get("_updatedDate")
    if isinstance(updated, dict):
        updated = clean(updated.get("$date"))
    else:
        updated = clean(updated)

    return {
        "url": target,
        "source_domain": Config.source_name,
        "country_code": Config.country_code,
        "title": title,
        "date_publish": published,
        "maintext": body,
        "language": language,
        "parser": "climateinsurance_wix_outputs_datepublished_richcontent",
        "collection": None,
        "parser_error": parser_error,
        "field_sources": {
            "title": "Wix recordsByCollectionId.Outputs.title",
            "date_publish": "Wix recordsByCollectionId.Outputs.datePublished",
            "maintext": "Wix recordsByCollectionId.Outputs.blog RichContent TEXT nodes",
            "language": "html[lang]",
        },
        "field_evidence": {
            "http_status": response.status_code,
            "response_url": response.url,
            "response_sha256": response_sha256(response),
            "response_bytes": len(response.content),
            "transport": getattr(response, "scrape_transport", "requests"),
            "record_id": row["record_id"],
            "canonical_url": canonical_href,
            "titles": titles,
            "dates": [{
                "kind": "published",
                "source": "Wix recordsByCollectionId.Outputs.datePublished",
                "raw": row["date_raw"],
            }] + ([{
                "kind": "modified",
                "source": "Wix recordsByCollectionId.Outputs._updatedDate",
                "raw": updated,
            }] if updated else []),
            "bodies": ([{
                "kind": "wix_richcontent_blog",
                "source": "Wix recordsByCollectionId.Outputs.blog",
                "text": evidence_body,
            }] if evidence_body else []),
            "paragraph_count": text_fragment_count,
            "html_language": html_language,
            "sitemap_lastmod_used_as_publication_date": False,
        },
    }
