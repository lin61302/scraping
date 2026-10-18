"""Record an exact-verified recent interval without bridging a historical gap.

All five evidence files must live under the automation root. The default is a
dry run; ``--commit`` writes one watermark row only after every gate passes.
The strict ``safe_through`` date is preserved, including SQL NULL. A recent
interval can advance ``validated_through`` without certifying older history.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field}_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}_invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field}_invalid")
    return parsed


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field}_invalid")
    return value


def _empty_list(value: object, field: str) -> None:
    if not isinstance(value, list) or value:
        raise ValueError(f"{field}_unresolved")


def _empty_dict(value: object, field: str) -> None:
    if not isinstance(value, dict) or value:
        raise ValueError(f"{field}_unresolved")


def _evidence(root: Path, path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    absolute = path.resolve()
    try:
        relative = absolute.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("evidence_outside_automation_root") from exc
    if not absolute.is_file():
        raise ValueError(f"evidence_missing:{relative}")
    raw = absolute.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"evidence_invalid_json:{relative}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"evidence_not_object:{relative}")
    return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}, payload


def _months_in_window(start: date, end: date) -> list[str]:
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _complete_api_months(discovery: dict[str, Any], key: str, months: list[str]) -> None:
    """Require every requested month and every page claimed by the REST API."""
    rows = discovery.get(key)
    if not isinstance(rows, list) or [row.get("month") for row in rows if isinstance(row, dict)] != months \
            or len(rows) != len(months):
        raise ValueError(f"discovery_{key}_incomplete")
    for row in rows:
        total = _nonnegative_int(row.get("total"), f"discovery_{key}_total")
        pages = row.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError(f"discovery_{key}_pages_incomplete")
        declared_pages = 0 if total == 0 else len(pages)
        if [page.get("page") for page in pages if isinstance(page, dict)] != list(range(1, len(pages) + 1)):
            raise ValueError(f"discovery_{key}_pages_incomplete")
        if any(page.get("total") != total or page.get("pages") != declared_pages for page in pages):
            raise ValueError(f"discovery_{key}_pages_incomplete")
        if sum(_nonnegative_int(page.get("count"), f"discovery_{key}_page_count") for page in pages) != total:
            raise ValueError(f"discovery_{key}_pages_incomplete")


def _empty_discovery_arrays(discovery: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        _empty_list(discovery.get(key), f"discovery_{key}")


def _pure_inventory_complete(domain: str, discovery: dict[str, Any], start: date,
                             end: date, discovered: int) -> None:
    """Check the independent inventory behind a pure adapter's coverage claim."""
    months = _months_in_window(start, end)
    if domain == "infoamazonia.org":
        if discovery.get("method") != "all_indexed_post_sitemaps_plus_counted_pt_en_es_REST_publication_inventory" or \
                discovery.get("sitemap_index") != "https://infoamazonia.org/sitemap_index.xml" or \
                discovery.get("sitemap_lastmod_is_not_publication_date") is not True:
            raise ValueError("discovery_inventory_strategy_mismatch")
        maps = discovery.get("post_sitemaps")
        if not isinstance(maps, list) or not maps:
            raise ValueError("discovery_post_sitemaps_incomplete")
        shard_numbers = []
        map_entries = 0
        for row in maps:
            if not isinstance(row, dict):
                raise ValueError("discovery_post_sitemaps_incomplete")
            match = re.fullmatch(r"https://infoamazonia\.org/post-sitemap(\d*)\.xml", str(row.get("url", "")))
            if not match or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", ""))):
                raise ValueError("discovery_post_sitemaps_incomplete")
            shard_numbers.append(int(match.group(1) or 1))
            entries = _nonnegative_int(row.get("entries"), "discovery_post_sitemap_entries")
            _nonnegative_int(row.get("duplicate_urls"), "discovery_post_sitemap_duplicate_urls")
            if entries == 0 or _nonnegative_int(row.get("bytes"), "discovery_post_sitemap_bytes") == 0:
                raise ValueError("discovery_post_sitemaps_incomplete")
            map_entries += entries
        if shard_numbers != list(range(1, len(shard_numbers) + 1)):
            raise ValueError("discovery_post_sitemaps_incomplete")
        dated_total = _nonnegative_int(discovery.get("post_sitemap_total_dated_urls"),
                                       "discovery_post_sitemap_total_dated_urls")
        if not discovered <= dated_total <= map_entries or \
                _nonnegative_int(discovery.get("post_sitemap_path_window_urls"),
                                 "discovery_post_sitemap_path_window_urls") != discovered:
            raise ValueError("discovery_post_sitemap_count_mismatch")
        _empty_discovery_arrays(discovery, ("post_sitemap_duplicate_window_urls",
                                            "api_urls_missing_in_sitemap",
                                            "path_dated_sitemap_urls_missing_in_api"))
        languages = discovery.get("api_languages")
        if not isinstance(languages, list) or [row.get("language") for row in languages
                                                    if isinstance(row, dict)] != ["pt", "en", "es"]:
            raise ValueError("discovery_api_languages_incomplete")
        api_window = 0
        for row in languages:
            total = _nonnegative_int(row.get("total_with_boundary_overlap"), "discovery_api_total")
            in_window = _nonnegative_int(row.get("in_window"), "discovery_api_in_window")
            pages = row.get("pages")
            declared_pages = (total + 99) // 100
            if in_window > total or not isinstance(pages, list) or \
                    len(pages) != max(1, declared_pages) or \
                    [page.get("page") for page in pages if isinstance(page, dict)] != list(range(1, len(pages) + 1)):
                raise ValueError("discovery_api_pages_incomplete")
            if any(page.get("total") != total or page.get("pages") != declared_pages or
                   not re.fullmatch(r"[0-9a-f]{64}", str(page.get("sha256", "")))
                   for page in pages) or \
                    sum(_nonnegative_int(page.get("items"), "discovery_api_page_items")
                        for page in pages) != total:
                raise ValueError("discovery_api_pages_incomplete")
            api_window += in_window
        if api_window != discovered or \
                _nonnegative_int(discovery.get("api_window_urls"), "discovery_api_window_urls") != discovered:
            raise ValueError("discovery_api_window_count_mismatch")
        external = discovery.get("external_rest_links_excluded")
        if not isinstance(external, list) or len({row.get("id") for row in external
                                                    if isinstance(row, dict)}) != len(external):
            raise ValueError("discovery_external_rest_links_invalid")
        for row in external:
            if not isinstance(row, dict) or row.get("language") not in {"pt", "en", "es"} or \
                    type(row.get("id")) is not int or not isinstance(row.get("link"), str) or \
                    urlsplit(row["link"]).hostname in {None, "infoamazonia.org", "www.infoamazonia.org"}:
                raise ValueError("discovery_external_rest_links_invalid")
            try:
                datetime.fromisoformat(row["date"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("discovery_external_rest_links_invalid") from exc
    elif domain == "agenciaeconordeste.com.br":
        if discovery.get("discovery") != "publisher_wordpress_posts_api_plus_chronological_ultimas_noticias_listing":
            raise ValueError("discovery_inventory_strategy_mismatch")
        _complete_api_months(discovery, "api_months", months)
        listing = discovery.get("listing_pages")
        if not isinstance(listing, list) or not listing or \
                [page.get("page") for page in listing if isinstance(page, dict)] != list(range(1, len(listing) + 1)):
            raise ValueError("discovery_listing_pages_incomplete")
        if discovery.get("listing_lower_boundary_confirmed") is not True:
            raise ValueError("discovery_listing_boundary_unproven")
        try:
            oldest = datetime.fromisoformat(listing[-1]["oldest"]).date()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("discovery_listing_boundary_unproven") from exc
        if oldest >= start:
            raise ValueError("discovery_listing_boundary_unproven")
        for key in ("api_window_count", "listing_window_count"):
            if _nonnegative_int(discovery.get(key), f"discovery_{key}") != discovered:
                raise ValueError(f"discovery_{key}_mismatch")
        _empty_discovery_arrays(discovery, ("api_missing_from_listing", "listing_missing_from_api",
                                            "api_listing_date_mismatches"))
    elif domain == "amazoniareal.com.br":
        if discovery.get("discovery") != "publisher_posts_and_specials_rest_by_publication_month_plus_dated_archive_and_type_sitemaps":
            raise ValueError("discovery_inventory_strategy_mismatch")
        for key in ("api_months", "special_api_months", "correction_api_months"):
            _complete_api_months(discovery, key, months)
        archives = discovery.get("archive_months")
        if not isinstance(archives, list) or len(archives) != len(months) or \
                [row.get("month") for row in archives if isinstance(row, dict)] != months:
            raise ValueError("discovery_archive_months_incomplete")
        for row in archives:
            _nonnegative_int(row.get("dated_urls"), "discovery_archive_dated_urls")
            if not isinstance(row.get("url"), str) or not row["url"]:
                raise ValueError("discovery_archive_months_incomplete")
        for key in ("post_sitemaps", "special_sitemaps", "correction_sitemaps"):
            rows = discovery.get(key)
            if not isinstance(rows, list) or not rows or any(
                not isinstance(row, dict) or not isinstance(row.get("url"), str) or not row["url"]
                or isinstance(row.get("entries"), bool) or not isinstance(row.get("entries"), int)
                or row["entries"] < 0 for row in rows
            ):
                raise ValueError(f"discovery_{key}_incomplete")
        if discovery.get("sitemap_lastmod_is_not_publication_date") is not True:
            raise ValueError("discovery_publication_date_basis_unproven")
        _empty_discovery_arrays(discovery, (
            "api_archive_missing_from_archive", "archive_missing_from_api", "api_archive_date_mismatches",
            "other_custom_sitemaps_requiring_policy", "api_window_missing_from_sitemap",
            "sitemap_only_in_window", "sitemap_extra_page_failures", "sitemap_unresolved_candidates",
            "archive_only_in_window", "special_api_window_missing_from_sitemap",
            "special_sitemap_only_in_window", "special_sitemap_page_failures",
            "special_sitemap_unresolved_candidates", "correction_api_window_missing_from_sitemap",
            "correction_modified_not_in_api_full_months",
        ))
        correction = discovery.get("correction_policy_exclusion")
        if not isinstance(correction, dict) or correction.get("verified") is not True or \
                correction.get("publisher_rest_type") != "correcao" or \
                not isinstance(correction.get("in_window_urls"), list):
            raise ValueError("discovery_correction_policy_unverified")
        _nonnegative_int(discovery.get("special_api_in_window"), "discovery_special_api_in_window")
    elif domain == "reporterbrasil.org.br":
        if discovery.get("method") != "count_checked_post_rest_plus_all_post_sitemaps_plus_dated_jornalismo_archive":
            raise ValueError("discovery_inventory_strategy_mismatch")
        rest_pages = discovery.get("rest_pages")
        if not isinstance(rest_pages, list) or not rest_pages or \
                [row.get("page") for row in rest_pages if isinstance(row, dict)] != list(range(1, len(rest_pages) + 1)):
            raise ValueError("discovery_rest_pages_incomplete")
        rest_total = _nonnegative_int(discovery.get("rest_window_urls"), "discovery_rest_window_urls")
        if any(row.get("total") != rest_total or row.get("total_pages") != len(rest_pages) or
               not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64
               for row in rest_pages) or sum(_nonnegative_int(row.get("rows"), "discovery_rest_page_rows")
                                         for row in rest_pages) != rest_total:
            raise ValueError("discovery_rest_pages_incomplete")
        archives = discovery.get("archive_pages")
        if not isinstance(archives, list) or not archives or discovery.get("archive_lower_boundary_reached") is not True:
            raise ValueError("discovery_archive_boundary_unproven")
        for number, row in enumerate(archives, 1):
            expected_url = "https://reporterbrasil.org.br/jornalismo/" if number == 1 else \
                f"https://reporterbrasil.org.br/jornalismo/?e-page-f8294ee={number}"
            if not isinstance(row, dict) or row.get("url") != expected_url or \
                    not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64 or \
                    _nonnegative_int(row.get("new_cards"), "discovery_archive_new_cards") == 0 or \
                    _nonnegative_int(row.get("cards"), "discovery_archive_cards") < row["new_cards"]:
                raise ValueError("discovery_archive_pages_incomplete")
        try:
            archive_oldest = date.fromisoformat(archives[-1]["last_new_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("discovery_archive_boundary_unproven") from exc
        if archive_oldest >= start:
            raise ValueError("discovery_archive_boundary_unproven")
        archive_window = _nonnegative_int(discovery.get("archive_window_urls"), "discovery_archive_window_urls")
        if archive_window > rest_total:
            raise ValueError("discovery_archive_rest_count_mismatch")
        maps = discovery.get("post_sitemaps")
        if not isinstance(maps, list) or not maps or len({row.get("url") for row in maps if isinstance(row, dict)}) != len(maps) or \
                any(not isinstance(row, dict) or not isinstance(row.get("url"), str) or
                    not row["url"].startswith("https://reporterbrasil.org.br/post-sitemap") or
                    not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64
                    for row in maps):
            raise ValueError("discovery_post_sitemaps_incomplete")
        if sum(_nonnegative_int(row.get("entries"), "discovery_post_sitemap_entries") for row in maps) != \
                _nonnegative_int(discovery.get("all_post_sitemap_urls"), "discovery_all_post_sitemap_urls"):
            raise ValueError("discovery_post_sitemap_count_mismatch")
        if discovery.get("sitemap_lastmod_is_not_publication_date") is not True or \
                not isinstance(discovery.get("sitemap_index_sha256"), str) or \
                len(discovery["sitemap_index_sha256"]) != 64:
            raise ValueError("discovery_publication_date_basis_unproven")
        selected_urls = discovery.get("selected_url_list")
        exclusions = discovery.get("policy_exclusions")
        if not isinstance(selected_urls, list) or len(selected_urls) != discovered or \
                len(set(selected_urls)) != discovered or discovery.get("selected_urls") != discovered or \
                not isinstance(exclusions, list) or \
                rest_total != discovered + len(exclusions):
            raise ValueError("discovery_inventory_count_mismatch")
        excluded_urls = [row.get("url") for row in exclusions if isinstance(row, dict)]
        if len(excluded_urls) != len(exclusions) or len(set(excluded_urls)) != len(exclusions) or \
                set(selected_urls) & set(excluded_urls) or any(
                    row.get("reason") not in {"annex_or_statement", "note_or_release"}
                    or not isinstance(row.get("categories"), list)
                    for row in exclusions):
            raise ValueError("discovery_policy_exclusions_invalid")
        _empty_discovery_arrays(discovery, ("archive_urls_missing_from_rest",
                                            "archive_rest_date_disagreements",
                                            "sitemap_only_in_window", "sitemap_only_unresolved"))
        checks = discovery.get("sitemap_only_page_checks")
        if not isinstance(checks, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("url"), str)
                or row.get("in_window") is not False
                or not isinstance(row.get("date_publish"), str)
                for row in checks):
            raise ValueError("discovery_sitemap_only_pages_unresolved")
        for row in checks:
            try:
                published = datetime.fromisoformat(row["date_publish"]).date()
            except ValueError as exc:
                raise ValueError("discovery_sitemap_only_pages_unresolved") from exc
            if start <= published <= end:
                raise ValueError("discovery_sitemap_only_in_window")
    elif domain == "apublica.org":
        if discovery.get("method") != "Socioambiental archive reconciled with count-checked REST taxonomy inventory":
            raise ValueError("discovery_inventory_strategy_mismatch")
        term = discovery.get("editorial_term")
        if not isinstance(term, dict) or term.get("slug") != "socioambiental" or \
                discovery.get("archive") != term.get("link"):
            raise ValueError("discovery_editorial_term_mismatch")
        types = discovery.get("editorial_content_types")
        api_types = discovery.get("api_types")
        if not isinstance(types, list) or not types or not isinstance(api_types, list) or \
                len(types) != len(api_types):
            raise ValueError("discovery_api_types_incomplete")
        identities = lambda rows: [(row.get("type"), row.get("rest_namespace"), row.get("rest_base"))
                                   for row in rows if isinstance(row, dict)]
        type_ids = identities(types)
        if len(type_ids) != len(types) or len(set(type_ids)) != len(types) or \
                sorted(type_ids) != sorted(identities(api_types)):
            raise ValueError("discovery_api_types_incomplete")
        in_window_total = 0
        for row in api_types:
            total = _nonnegative_int(row.get("total_with_boundary_overlap"), "discovery_api_total")
            in_window = _nonnegative_int(row.get("in_window"), "discovery_api_in_window")
            pages = row.get("pages")
            if in_window > total or not isinstance(pages, list) or not pages or \
                    [page.get("page") for page in pages if isinstance(page, dict)] != list(range(1, len(pages) + 1)):
                raise ValueError("discovery_api_pages_incomplete")
            if any(page.get("headers_total") != total or page.get("headers_pages") != (0 if total == 0 else len(pages))
                   or not isinstance(page.get("sha256"), str) or len(page["sha256"]) != 64
                   for page in pages) or \
                    sum(_nonnegative_int(page.get("count"), "discovery_api_page_count") for page in pages) != total:
                raise ValueError("discovery_api_pages_incomplete")
            in_window_total += in_window
        if in_window_total != discovered or \
                _nonnegative_int(discovery.get("api_window_urls"), "discovery_api_window_urls") != discovered:
            raise ValueError("discovery_api_window_count_mismatch")
        archive_pages = discovery.get("archive_pages")
        if not isinstance(archive_pages, list) or not archive_pages or \
                discovery.get("archive_lower_boundary_reached") is not True:
            raise ValueError("discovery_archive_boundary_unproven")
        for page in archive_pages:
            if not isinstance(page, dict) or not isinstance(page.get("url"), str) or \
                    not page["url"].startswith(discovery["archive"]) or \
                    not isinstance(page.get("sha256"), str) or len(page["sha256"]) != 64 or \
                    _nonnegative_int(page.get("cards"), "discovery_archive_cards") == 0:
                raise ValueError("discovery_archive_pages_incomplete")
        try:
            oldest = date.fromisoformat(archive_pages[-1]["last_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("discovery_archive_boundary_unproven") from exc
        if oldest >= start:
            raise ValueError("discovery_archive_boundary_unproven")
        if _nonnegative_int(discovery.get("archive_window_urls"), "discovery_archive_window_urls") != discovered:
            raise ValueError("discovery_archive_window_count_mismatch")
        _empty_discovery_arrays(discovery, ("api_urls_missing_from_archive", "archive_urls_missing_from_api"))
    elif domain == "metsul.com":
        if discovery.get("strategy") != "complete_date_bounded_wordpress_api_plus_dated_analyses_listing_plus_all_post_sitemaps" or \
                discovery.get("api_endpoint") != "https://metsul.com/wp-json/wp/v2/posts" or \
                discovery.get("listing_endpoint") != "https://metsul.com/analises/":
            raise ValueError("discovery_inventory_strategy_mismatch")
        pages = discovery.get("api_pages")
        if not isinstance(pages, list) or not pages or \
                [page.get("page") for page in pages if isinstance(page, dict)] != list(range(1, len(pages) + 1)):
            raise ValueError("discovery_api_pages_incomplete")
        expected_after = (start - timedelta(days=1)).isoformat() + "T23:59:59"
        expected_before = (end + timedelta(days=1)).isoformat() + "T00:00:00"
        from urllib.parse import parse_qs, urlparse
        for page in pages:
            url = page.get("url")
            if not isinstance(url, str) or not url.startswith(discovery["api_endpoint"] + "?"):
                raise ValueError("discovery_api_pages_incomplete")
            query = parse_qs(urlparse(url).query)
            if query.get("after") != [expected_after] or query.get("before") != [expected_before] or \
                    query.get("page") != [str(page["page"])]:
                raise ValueError("discovery_api_window_mismatch")
        if any(page.get("total") != discovered or page.get("pages") != len(pages) for page in pages) or \
                sum(_nonnegative_int(page.get("count"), "discovery_api_page_count") for page in pages) != discovered or \
                _nonnegative_int(discovery.get("api_window_count"), "discovery_api_window_count") != discovered or \
                discovery.get("api_first_page_stable") is not True:
            raise ValueError("discovery_api_count_mismatch")
        listing = discovery.get("listing_pages")
        if not isinstance(listing, list) or not listing or \
                [page.get("page") for page in listing if isinstance(page, dict)] != list(range(1, len(listing) + 1)) or \
                discovery.get("listing_lower_boundary_confirmed") is not True:
            raise ValueError("discovery_listing_boundary_unproven")
        try:
            oldest = date.fromisoformat(listing[-1]["oldest"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("discovery_listing_boundary_unproven") from exc
        if oldest >= start:
            raise ValueError("discovery_listing_boundary_unproven")
        maps = discovery.get("post_sitemaps")
        if not isinstance(maps, list) or not maps or len({row.get("url") for row in maps if isinstance(row, dict)}) != len(maps) or \
                any(not isinstance(row, dict) or not isinstance(row.get("url"), str) or
                    not row["url"].startswith("https://metsul.com/post-sitemap") or
                    _nonnegative_int(row.get("entries"), "discovery_sitemap_entries") == 0 for row in maps):
            raise ValueError("discovery_post_sitemaps_incomplete")
        if discovery.get("sitemap_lastmod_is_not_publication_date") is not True or \
                discovery.get("api_missing_from_listing") and \
                discovery.get("listing_partial_but_api_sitemap_reconciled") is not True:
            raise ValueError("discovery_listing_sitemap_reconciliation_unproven")
        _empty_discovery_arrays(discovery, ("listing_missing_from_api", "api_listing_date_mismatches",
                                            "api_missing_from_sitemap", "news_missing_from_api",
                                            "news_missing_from_listing", "sitemap_only_in_window",
                                            "sitemap_extra_unresolved", "sitemap_relevant_duplicates"))
    else:
        raise ValueError("discovery_boundary_unproven")


PURE_INVENTORY_SOURCE_IDS = {
    "mleed:BRA:agenciaeconordeste.com.br", "mleed:BRA:amazoniareal.com.br",
    "mleed:BRA:reporterbrasil.org.br", "mleed:BRA:apublica.org", "mleed:BRA:metsul.com",
    "mleed:BRA:infoamazonia.org",
}


def _validate(
    source_id: str,
    manifest: dict[str, Any],
    review: dict[str, Any],
    promotion: dict[str, Any],
    postcheck: dict[str, Any],
) -> tuple[str, str, str, int]:
    parts = source_id.split(":", 2)
    if len(parts) != 3 or parts[0] not in {"mlp", "mleed"} or not all(parts):
        raise ValueError("source_id_invalid")
    project, country, domain = parts
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id or manifest.get("status") != "observed":
        raise ValueError("manifest_not_observed")
    if (manifest.get("source"), manifest.get("country"), bool(manifest.get("is_env"))) != (
        domain, country, project == "mleed"
    ):
        raise ValueError("manifest_source_identity_mismatch")
    start = _date(manifest.get("requested_start"), "manifest_start")
    end = _date(manifest.get("requested_end"), "manifest_end")
    if start > end:
        raise ValueError("manifest_window_reversed")
    if manifest.get("mode") != "observation_no_production_writes":
        raise ValueError("manifest_mode_invalid")
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("manifest_counts_missing")
    discovered = _nonnegative_int(counts.get("discovered"), "manifest_discovered")
    selected = _nonnegative_int(counts.get("selected"), "manifest_selected")
    passed = _nonnegative_int(counts.get("passed"), "manifest_passed")
    skipped = _nonnegative_int(counts.get("skipped"), "manifest_skipped")
    if _nonnegative_int(counts.get("quarantined"), "manifest_quarantined"):
        raise ValueError("manifest_quarantined")
    if _nonnegative_int(counts.get("exceptions"), "manifest_exceptions"):
        raise ValueError("manifest_exceptions")
    if discovered != selected or selected != passed + skipped:
        raise ValueError("manifest_candidate_accounting_mismatch")

    identity = (run_id, domain, country, start.isoformat(), end.isoformat())
    if tuple(review.get(k) for k in ("run_id", "source", "country", "requested_start", "requested_end")) != identity:
        raise ValueError("review_identity_mismatch")
    if review.get("status") != "ready_for_dry_run":
        raise ValueError("review_not_ready")
    if review.get("safe_through_candidate") != end.isoformat():
        raise ValueError("review_window_not_complete")
    if review.get("manifest_counts") != counts:
        raise ValueError("review_manifest_counts_mismatch")
    if _nonnegative_int(review.get("eligible_in_window"), "review_eligible") != passed:
        raise ValueError("review_eligible_mismatch")
    for key in ("issue_code_counts", "unresolved_by_date"):
        _empty_dict(review.get(key), key)
    for key in ("undated_or_unknown_window_issues", "discovery_or_runtime_failures"):
        _empty_list(review.get(key), key)
    anomalies = review.get("volume_anomalies")
    if not isinstance(anomalies, list):
        raise ValueError("review_volume_anomalies_invalid")
    if anomalies:
        resolved = review.get("resolved_volume_anomalies")
        if not isinstance(resolved, list):
            raise ValueError("review_volume_anomalies_unresolved")
        unresolved_days = {item.get("date") for item in anomalies if isinstance(item, dict)}
        resolved_days = {item.get("day") for item in resolved if isinstance(item, dict)}
        if None in unresolved_days or unresolved_days != resolved_days:
            raise ValueError("review_volume_anomalies_unresolved")
        if any(not item.get("classification") or not item.get("reason") for item in resolved):
            raise ValueError("review_volume_resolution_incomplete")
    discovery = review.get("discovery_evidence")
    if not isinstance(discovery, dict):
        raise ValueError("discovery_boundary_unproven")
    if discovery != manifest.get("discovery_evidence"):
        raise ValueError("review_discovery_evidence_mismatch")
    if discovery.get("coverage_complete") is False:
        raise ValueError("discovery_coverage_incomplete")
    if source_id in PURE_INVENTORY_SOURCE_IDS:
        if discovery.get("coverage_complete") is not True:
            raise ValueError("discovery_boundary_unproven")
        _pure_inventory_complete(domain, discovery, start, end, discovered)
    elif discovery.get("boundary_reached") is not True:
        raise ValueError("discovery_boundary_unproven")
    failures = discovery.get("page_failures", [])
    if not isinstance(failures, list) or failures:
        raise ValueError("discovery_page_failures")
    # Adapters can expose nested sitemap or API fetch failures as well as the
    # generic page_failures list. A complete interval cannot contain either.
    for key in ("sitemap_failures", "api_failures", "fetch_failures", "listing_page_failures"):
        failures = discovery.get(key, [])
        if not isinstance(failures, list) or failures:
            raise ValueError(f"discovery_{key}")
    sitemap = discovery.get("sitemap_evidence")
    if isinstance(sitemap, dict):
        failures = sitemap.get("sitemap_failures", [])
        if not isinstance(failures, list) or failures:
            raise ValueError("discovery_sitemap_failures")

    if promotion.get("observation_run_id") != run_id or promotion.get("source") != domain:
        raise ValueError("promotion_identity_mismatch")
    if promotion.get("mode") != "commit" or promotion.get("through_date") != end.isoformat():
        raise ValueError("promotion_window_or_mode_mismatch")
    _empty_list(promotion.get("errors"), "promotion_errors")
    if _nonnegative_int(promotion.get("rejected"), "promotion_rejected"):
        raise ValueError("promotion_rejected")
    eligible = _nonnegative_int(promotion.get("eligible"), "promotion_eligible")
    new = _nonnegative_int(promotion.get("new"), "promotion_new")
    inserted = _nonnegative_int(promotion.get("inserted"), "promotion_inserted")
    present = _nonnegative_int(promotion.get("already_present"), "promotion_already_present")
    if eligible != passed or eligible != new + present or inserted != new:
        raise ValueError("promotion_accounting_mismatch")
    if _nonnegative_int(promotion.get("selected"), "promotion_selected") != selected:
        raise ValueError("promotion_selected_mismatch")
    exclusions = promotion.get("excluded")
    if not isinstance(exclusions, list) or len(exclusions) != skipped:
        raise ValueError("promotion_exclusions_mismatch")
    if source_id == "mleed:BRA:apublica.org":
        policy = review.get("policy_exclusions")
        if not isinstance(policy, list) or len(policy) != skipped or policy != exclusions or \
                len({item.get("url") for item in policy if isinstance(item, dict)}) != skipped:
            raise ValueError("podcast_policy_exclusions_mismatch")
        for item in policy:
            evidence = item.get("evidence") if isinstance(item, dict) else None
            if not isinstance(item, dict) or item.get("reason") != "excluded_podcast_description_without_transcript" or \
                    not isinstance(item.get("url"), str) or \
                    not item["url"].startswith("https://apublica.org/podcast/") or \
                    not isinstance(evidence, dict) or evidence.get("publisher_url") != item["url"] or \
                    evidence.get("policy_type") != "publisher_podcast_description_without_transcript" or \
                    evidence.get("audio_player_present") is not True or \
                    evidence.get("transcript_markers_present") is not False or \
                    type(evidence.get("paragraph_count")) is not int or \
                    evidence["paragraph_count"] not in (1, 2, 3) or \
                    type(evidence.get("description_characters")) is not int or \
                    evidence["description_characters"] < 100:
                raise ValueError("podcast_policy_exclusion_unverified")
    elif source_id == "mleed:BRA:metsul.com" and (skipped or exclusions or review.get("policy_exclusions")):
        raise ValueError("metsul_unexpected_policy_exclusion")

    if postcheck.get("observation_run_id") != run_id or postcheck.get("source") != domain:
        raise ValueError("postcheck_identity_mismatch")
    expected = _nonnegative_int(postcheck.get("expected_eligible"), "postcheck_expected")
    exact = _nonnegative_int(postcheck.get("exact_match"), "postcheck_exact")
    for key in ("missing_urls", "duplicate_urls", "field_mismatches"):
        _empty_list(postcheck.get(key), f"postcheck_{key}")
    if expected != passed or exact != expected:
        raise ValueError("postcheck_exact_count_mismatch")
    # Apply the canonical verifier's pass predicate after checking its fields
    # explicitly; the predicate alone treats absent issue arrays as empty.
    from scraper_control.postcheck import postcheck_passed

    if not postcheck_passed(postcheck):
        raise ValueError("postcheck_failed")
    return run_id, start.isoformat(), end.isoformat(), passed


def record_segment(
    state_path: Path,
    automation_root: Path,
    source_id: str,
    manifest_path: Path,
    review_path: Path,
    promotion_path: Path,
    postcheck_path: Path,
    historical_gap_reason: str,
    *,
    commit: bool = False,
) -> dict[str, Any]:
    """Validate evidence, then preserve strict safe_through while advancing recent coverage."""
    result: dict[str, Any] = {"mode": "commit" if commit else "dry_run", "source_id": source_id,
                              "ready": False, "committed": False, "errors": []}
    try:
        if not isinstance(historical_gap_reason, str):
            raise ValueError("historical_gap_reason_required")
        reason = historical_gap_reason.strip()
        if len(reason) < 12:
            raise ValueError("historical_gap_reason_required")
        root = automation_root.resolve()
        paths = {"observation_manifest": manifest_path, "resolved_review": review_path,
                 "promotion_commit": promotion_path, "exact_postcheck": postcheck_path}
        evidence: dict[str, dict[str, str]] = {}
        payloads: dict[str, dict[str, Any]] = {}
        for name, path in paths.items():
            evidence[name], payloads[name] = _evidence(root, path)
        run_id, start, end, eligible = _validate(source_id, payloads["observation_manifest"],
                                                  payloads["resolved_review"], payloads["promotion_commit"],
                                                  payloads["exact_postcheck"])
        state = state_path.resolve()
        if not state.is_file():
            raise ValueError("state_missing")
        with sqlite3.connect(state) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE" if commit else "BEGIN")
            registry = connection.execute(
                "SELECT project, country_code, domain FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
            if registry is None or tuple(registry) != tuple(source_id.split(":", 2)):
                raise ValueError("source_not_registered_or_identity_mismatch")
            previous = connection.execute(
                "SELECT safe_through, validated_through, evidence_json, run_id, assessed_at "
                "FROM watermarks WHERE source_id=?", (source_id,)
            ).fetchone()
            strict_safe = previous["safe_through"] if previous else None
            previous_validated = previous["validated_through"] if previous else None
            if strict_safe and _date(strict_safe, "existing_safe_through") > _date(end, "end"):
                raise ValueError("safe_through_regression")
            if previous_validated and _date(previous_validated, "existing_validated_through") > _date(end, "end"):
                raise ValueError("validated_through_regression")
            if strict_safe and _date(strict_safe, "existing_safe_through") >= _date(start, "start") - timedelta(days=1):
                raise ValueError("historical_gap_not_present")
            if previous and previous["run_id"] == run_id and previous_validated == end:
                raise ValueError("segment_already_recorded")
            prior_evidence = {}
            if previous:
                try:
                    prior_evidence = json.loads(previous["evidence_json"] or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("previous_evidence_invalid") from exc
                if not isinstance(prior_evidence, dict):
                    raise ValueError("previous_evidence_invalid")
            history: list[dict[str, Any]] = []
            prior_external_evidence: dict[str, Any] | None = None
            if prior_evidence.get("kind") == "verified_recent_segment_with_historical_gap":
                stored_history = prior_evidence.get("segment_history", [])
                if not isinstance(stored_history, list):
                    raise ValueError("previous_segment_history_invalid")
                history = list(stored_history)
                history.append({
                    key: prior_evidence[key]
                    for key in ("observation_run_id", "validated_interval", "historical_gap", "files")
                    if key in prior_evidence
                })
                prior_external_evidence = prior_evidence.get("prior_external_evidence")
            elif prior_evidence:
                prior_external_evidence = prior_evidence
            historical_gap = {
                "reason": reason,
                "start": (_date(strict_safe, "existing_safe_through") + timedelta(days=1)).isoformat()
                if strict_safe else None,
                "end": (_date(start, "start") - timedelta(days=1)).isoformat(),
            }
            inter_segment_gaps: list[dict[str, str]] = []
            if history:
                # The latest watermark can represent several disjoint verified
                # intervals. Preserve the gap before the *first* one; do not
                # label an already verified earlier segment as missing history.
                intervals: list[tuple[date, date]] = []
                for segment in history:
                    if not isinstance(segment, dict) or not isinstance(segment.get("validated_interval"), dict):
                        raise ValueError("previous_segment_history_invalid")
                    interval = segment["validated_interval"]
                    interval_start = _date(interval.get("start"), "previous_segment_start")
                    interval_end = _date(interval.get("end"), "previous_segment_end")
                    if interval_start > interval_end or (intervals and interval_start <= intervals[-1][1]):
                        raise ValueError("previous_segment_history_invalid")
                    intervals.append((interval_start, interval_end))
                if intervals[-1][1].isoformat() != previous_validated:
                    raise ValueError("previous_segment_history_invalid")
                current_start = _date(start, "start")
                if current_start <= intervals[-1][1]:
                    raise ValueError("segment_window_overlap")
                first_gap = history[0].get("historical_gap")
                expected_gap_start = historical_gap["start"]
                expected_gap_end = (intervals[0][0] - timedelta(days=1)).isoformat()
                if (
                    not isinstance(first_gap, dict)
                    or first_gap.get("start") != expected_gap_start
                    or first_gap.get("end") != expected_gap_end
                    or not isinstance(first_gap.get("reason"), str)
                    or not first_gap["reason"]
                ):
                    raise ValueError("previous_segment_history_invalid")
                historical_gap = dict(first_gap)
                intervals.append((current_start, _date(end, "end")))
                for older, newer in zip(intervals, intervals[1:]):
                    gap_start = older[1] + timedelta(days=1)
                    gap_end = newer[0] - timedelta(days=1)
                    if gap_start <= gap_end:
                        inter_segment_gaps.append({
                            "start": gap_start.isoformat(), "end": gap_end.isoformat(),
                            "reason": "No exact-verified segment covers this interval.",
                        })
            item = {
                "kind": "verified_recent_segment_with_historical_gap",
                "observation_run_id": run_id,
                "validated_interval": {"start": start, "end": end, "eligible": eligible},
                "historical_gap": historical_gap,
                "inter_segment_gaps": inter_segment_gaps,
                "strict_safe_through_preserved": strict_safe,
                "files": evidence,
                "segment_history": history,
                "prior_external_evidence": prior_external_evidence,
                "previous_watermark": (
                    {"run_id": previous["run_id"], "safe_through": strict_safe,
                     "validated_through": previous_validated, "assessed_at": previous["assessed_at"],
                     "evidence_sha256": hashlib.sha256(previous["evidence_json"].encode()).hexdigest()}
                    if previous else None
                ),
            }
            result.update({"ready": True, "run_id": run_id, "validated_interval": item["validated_interval"],
                           "safe_through": strict_safe, "evidence": item})
            if commit:
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """INSERT INTO watermarks
                       (source_id, discovery_through, parsed_through, validated_through,
                        safe_through, confidence, evidence_json, run_id, assessed_at)
                       VALUES (?, ?, ?, ?, ?, 'high', ?, ?, ?)
                       ON CONFLICT(source_id) DO UPDATE SET
                         discovery_through=excluded.discovery_through,
                         parsed_through=excluded.parsed_through,
                         validated_through=excluded.validated_through,
                         safe_through=excluded.safe_through,
                         confidence=excluded.confidence,
                         evidence_json=excluded.evidence_json,
                         run_id=excluded.run_id,
                         assessed_at=excluded.assessed_at""",
                    (source_id, end, end, end, strict_safe,
                     json.dumps(item, ensure_ascii=False, sort_keys=True), run_id, now),
                )
                connection.commit()
                result["committed"] = True
    except (OSError, sqlite3.Error, ValueError) as exc:
        result["ready"] = False
        result["errors"].append(str(exc))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--automation-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--observation-manifest", type=Path, required=True)
    parser.add_argument("--resolved-review", type=Path, required=True)
    parser.add_argument("--promotion-commit", type=Path, required=True)
    parser.add_argument("--exact-postcheck", type=Path, required=True)
    parser.add_argument("--historical-gap-reason", required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = record_segment(args.state, args.automation_root, args.source_id,
                            args.observation_manifest, args.resolved_review,
                            args.promotion_commit, args.exact_postcheck,
                            args.historical_gap_reason, commit=args.commit)
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
