# Autonomous verification and downstream handoff

This policy governs scraping, guarded promotion, Patchtools, pre-TCG, TCG,
tracker reconciliation, and completion reporting. The operator and reviewer
roles belong to agents. Routine source work must not pause for a user to
inspect output, approve a technical repair, confirm a plot, or declare a
source complete.

## Responsibility model

- The operating agent owns execution, evidence collection, primary review,
  exact repair, reconciliation, and publication.
- A separate agent or a separate deterministic audit invocation performs the
  independent review. Independence means a fresh read of pinned artifacts and
  receipts; it is not a second assertion copied from the operating report.
- The user is an observer and project owner, not a verification queue. Notify
  the user after a source or material batch completes, or when an action truly
  depends on access outside the agents' control.
- `Inspected; verification pending`, `needs_review`, and similar legacy states
  identify an agent work queue. They never mean that Zung-Ru must inspect or
  approve the source.
- A current-window authorization record is an internal immutable execution
  artifact. An agent creates it after the gates pass and an independent agent
  or deterministic audit verifies it. It is not a request for user approval.

## Source completion flow

1. Freeze the source scope, coverage window, exact inputs, code hashes,
   dependencies, runtime roots, capacity limits, and `production_writes`
   setting.
2. Run the applicable capacity, integrity, source-eligibility, process,
   privacy, and immutable-marker gates. A failed gate stops only the unsafe
   action; the agent diagnoses it and prepares the narrowest safe correction.
3. Launch once, inspect startup once, and leave healthy bounded work
   unattended.
4. At terminal state, the operating agent verifies receipt hashes, process
   quiescence, exact identity accounting, coverage boundaries, field and
   language checks, exclusions, exceptions, and acceptance criteria.
5. A separate agent or deterministic audit independently reconstructs the
   conclusion from the immutable evidence. The review returns `passed`,
   `shortfall`, or `blocked_external`, with exact reasons and artifact hashes.
6. For `shortfall`, preserve every passing identity and repair only the exact
   unresolved source-local set. Continue automatically through another frozen
   packet when required; do not rerun a full source or wait for user review.
7. For `passed`, record the supported scraping boundary, generate the source
   count time series, update the tracker, and continue through eligible
   downstream stages without a separate prompt.
8. Publish the completion record and plot after the independent audit. The
   public plot is a revisitable audit view, not a pre-completion approval gate.

## Scraping completion evidence

A source may be called scraping-complete only when the evidence includes:

- a terminal receipt bound to the exact plan and code hashes;
- exact selected, passed, excluded, quarantined, and exception accounting;
- coverage-window and boundary proof;
- required title, publication date, maintext, canonical URL, and likely
  language checks, with source-specific policy exceptions classified;
- preserved exact identities for every passing row and every unresolved row;
- a quiescence or isolated-active-work determination;
- a source-level daily or monthly count series with a data hash, coverage
  labels, scrape-stage state, and generation timestamp; and
- a primary review plus an independent review whose conclusions agree.

The plot is generated from reviewed source-level counts. Country aggregation
is optional and must never replace the source-level series.

## Guarded write and downstream invariants

Removing the user from routine verification does not weaken execution gates.

- Observation remains `production_writes=false`.
- Production promotion requires an immutable exact-ID manifest, backup,
  guarded idempotent write, postwrite verification, and receipt-bound counts.
- Patchtools must remain source, country, and time scoped to the verified
  receipt IDs.
- Translation, classifiers, geoparsing, and other pre-TCG stages must emit
  source-scoped receipts and exact input/output accounting.
- No Git commit, plot, run timestamp, startup receipt, or aggregate summary may
  advance a verified boundary without reviewed terminal evidence.
- Interrupted single-use attempts remain closed. The agent prepares a new
  reviewed packet rather than bypassing or removing an immutable marker.

## TCG handoff contract

When a source becomes eligible, the scraping agent sends the TCG agent an
immutable handoff with:

- source ID, country or shared-source group, project, and source domain;
- requested coverage window and reviewed scraping boundary;
- exact collection, document-ID, and canonical-URL set hashes;
- selected, passed, excluded, held, quarantined, and exception counts;
- observation, promotion, postwrite, Patchtools, translation, classifier, and
  geoparsing receipt hashes when applicable;
- language and duplicate-policy decisions;
- source-level count-series data hash and plot status;
- the exact unresolved set, if any, and whether it is ineligible, parked, or
  requires a source-local repair; and
- the next TCG action and its acceptance criteria.

The TCG agent independently verifies the handoff and proceeds automatically
when its gates pass. If a gate fails, it returns the exact missing artifact or
identity set to the scraping agent. Neither agent waits for routine user
confirmation.

## External-action boundary

User action is requested only when the next step cannot be performed by an
agent, such as obtaining publisher permission or a licensed feed, supplying a
credential that the agents do not possess, restoring a physical network link,
or granting required administrative privilege. Technical uncertainty, plot
inspection, normal verification, routine repairs, tracker publication, and
TCG handoff are agent-owned work.

## Reporting rule

Report completed sources and material batches after the evidence is sealed.
Report a blocker only with the exact external action required. Keep routine
agent review states visible in the tracker so the user can revisit them, but
never turn that visibility into a pause or approval dependency.
