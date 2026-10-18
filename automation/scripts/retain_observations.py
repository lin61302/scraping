#!/usr/bin/env python3
from __future__ import annotations

"""Bounded, dry-run-first retention for immutable observations."""

import argparse
import json
from pathlib import Path

from scraper_control.retention import build_plan, execute_plan, plan_as_dict


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    actions = result.add_mutually_exclusive_group()
    actions.add_argument("--compress", action="store_true", help="create and verify deterministic archives; keep sources")
    actions.add_argument("--delete-verified", action="store_true", help="delete only sources with verified archives and receipts")
    result.add_argument("--project-root", type=Path, default=Path("."))
    result.add_argument("--observation-root", type=Path, action="append", default=[])
    result.add_argument("--archive-root", type=Path, default=Path("var/observation-archives"))
    result.add_argument("--reference-root", type=Path, action="append", default=[])
    result.add_argument("--state", type=Path, default=Path("var/control.db"))
    result.add_argument("--min-age-days", type=float, default=7.0)
    result.add_argument("--max-runs", type=int, default=10)
    result.add_argument("--max-input-gb", type=float, default=5.0)
    result.add_argument("--max-reference-mb", type=float, default=512.0)
    result.add_argument("--output", type=Path)
    return result


def _under(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.project_root.resolve()
    mode = "compress" if args.compress else "delete_verified" if args.delete_verified else "plan"
    observation_roots = args.observation_root or [Path("var/observations"), Path("runs/orchestrator")]
    reference_roots = args.reference_root or [Path("config"), Path("reports"), Path("status")]
    plan = build_plan(
        project_root=root,
        observation_roots=[_under(root, item) for item in observation_roots],
        archive_root=_under(root, args.archive_root),
        reference_roots=[_under(root, item) for item in reference_roots],
        state_path=_under(root, args.state),
        mode=mode,
        min_age_days=args.min_age_days,
        max_runs=args.max_runs,
        max_input_bytes=int(args.max_input_gb * 1024**3),
        max_reference_bytes=int(args.max_reference_mb * 1024**2),
    )
    payload = plan_as_dict(plan)
    if mode != "plan":
        payload["results"] = execute_plan(plan)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = _under(root, args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
