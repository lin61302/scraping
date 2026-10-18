from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

from .registry import import_notion, merge_overrides, write_registry
from .reporting import write_inventory_report
from .reconcile import reconcile
from .state import initialize, plan_sources, upsert_sources
from .validation import validate_article


def bootstrap(args) -> None:
    workspace = Path(args.workspace).resolve()
    state_path = Path(args.state).resolve()
    registry_path = Path(args.registry).resolve()
    records = merge_overrides(
        import_notion(workspace),
        Path(args.overrides).resolve() if args.overrides else None,
    )
    write_registry(records, registry_path)
    initialize(state_path)
    count = upsert_sources(state_path, records)
    projects = Counter(record.project for record in records)
    print(json.dumps({"imported": count, "projects": projects, "registry": str(registry_path), "state": str(state_path)}, default=dict, indent=2))


def status(args) -> None:
    import sqlite3
    connection = sqlite3.connect(args.state)
    connection.row_factory = sqlite3.Row
    payload = {
        "sources": connection.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0],
        "by_project": [dict(row) for row in connection.execute("SELECT project, COUNT(*) AS sources, COUNT(DISTINCT country_code) AS countries FROM sources WHERE enabled=1 GROUP BY project")],
        "open_issues": connection.execute("SELECT COUNT(*) FROM issues WHERE status='open'").fetchone()[0],
        "watermarks": [dict(row) for row in connection.execute("SELECT confidence, COUNT(*) AS sources FROM watermarks GROUP BY confidence")],
    }
    print(json.dumps(payload, indent=2))


def plan(args) -> None:
    rows = plan_sources(Path(args.state), args.limit, args.project, args.country)
    for row in rows:
        hints = ",".join(json.loads(row["discovery_hints_json"])) or "unknown"
        print(f"{row['source_id']}\tstale_days={row['stale_days']}\tpriority={row['priority']}\tdiscovery={hints}")


def validate(args) -> None:
    payload = json.loads(Path(args.article).read_text(encoding="utf-8"))
    result = validate_article(payload, date.fromisoformat(args.start), date.fromisoformat(args.end), args.site_name)
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    raise SystemExit(0 if result.eligible else 2)


def inventory_report(args) -> None:
    result = write_inventory_report(Path(args.state), Path(args.output_dir), date.fromisoformat(args.as_of))
    print(json.dumps(result, indent=2))


def reconcile_scripts(args) -> None:
    result = reconcile(Path(args.state), Path(args.manifest), Path(args.output_dir))
    print(json.dumps(result, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="MLP/MLEED scraper control and QA")
    commands = root.add_subparsers(dest="command", required=True)

    command = commands.add_parser("bootstrap", help="Import Notion exports and initialize control state")
    command.add_argument("--workspace", required=True)
    command.add_argument("--state", required=True)
    command.add_argument("--registry", required=True)
    command.add_argument("--overrides", help="Optional explicit source-registry override JSON")
    command.set_defaults(func=bootstrap)

    command = commands.add_parser("status", help="Show control state summary")
    command.add_argument("--state", required=True)
    command.set_defaults(func=status)

    command = commands.add_parser("plan", help="List the stalest enabled sources")
    command.add_argument("--state", required=True)
    command.add_argument("--limit", type=int, default=25)
    command.add_argument("--project", choices=("mlp", "mleed"))
    command.add_argument("--country")
    command.set_defaults(func=plan)

    command = commands.add_parser("validate-article", help="Validate one normalized article JSON file")
    command.add_argument("--article", required=True)
    command.add_argument("--start", required=True)
    command.add_argument("--end", required=True)
    command.add_argument("--site-name")
    command.set_defaults(func=validate)

    command = commands.add_parser("inventory-report", help="Write initial source and country inventory reports")
    command.add_argument("--state", required=True)
    command.add_argument("--output-dir", required=True)
    command.add_argument("--as-of", default=date.today().isoformat())
    command.set_defaults(func=inventory_report)

    command = commands.add_parser("reconcile-scripts", help="Match registry sources to the sanitized production script manifest")
    command.add_argument("--state", required=True)
    command.add_argument("--manifest", required=True)
    command.add_argument("--output-dir", required=True)
    command.set_defaults(func=reconcile_scripts)
    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
