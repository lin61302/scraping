from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Tuple

from .validation import FieldCheck


_SERBIAN_MONTHS = {
    "januar": 1, "januara": 1, "februar": 2, "februara": 2,
    "mart": 3, "marta": 3, "april": 4, "aprila": 4, "maj": 5, "maja": 5,
    "jun": 6, "juna": 6, "jul": 7, "jula": 7, "avgust": 8, "avgusta": 8,
    "septembar": 9, "septembra": 9, "oktobar": 10, "oktobra": 10,
    "novembar": 11, "novembra": 11, "decembar": 12, "decembra": 12,
}


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", value if isinstance(value, str) else "").strip()


def _json_objects(value):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    yield item
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield from _json_objects(item)


def extract_html_evidence(html: str) -> Dict[str, object]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    titles: List[Dict[str, str]] = []
    dates: List[Dict[str, str]] = []
    bodies: List[Dict[str, str]] = []

    for selector, label in (("h1", "visible_h1"), ("meta[property='og:title']", "og_title"), ("meta[name='twitter:title']", "twitter_title")):
        node = soup.select_one(selector)
        if node:
            value = _clean(node.get("content") or node.get_text(" ", strip=True))
            if value:
                titles.append({"kind": label, "raw": value})

    for selector, label, attr in (
        ("meta[property='article:published_time']", "published", "content"),
        ("meta[itemprop='datePublished']", "published", "content"),
        ("meta[property='article:modified_time']", "modified", "content"),
        ("meta[itemprop='dateModified']", "modified", "content"),
    ):
        node = soup.select_one(selector)
        if node and node.get(attr):
            dates.append({"kind": label, "source": selector, "raw": _clean(node.get(attr))})
    for node in soup.find_all("time")[:8]:
        raw = _clean(node.get("datetime") or node.get_text(" ", strip=True))
        if raw:
            dates.append({"kind": "visible_time", "source": "time", "raw": raw})

    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for item in _json_objects(data):
            headline = _clean(item.get("headline"))
            if headline:
                titles.append({"kind": "jsonld_headline", "raw": headline})
            for key, kind in (("datePublished", "published"), ("dateCreated", "created"), ("dateModified", "modified")):
                value = _clean(item.get(key))
                if value:
                    dates.append({"kind": kind, "source": f"jsonld.{key}", "raw": value})
            body = _clean(item.get("articleBody"))
            if body:
                bodies.append({"kind": "jsonld_articleBody", "text": body[:20000]})

    for selector in ("[itemprop='articleBody']", "article", ".entry-content", ".post-content", ".article-content", "main"):
        node = soup.select_one(selector)
        if not node:
            continue
        paragraphs = [_clean(item.get_text(" ", strip=True)) for item in node.find_all("p")]
        text = _clean(" ".join(item for item in paragraphs if item))
        if text:
            bodies.append({"kind": selector, "text": text[:20000]})
    return {"titles": titles, "dates": dates, "bodies": bodies}


def _parse_date(raw: str) -> Optional[date]:
    # dateparser can interpret Serbian `mart` as the MART timezone and silently
    # substitute the current month. Resolve explicit Serbian calendar text
    # before the generic multilingual fallback.
    named = re.search(
        r"\b(\d{1,2})\.?\s+(januar(?:a)?|februar(?:a)?|mart(?:a)?|april(?:a)?|"
        r"maj(?:a)?|jun(?:a)?|jul(?:a)?|avgust(?:a)?|septembar(?:a)?|"
        r"oktobar(?:a)?|novembar(?:a)?|decembar(?:a)?)\s+(20\d{2})\.?",
        raw,
        re.I,
    )
    if named:
        try:
            return date(int(named.group(3)), _SERBIAN_MONTHS[named.group(2).casefold()], int(named.group(1)))
        except (KeyError, ValueError):
            return None
    try:
        import dateparser
        order = "YMD" if re.match(r"^\s*20\d{2}[-/.]", raw) else "DMY"
        parsed = dateparser.parse(
            raw,
            settings={"DATE_ORDER": order, "PREFER_LOCALE_DATE_ORDER": False},
        )
    except Exception:
        parsed = None
    return parsed.date() if parsed else None


def compare_evidence(article: Dict[str, object], evidence: Dict[str, object]) -> List[FieldCheck]:
    checks: List[FieldCheck] = []
    title = _clean(article.get("title"))
    title_signals = [item["raw"] for item in evidence.get("titles", []) if item.get("raw")]
    title_scores = [SequenceMatcher(None, title.casefold(), candidate.casefold()).ratio() for candidate in title_signals] if title else []
    if title_signals:
        best = max(title_scores, default=0.0)
        checks.append(FieldCheck("title", "pass" if best >= 0.78 else "fail", best, [] if best >= 0.78 else ["title_signal_disagreement"], {"best_similarity": best, "signals": title_signals[:8]}))

    parsed_value = article.get("date_publish")
    if isinstance(parsed_value, datetime):
        article_date = parsed_value.date()
    elif isinstance(parsed_value, date):
        article_date = parsed_value
    elif isinstance(parsed_value, str):
        article_date = _parse_date(parsed_value)
    else:
        article_date = None
    published = [
        {**item, "parsed": _parse_date(item["raw"])}
        for item in evidence.get("dates", [])
        if item.get("kind") in ("published", "created", "visible_time") and item.get("raw")
    ]
    published = [item for item in published if item["parsed"]]
    if published and article_date:
        deltas = [abs((article_date - item["parsed"]).days) for item in published]
        best_delta = min(deltas)
        checks.append(FieldCheck("date_publish", "pass" if best_delta <= 1 else "fail", 1.0 if best_delta == 0 else 0.8 if best_delta == 1 else 0.1, [] if best_delta <= 1 else ["date_signal_disagreement"], {"article_date": article_date.isoformat(), "best_delta_days": best_delta, "signals": [{**item, "parsed": item["parsed"].isoformat()} for item in published[:8]]}))
    elif article_date and evidence.get("dates"):
        checks.append(FieldCheck("date_publish", "fail", 0.2, ["publication_signal_missing"], {"article_date": article_date.isoformat(), "signals": evidence.get("dates", [])[:8]}))

    maintext = _clean(article.get("maintext"))
    bodies = [_clean(item.get("text")) for item in evidence.get("bodies", []) if item.get("text")]
    if bodies and maintext:
        article_words = set(re.findall(r"\w+", maintext.casefold()))
        overlaps = []
        for body in bodies:
            body_words = set(re.findall(r"\w+", body.casefold()))
            overlaps.append(len(article_words & body_words) / max(1, min(len(article_words), len(body_words))))
        best_overlap = max(overlaps, default=0.0)
        checks.append(FieldCheck("maintext", "pass" if best_overlap >= 0.55 else "fail", best_overlap, [] if best_overlap >= 0.55 else ["maintext_signal_disagreement"], {"best_word_overlap": best_overlap, "body_signal_count": len(bodies)}))
    return checks


def compare_required_evidence(article: Dict[str, object], evidence: Dict[str, object]) -> List[FieldCheck]:
    """Compare available signals and fail every absent required field signal."""
    checks = compare_evidence(article, evidence)
    checked_fields = {item.field for item in checks}
    for field, key in (("title", "titles"), ("date_publish", "dates"), ("maintext", "bodies")):
        if not evidence.get(key) and field not in checked_fields:
            checks.append(FieldCheck(
                field, "fail", 0.0, [f"{field}_evidence_missing"],
                {"required_signal": key, "available_keys": sorted(evidence)},
            ))
    return checks
