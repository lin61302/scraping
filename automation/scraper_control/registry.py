from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


DOMAIN_RE = re.compile(r"(?<![\w@])(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9.-]+\.[a-z]{2,})(?:[/\s\](){#?]|$)", re.I)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
META_RE = re.compile(r"^(Date|Date Created|Language|Last update|Next selenium update|Schedule|Status|This update):\s*(.*)$", re.M | re.I)
PAGE_ID_RE = re.compile(r"\s([0-9a-f]{32})\.md$", re.I)
TOP_LEVEL_BULLET_RE = re.compile(r"^-\s+(.+?)\s*$", re.M)


STATUS_MAP = {
    "scraping list": "queued",
    "scraping in progress": "running",
    "scraping done": "scraped",
    "patchtools done": "complete",
    "counts generated": "counts_generated",
    "tcg in progress": "postprocessing",
    "tcg in process": "postprocessing",
    "env pipeline": "external_pipeline",
    "env gemini": "external_pipeline",
    "": "unknown",
}


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    project: str
    country_code: str
    country_name: str
    domain: str
    languages: List[str]
    normalized_status: str
    raw_status: str
    last_update: Optional[str]
    schedule: Optional[str]
    next_selenium_update: Optional[str]
    notion_page_id: Optional[str]
    note_path: str
    discovery_hints: List[str]
    access_hints: List[str]
    note_excerpt: str


def _iso_date(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _country(first_heading: str, project: str) -> Tuple[str, str]:
    clean = first_heading.replace("\\_", "_")
    clean = re.sub(r"[*`]", "", clean).strip()
    match = re.search(r"\b(?:ENV[_ -])?([A-Z]{3})\b", clean)
    if match:
        code = match.group(1)
        name = re.sub(r"^(?:ENV[_ -])?" + re.escape(code) + r"\s*:?\s*", "", clean).strip(" -:")
        return code, name or code
    lowered = clean.lower()
    if "international" in lowered:
        return "INT", "International"
    if "regional" in lowered or "environmental sources" in lowered:
        return "REG", "Regional"
    digest = hashlib.sha1((project + ":" + clean).encode()).hexdigest()[:6].upper()
    return "UNK" + digest, clean or "Unknown"


def _domain(heading: str) -> Optional[str]:
    clean = re.sub(r"[*_`~]", "", heading)
    match = DOMAIN_RE.search(clean + " ")
    if not match:
        return None
    domain = match.group(1).lower().rstrip(".")
    if domain.endswith(".csv"):
        domain = domain[:-4]
    return domain[4:] if domain.startswith("www.") else domain


def _hints(section: str) -> Tuple[List[str], List[str]]:
    low = section.lower()
    discovery = []
    for term, label in (
        ("sitemap", "sitemap"),
        ("archive", "archive"),
        ("category", "category"),
        ("categor", "category"),
        ("keyword", "keyword_search"),
        ("load more", "load_more"),
        ("selenium", "selenium"),
        ("pagination", "pagination"),
    ):
        if term in low and label not in discovery:
            discovery.append(label)
    access = []
    for term, label in (
        ("cloudflare", "cloudflare"),
        ("blocked", "blocked_history"),
        ("503", "http_503"),
        ("cloudscraper", "cloudscraper"),
        ("ssl", "tls"),
    ):
        if term in low:
            access.append(label)
    return discovery, access


def _page_records(path: Path, project: str, workspace: Path) -> Iterable[SourceRecord]:
    text = path.read_text(encoding="utf-8", errors="replace")
    headings = list(HEADING_RE.finditer(text))
    if not headings:
        return
    country_code, country_name = _country(headings[0].group(2), project)
    # Country pages use H1 source headings. The MLP international and regional
    # pages instead use top-level bullets. Lower headings inside reports often
    # contain domains as examples and must not become registry records.
    candidates = [
        (heading.start(), heading.end(), domain)
        for heading in headings[1:]
        for domain in [_domain(heading.group(2))]
        if len(heading.group(1)) == 1 and domain
    ]
    if country_code in ("INT", "REG"):
        candidates.extend(
            (item.start(), item.end(), domain)
            for item in TOP_LEVEL_BULLET_RE.finditer(text)
            for domain in [_domain(item.group(1))]
            if domain
        )
    candidates.sort(key=lambda item: item[0])
    first_source_start = candidates[0][0] if candidates else len(text)
    meta: Dict[str, str] = {k.lower(): v.strip() for k, v in META_RE.findall(text[:first_source_start])}
    raw_status = meta.get("status", "")
    languages = [item.strip() for item in meta.get("language", "").split(",") if item.strip()]
    page_id_match = PAGE_ID_RE.search(path.name)
    page_id = page_id_match.group(1) if page_id_match else None

    for index, (start, content_start, domain) in enumerate(candidates):
        end = candidates[index + 1][0] if index + 1 < len(candidates) else len(text)
        section = text[content_start:end]
        discovery, access = _hints(section)
        excerpt = re.sub(r"!\[[^]]*]\([^)]*\)", "", section)
        excerpt = re.sub(r"\s+", " ", excerpt).strip()[:1200]
        source_id = f"{project}:{country_code}:{domain}"
        yield SourceRecord(
            source_id=source_id,
            project=project,
            country_code=country_code,
            country_name=country_name,
            domain=domain,
            languages=languages,
            normalized_status=STATUS_MAP.get(raw_status.lower(), "unknown"),
            raw_status=raw_status,
            last_update=_iso_date(meta.get("last update", "")),
            schedule=meta.get("schedule") or None,
            next_selenium_update=_iso_date(meta.get("next selenium update", "")),
            notion_page_id=page_id,
            note_path=str(path.relative_to(workspace)),
            discovery_hints=discovery,
            access_hints=access,
            note_excerpt=excerpt,
        )


def import_notion(workspace: Path) -> List[SourceRecord]:
    mappings = (
        (workspace / "MLEED_extra_sources_notion", "mlp"),
        (workspace / "MLP_sources_notion", "mleed"),
    )
    records: Dict[str, SourceRecord] = {}
    for folder, project in mappings:
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.md")):
            for record in _page_records(path, project, workspace) or ():
                # Keep separate country entries, but collapse duplicate headings in a page.
                records[record.source_id] = record
    return sorted(records.values(), key=lambda r: (r.project, r.country_code, r.domain))


def write_registry(records: Iterable[SourceRecord], output: Path) -> None:
    rows = [asdict(record) for record in records]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"version": 1, "sources": rows}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_registry(path: Path) -> List[SourceRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [SourceRecord(**row) for row in payload["sources"]]


def merge_overrides(records: Iterable[SourceRecord], path: Optional[Path]) -> List[SourceRecord]:
    """Merge explicit registry rows that Notion cannot yet represent cleanly."""

    result = {record.source_id: record for record in records}
    if path is None:
        return sorted(result.values(), key=lambda r: (r.project, r.country_code, r.domain))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("sources"), list):
        raise ValueError("registry override file must contain version 1 and a sources list")
    for raw in payload["sources"]:
        record = SourceRecord(**raw)
        expected = f"{record.project}:{record.country_code}:{record.domain}"
        if record.source_id != expected:
            raise ValueError(f"registry override source_id mismatch: {record.source_id} != {expected}")
        result[record.source_id] = record
    return sorted(result.values(), key=lambda r: (r.project, r.country_code, r.domain))
