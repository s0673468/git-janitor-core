# Git Janitor Core — agent instructions

<!-- shared-rules:v1 -->
Shared rules, skills and helper roles load on their own from this machine's
global instructions (source: fleet-infra `agent_policy` and `skills`). This file
holds only what is special about this project. A shared safety or approval rule
wins over this file; for project facts, this file wins.
<!-- /shared-rules -->

## What this is

The public, portable source of `git-janitor`: a conservative Git and GitHub
hygiene scanner plus explicit operator tools (`git-safe-delete`,
`preserve-branch`, `pr-shepherd`, `repo-facts`, `git-closeout-facts`). Python
3.11+, standard library only at runtime. Machine configuration, deployment,
schedules, generated reports and runtime receipts live outside this repository;
`docs/export-boundary.md` lists what the export leaves out.

## How to run and check it

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make check PYTHON=.venv/bin/python        # ruff + offline unittest suite
make lint-workflows ACTIONLINT=/absolute/path/to/actionlint
```

- While iterating: `PYTHONPATH=src python3 -m unittest discover -s tests` on the
  affected tests. Hosted CI (`.github/workflows/test.yml`, Python 3.12) runs
  `make check` and is the full required gate; `source-safety.yml` runs a secret
  scan over history and dependency review.
- `make mutation` is deliberately manual and CPU-heavy; it is not the CI gate.
- The suite is offline: synthetic GitHub responses, generated temporary Git
  repositories and invented owners and paths. No credentials are needed.

## What must not break

- **Scanning never mutates.** A scan only reports; it never merges PRs, deletes
  branches or applies recommendations.
- **Every mutation passes all gates:** the action's configuration flag, its
  configured category allowlist, explicit `--apply`, and fresh precondition
  checks. A CLI category filter can only narrow the allowlist; `--dry-run`
  never mutates; every attempt writes an append-only audit record.
- **Operator tools keep their proofs:** branch and PR identity checks, merge
  evidence, exact-head tests, dirty-worktree guards and append-only receipts.
  Preservation verifies the bundle against the inspected commit, uses
  compare-and-swap on the branch, and recovers through `.preserve-journal`.
- **Unclear work is preserved.** Repositories with stashes or submodules are
  blocked from cleanup and deletion; execution refuses to overwrite ignored
  files.
- **No authority from a green result.** A passing proof gate, fixture or green PR
  never grants cleanup or delivery authority. Deployment, credentials and
  publication stay outside automatic execution. Code-review generation stays
  disabled; historical review parsers remain only for compatibility.
- **Safe defaults.** `config.example.toml` keeps empty scope, fetching off,
  every mutation flag false and an empty apply allowlist. There is no personal
  account default: account-wide sweeps need `--owner` or `GIT_JANITOR_OWNER`.
- **Public and portable.** No real account names, repository inventory, machine
  paths, reports, credentials or private instructions in code, fixtures or docs;
  fixtures use invented examples. Never replace an absent private component
  with a successful stub.
- **CI boundary.** Public CI uses standard GitHub-hosted Linux only, with a
  read-only token, no secrets, no self-hosted runner, no artifact uploads and no
  persistent caches. The package never registers a runner or attaches local
  execution to a public repository.

## How it ships

Merging to `main` is the release; there is no package publication or scheduled
runtime here. Private integration and deployed runtimes are maintained
separately.

## Skills this project leans on

- Pull request ready or stuck: `git-pr-babysit`.
- A hosted check failed or hangs: `git-ci-flake-failure-miner`.
- No project-local skills.

## Where to look

- `README.md`: install, operations, configuration, validation.
- `docs/execution-engine.md`: supported operations and excluded categories.
- `docs/preservation-publication.md`, `docs/detached-operation-guard.md`.
- `docs/qualification.md`: the frozen qualification matrix.
- `docs/export-boundary.md`: what this public export excludes.
