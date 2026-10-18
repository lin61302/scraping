# GitHub scraper provenance

`automation/ops/publish_github_provenance.py` is the only unattended path for
recording reviewed scraper changes and sanitized run outcomes in this archive.
It pushes directly to `lin61302/scraping` on `main` after all gates pass.

## One-time bootstrap

Review and commit this helper, its policy, tests, documentation, and ignore
rules manually. The publisher requires a clean checkout synchronized with
`origin/main`, so it cannot commit its own unreviewed installation.

## Manifest contract

Keep the input manifest and receipt directory outside this Git repository. A
manifest pins each source file by relative path, destination, SHA-256, byte
size, and mode (`0644` or `0755`). It may also contain sanitized `launch` and
`terminal` records. Every run record pins the exact 40-character Git commit,
operation-manifest SHA-256, acceptance-window `from` and `through` dates, and a
bounded nonempty list of local evidence or receipt SHA-256 values. It also
contains identifiers, timestamps, script hashes, production-write state and,
for terminal events, outcomes, exit codes, and approved numeric counts. Run
records never contain article text, URLs, raw logs, machine details,
credentials, or local paths.

Example:

```json
{
  "schema_version": 1,
  "manifest_id": "jam-north-coast-times-20261018",
  "created_at": "2026-10-18T14:15:00Z",
  "files": [
    {
      "source": "pure_adapters/jam_north_coast_times.py",
      "destination": "JAM/north_coast_times.py",
      "sha256": "<64 lowercase hex characters>",
      "size_bytes": 1234,
      "mode": "0644"
    }
  ],
  "run_records": []
}
```

A launch record in `run_records` has this provenance core (terminal records
add `outcome`, `exit_code`, and approved `counts`):

```json
{
  "event": "launch",
  "run_id": "jam-nct-20261018",
  "occurred_at": "2026-10-18T14:15:00Z",
  "source_ids": ["JAM-NCT"],
  "scripts": [{"destination": "JAM/north_coast_times.py", "sha256": "<sha256>"}],
  "production_writes": false,
  "git_commit": "<40 lowercase hex characters>",
  "operation_manifest_sha256": "<sha256>",
  "acceptance_window": {"from": "2026-07-01", "through": "2026-09-30"},
  "evidence_sha256": ["<sha256>"]
}
```

Run from the archive checkout:

```sh
python3 automation/ops/publish_github_provenance.py \
  --manifest /path/outside/repository/publish_manifest.json \
  --source-root /path/to/reviewed/source/root \
  --receipt-dir /path/outside/repository/github_publish_receipts
```

## Fail-closed behavior

The helper requires the authenticated GitHub login `lin61302`, exact
`origin` repository `lin61302/scraping`, branch `main`, and upstream
`origin/main`. It fetches first and permits only a fast-forward. It stages only
manifest destinations, runs the configured offline tests, makes one normal
push, and verifies that the remote branch SHA matches the new commit. It never
uses force push.

Binary files, oversized files, symlinks, path traversal, embedded credentials,
absolute user-home paths, and infrastructure IP addresses are rejected. Raw
receipts, logs, data, backups, browser profiles, canonical workbooks, and notes
are outside the allowlist. A repeated manifest is a no-op when its exact files
and immutable run records are already present.
