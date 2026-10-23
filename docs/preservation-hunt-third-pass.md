# Preservation safety hunt — third pass, 2026-10-23

Scope: direct data loss during explicit PR finish, plus branch/history and bundle
checks. Read README.md and both prior hunt notes; no checkout AGENTS.md was
present. No existing tests or fixtures changed. All reproductions used disposable
synthetic repositories and mocked GitHub responses. No live fetches, messages,
checkout Git metadata changes, or fleet checks ran.

## Fixed

1. **Hidden tracked edits were deleted by `pr-shepherd finish`.** Set either
   `git update-index --assume-unchanged file.txt` or `--skip-worktree`, then edit
   the tracked file in an otherwise merged task worktree. Both `git status` and
   non-forced `git worktree remove` trust these index bits. Before the fix,
   finish returned **0**, removed the worktree and deleted its branch, losing
   the local edit. The combined flags behave the same way.

   `delivery.py` now inspects NUL-delimited `git ls-files -v -z` records at each
   task ownership/status check. Skip-worktree or assume-unchanged entries block
   finish without changing the index or exposing file contents. A failed probe
   also blocks. This intentionally includes clean flagged entries: status alone
   cannot prove them clean. Tests cover both bits, their combination, inspection
   timeout, and a hidden edit introduced between cleanup checks.

2. **Default checkout synchronization overwrote ignored local files.** Create
   ignored `notes.txt` in the local default checkout, while the fetched default
   starts tracking that path. Finish's status check omitted ignored files, and
   its `git merge --ff-only` overwrote the local notes. Before the fix, finish
   returned **0** and the local content was replaced with the incoming content.

   The merge now uses `--no-overwrite-ignore`, protecting both existing files
   and files created after the status check. Tests prove refusal preserves file
   bytes, the default HEAD, and task resources. Positive controls retain normal
   synchronization/cleanup and preserve unrelated ignored files during a safe
   sync (the existing primary-checkout cleanup guard then blocks deletion).

Regressions: `tests/test_preservation_hunt3.py` (nine new tests). The initial
focused run exited **1** with eight failures; one positive control already passed.
After the fix, all nine and the 25 existing delivery tests passed (exit **0**).

## Additional findings left for focused follow-ups

- **Shallow bundle can falsely report complete preservation.** A synthetic
  three-commit source cloned locally with `--depth=1 --no-local` produces
  `preserve_branch(...)["ok"] == True`. Even unbundling into a fresh bare repo
  returns **0**, but `git fsck --full --no-reflogs <saved-head>` there exits
  **2** because the parent commit is absent. In-source `bundle verify` does not
  establish independent restoration. Follow-up should validate the entire
  reachable graph in an isolated object database, preserve the previous pair
  on failure, and cover partial/promisor objects without lazy network access.
  Partial clones were not qualified in this pass.

- **Post-squash history with an unchanged tree remains deletable.** A merged
  topic with a gone upstream, followed by a synthetic `--allow-empty` commit,
  still passes `evaluate_delete`: merge tree equality cannot establish that
  the current tip was the PR's merged tip. Ordinary content-changing commits
  are covered by existing refusal tests; this distinct case needs immutable
  merged-head identity proof, including unavailable/malformed head evidence.
  Commit history or metadata can be valuable even when file trees match.

These were reproduced but left out to keep this change focused on two direct
losses of file contents in the same delivery operation. No universal safety
claim is made. Existing ancestry-only deletion still uses `git branch -d`;
its proof-to-delete race and independent upstream check merit a separate audit.

## Other boundaries exercised

The affected suite includes existing checks for gone/rewritten upstreams,
content-changing post-squash commits, detached/unmerged worktrees, dirty/untracked
and ignored files, stashes, submodules, exact-head branch races, and interrupted
bundle publication. Those protections were not relaxed. No UI, application
platform, CI configuration, or dependency pin changed.

## Validation

Affected safety/delivery suite:

```sh
TMPDIR="$PWD/runtime/test-tmp" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_preservation_hunt3 test_delivery test_safety_hunt test_cleanup_safety test_preservation_races test_preservation_failures test_fast_forward_preservation test_cleanup_update_blockers
```

Exit **0**: **116 tests passed**.

Repository full gate:

```sh
TMPDIR="$PWD/runtime/test-tmp" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 make check PYTHON=.venv/bin/python
```

Exit **2**: Ruff passed; unittest ran **559 tests**, reporting **18 failures,
61 errors, 16 skips** (unittest exit **1**). Full output is in the ignored
`runtime/hunt3-check.log`. Receipt ancestor ownership checks and the read-only
default provider-lock directory cause the same environment failures documented
by previous hunts. Neither boundary was weakened.

To verify the baseline, exported untouched HEAD with `git archive` into an
ignored temporary directory and ran:

```sh
TMPDIR="$PWD/runtime/test-tmp" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 make -C runtime/hunt3-baseline check PYTHON="$PWD/.venv/bin/python"
```

Exit **2**: Ruff passed; **550 tests**, the same **18 failures, 61 errors,
16 skips**. All **79** failure/error identities (including subtests) matched
exactly. Baseline output is in `runtime/hunt3-baseline-check.log`; the temporary
source copy was removed after comparison. The full gate is not green in this
sandbox, so these results establish unchanged failures rather than a green gate.

`git diff --check` passed (exit **0**). No commit was attempted: checkout Git
metadata is read-only and the runner owns the commit.
