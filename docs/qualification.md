# Scanner qualification

The frozen synthetic matrix is in
[`tests/fixtures/qualification/matrix.json`](../tests/fixtures/qualification/matrix.json).
It retains the original 19 case IDs and their observable triggers against the
standalone export baseline. Contract changes need explicit justification;
add probes instead of replacing a failing example.

Disposable repositories and bare remotes exercise current and noncurrent
unpublished work, local-only merges, divergence, dirty and detached worktrees,
partial refs, failed fetches and inaccessible identity. Mocked APIs exercise
authentication failures, malformed responses, PR head movement, blocked checks
and dependency actors. Fixture writes happen only in temporary directories;
no fixture applies an operator action or contacts GitHub.

Physical checkout matching uses proved directory identity. Case-sensitive and
unavailable paths stay distinct; sharing a Git common directory does not make
two linked checkout directories the same. Canonical reconciliation and saved
plans retain observed spelling and reject genuine duplicate or shared-project
ambiguity. An alias does not create a false missing checkout; an unavailable
identity does not establish an alias.

## GQ20: configured tracking and partial fetches

The additive GQ20 case is frozen against public source
`35fc4e12bf29d4edbbf297478cccea44093135e7`. A single-branch fetch can retain
`branch.feature.remote` and `branch.feature.merge` while its fetch refspec does
not map that branch to any local upstream ref. Configured tracking and resolved
local upstream are separate observations. The scanner never constructs an
`origin/<branch>` substitute or treats missing local refs as remote deletion.

Current, noncurrent and linked branches retain tracking configuration, unique
counts and dirty/untracked work. Configured-but-unmapped tracking is a comparison
coverage gap, rather than a genuinely unconfigured branch. Native `[gone]` means
the local upstream ref is unavailable; remote existence and publication remain
unknown. Config inspection failures stay unknown. Error gates block uncertain
operator decisions, and the queue remains inspection-only. URL-like remote or
invalid merge locations are redacted rather than exposing credentials.

Additive tracking fields default to unknown when old snapshots or fixture models
do not record them. Default ahead/behind values without an available upstream
and complete inspection are not proof of equality or remote publication. Replay
retains the original source gaps; it does not resolve tracking or fetch refs.

## Acceptance

Run the affected Git, API, classification, reconciliation, saved-plan and report
tests. The final repository gate remains `make check`. Fixture success does not
establish current real-repository coverage or delivery.

A real inventory needs the operator's canonical registry, authenticated
collection and fresh fetch evidence. Capture observation time, HEAD/default
SHAs, source gaps and errors. No-fetch results do not prove remote freshness;
collection errors remain partial coverage and CLI exit 3. Independently compare
selected status, divergence and PR heads with actual sources. Missing locations,
moving sources and uncollected paths remain visible rather than disposable.
Keep inventories, credentials, original histories and machine configuration
outside this public distribution.

The deterministic ranked queue links to JSON evidence and retains unknown
ownership, publication and gate receipts. A reported green check, author identity,
branch prefix or ancestor relationship does not establish mutation authority,
inactivity or cleanup permission. Saved-plan filters retain evidence-gap
categories. Inspect and preserve unknowns until the operator supplies authority.

## Offline reproduction

```sh
PYTHONPATH=src python3 -m git_janitor.reproduce /private/path/inventory.json \
  --out /private/path/reproduced.md --json-out /private/path/reproduced.json
```

Input and output paths must be distinct. Replay runs no commands and does not
fetch, authenticate, apply actions or replay execution entries. It retains the
original observation time and source gaps. Use the generating code version when
comparing bytes; reproduction does not refresh evidence. Execution history in a
snapshot is restored as data only.
