from __future__ import annotations

"""CLI for transaction-safe legacy article cleanup on the framework database."""

import argparse
import json
import sys
from pathlib import Path

from scraper_control.legacy_cleanup import (
    CleanupCommitError,
    CleanupManifestError,
    audit_exit_code,
    execute_cleanup,
    load_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run or commit an exact, backed-up legacy row cleanup manifest"
    )
    parser.add_argument("--framework-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--backup", required=True, help="New JSONL path used only for --commit")
    parser.add_argument("--audit", required=True, help="New JSON audit output path")
    parser.add_argument("--commit", action="store_true", help="Apply inside a MongoDB transaction")
    parser.add_argument(
        "--standalone-recoverable", action="store_true",
        help="For standalone MongoDB: quarantine and verify each full row before guarded deletion, with resumable journal",
    )
    args = parser.parse_args()

    try:
        manifest = load_manifest(Path(args.manifest))
    except (OSError, CleanupManifestError) as exc:
        print(json.dumps({"status": "invalid_manifest", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)

    framework_root = Path(args.framework_root).resolve()
    sys.path.insert(0, str(framework_root))
    from core.base_pipeline import MONGO_DB, MONGO_URI, MongoClient

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=20000)
    try:
        try:
            audit = execute_cleanup(
                client[MONGO_DB],
                manifest,
                backup_path=Path(args.backup),
                audit_path=Path(args.audit),
                commit=args.commit,
                standalone_recoverable=args.standalone_recoverable,
            )
        except (CleanupCommitError, OSError) as exc:
            print(
                json.dumps({"status": "blocked_before_cleanup", "error": str(exc)}),
                file=sys.stderr,
            )
            raise SystemExit(2)
    finally:
        client.close()
    print(json.dumps(audit, indent=2, ensure_ascii=False))
    raise SystemExit(audit_exit_code(audit))


if __name__ == "__main__":
    main()
