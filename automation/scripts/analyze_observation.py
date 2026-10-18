#!/usr/bin/env python3
"""Build a field, coverage, volume, and safe-through review for one observation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scraper_control.review import analyze


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.run_dir, args.baseline, args.output_dir), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
