from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .registry import SourceRecord


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    country_code TEXT NOT NULL,
    country_name TEXT NOT NULL,
    domain TEXT NOT NULL,
    languages_json TEXT NOT NULL,
    normalized_status TEXT NOT NULL,
    raw_status TEXT NOT NULL,
    last_update TEXT,
    schedule TEXT,
    next_selenium_update TEXT,
    notion_page_id TEXT,
    note_path TEXT NOT NULL,
    discovery_hints_json TEXT NOT NULL,
    access_hints_json TEXT NOT NULL,
    note_excerpt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    script_path TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(project, country_code, domain)
);

CREATE INDEX IF NOT EXISTS idx_sources_plan ON sources(enabled, priority DESC, last_update);
CREATE INDEX IF NOT EXISTS idx_sources_country ON sources(project, country_code);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_runs (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    passed_count INTEGER NOT NULL DEFAULT 0,
    quarantined_count INTEGER NOT NULL DEFAULT 0,
    committed_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY(run_id, source_id)
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT,
    stage TEXT NOT NULL,
    category TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_open ON issues(status, severity, source_id);

CREATE TABLE IF NOT EXISTS watermarks (
    source_id TEXT PRIMARY KEY REFERENCES sources(source_id),
    discovery_through TEXT,
    parsed_through TEXT,
    validated_through TEXT,
    safe_through TEXT,
    confidence TEXT NOT NULL DEFAULT 'unknown',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    assessed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monthly_counts (
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    year_month TEXT NOT NULL,
    metric TEXT NOT NULL,
    value INTEGER NOT NULL,
    is_partial INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(run_id, source_id, year_month, metric)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize(path: Path) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)


def upsert_sources(path: Path, records: Iterable[SourceRecord]) -> int:
    now = utc_now()
    rows = list(records)
    with connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO sources (
                source_id, project, country_code, country_name, domain,
                languages_json, normalized_status, raw_status, last_update,
                schedule, next_selenium_update, notion_page_id, note_path,
                discovery_hints_json, access_hints_json, note_excerpt, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                country_name=excluded.country_name,
                languages_json=excluded.languages_json,
                normalized_status=excluded.normalized_status,
                raw_status=excluded.raw_status,
                last_update=excluded.last_update,
                schedule=excluded.schedule,
                next_selenium_update=excluded.next_selenium_update,
                notion_page_id=excluded.notion_page_id,
                note_path=excluded.note_path,
                discovery_hints_json=excluded.discovery_hints_json,
                access_hints_json=excluded.access_hints_json,
                note_excerpt=excluded.note_excerpt,
                updated_at=excluded.updated_at
            """,
            [
                (
                    r.source_id, r.project, r.country_code, r.country_name, r.domain,
                    json.dumps(r.languages), r.normalized_status, r.raw_status, r.last_update,
                    r.schedule, r.next_selenium_update, r.notion_page_id, r.note_path,
                    json.dumps(r.discovery_hints), json.dumps(r.access_hints), r.note_excerpt, now,
                )
                for r in rows
            ],
        )
    return len(rows)


def plan_sources(path: Path, limit: int, project: Optional[str] = None, country: Optional[str] = None):
    clauses = ["enabled=1"]
    parameters = []
    if project:
        clauses.append("project=?")
        parameters.append(project)
    if country:
        clauses.append("country_code=?")
        parameters.append(country.upper())
    parameters.append(limit)
    query = f"""
        SELECT source_id, project, country_code, domain, last_update, priority,
               CAST(julianday('now') - julianday(COALESCE(last_update, '1970-01-01')) AS INTEGER) AS stale_days,
               discovery_hints_json, access_hints_json
        FROM sources
        WHERE {' AND '.join(clauses)}
        ORDER BY priority DESC, COALESCE(last_update, '1970-01-01') ASC, source_id ASC
        LIMIT ?
    """
    with connect(path) as connection:
        return connection.execute(query, parameters).fetchall()

