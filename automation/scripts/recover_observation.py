#!/usr/bin/env python3
"""Retry only parse exceptions from a complete pure-adapter observation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper_control.recover import recover_observation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--source-file", type=Path,
        help="Current adapter path; defaults to source_file in the parent manifest",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--initial-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--backoff-multiplier", type=float, default=2.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=10.0)
    parser.add_argument(
        "--pacing-seconds", type=float, default=0.0,
        help="Delay between completed target URLs; default: no inter-target delay",
    )
    args = parser.parse_args()
    run = recover_observation(
        args.parent_run,
        args.output_root,
        source_file=args.source_file,
        max_attempts=args.max_attempts,
        initial_backoff_seconds=args.initial_backoff_seconds,
        backoff_multiplier=args.backoff_multiplier,
        max_backoff_seconds=args.max_backoff_seconds,
        pacing_seconds=args.pacing_seconds,
    )
    print(run)


if __name__ == "__main__":
    main()
