# Scraping host recovery and capacity policy

Use this procedure before returning an unreachable scraping host to service.
Preserve incident evidence before cleaning files, restarting services, or
launching a scraper.

## Recovery evidence

Capture one bounded, read-only recovery packet:

1. Boot and shutdown history, the previous boot journal, kernel messages, and
   out-of-memory events.
2. SSH service state and authentication failures around the outage window.
3. Filesystem free space, inode use, mount errors, and available device health
   information.
4. Available memory, swap use, normalized load, process counts, open file
   limits, and orphaned browser processes.
5. Sizes and modification times for scraper logs, observation artifacts,
   browser profiles, and caches.
6. The code checkout status, remote, branch, commit, modified files, and
   untracked files. Create a private recovery archive before changing them.
7. A dependency export for every runtime used by the controller, adapters,
   and browser lane.

Record the evidence hashes in the incident report. Do not copy credentials,
raw article data, browser profiles, or database connection strings into a
public repository.

## Capacity states

`automation/ops/host_capacity_guard.py` is the common read-only gate. It
returns one of these recommendations:

| State | Network workers | Browser workers | Action |
|---|---:|---:|---|
| green | 4 | 1 | Admit the next bounded source. |
| reduced | 2 | 0 | Continue network-only work at lower pressure. |
| blocked | 0 | 0 | Do not dispatch another source. |

The default guard blocks when any required metric is unavailable. It also
blocks below 30 GiB or 3% filesystem free, below 8 GiB or 10% available
memory, at normalized five-minute load of 1.5 per CPU, when at least 75% swap
use coincides with less than 16 GiB or 20% available memory, above
12 matching scraper and browser processes, at a 4 GiB individual log, at
50 GiB across configured log and output roots, or below a 1,024 soft file
descriptor limit.

The guard reduces work below 60 GiB or 8% filesystem free, below 16 GiB or
20% available memory, at normalized five-minute load of 0.8 per CPU, at 50%
swap use (including sticky high swap when current memory is healthy), above
six matching processes, at a 1 GiB individual log, at 25 GiB
across configured roots, or below a 4,096 soft file descriptor limit.

Run the guard at controller startup, before dispatching another source, and at
terminal state. These event-driven checks replace continuous polling.

## Retention and process rules

- Permit one controller per host. Use an exclusive host lock and immutable
  attempt marker.
- Give every source a timeout and terminate its entire process group on
  timeout before admitting another source.
- Isolate a source failure so unrelated country sessions continue.
- Rotate text logs at 50 MiB. Keep campaign logs below 2 GiB and configured
  log plus output roots below the capacity thresholds.
- Delete ephemeral browser profiles after a source reaches a terminal state.
  Retain HTML or screenshots only for bounded failures that need review.
- Keep production writes disabled during observation. Promotion remains a
  separate exact-manifest operation with backup and postwrite verification.

## Restore decision

Do not infer an outage cause from an SSH timeout. Classify the cause only when
boot, service, kernel, storage, and process evidence supports it. If the cause
remains unknown, restore with the reduced worker recommendation for the first
campaign and promote the host to green only after a clean terminal receipt.
