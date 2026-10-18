from __future__ import annotations

"""Compare every eligible observation field to every matching production row."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scraper_control.postcheck import normalized_datetime, verify_exact_observation


verify = verify_exact_observation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--framework-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run_dir, args.framework_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("source", "expected_eligible", "exact_match")}, ensure_ascii=False))
    if result["missing_urls"] or result["duplicate_urls"] or result["field_mismatches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
