# Scanner qualification

The frozen synthetic matrix is in
[`tests/fixtures/qualification/matrix.json`](../tests/fixtures/qualification/matrix.json).
It retains 19 case IDs and their original observable triggers against the
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
