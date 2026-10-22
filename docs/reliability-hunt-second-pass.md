# Reliability hunt — second pass, 2026-10-22

Scope: generic local parsing, paths, failure visibility and report determinism.
Read README.md; no repository AGENTS.md was present. Excluded #4 and the
findings/residuals in `docs/safety-hunt-2026-10-21.md`. All reproductions used
invented identities and disposable repositories; scans disabled fetching. No
existing test, dependency pin, remote of this checkout, or Git metadata changed.

## Fixed, with failing tests first

1. **Embedded GitHub URL mistaken for remote identity**
   (`src/git_janitor/git.py:175`). A proxy such as
   `https://mirror.example.invalid/https://github.com/fixture-owner/project.git`
   yielded `fixture-owner/project`, potentially directing GitHub operations at
   an identity the remote did not establish. Three proxy/local-path fixtures
   failed before replacing regex search with full matching.
   `tests/test_reliability_hunt.py:46` exercises the scanner, preserves refs,
   and verifies these repositories still scan without errors. This narrows
   GitHub identification, not repository admission. Accepted as before: direct
   HTTPS, SSH and SCP-style GitHub remotes, dotted names, optional `.git`,
   trailing slash and surrounding whitespace (`:63`).

2. **Unicode separators silently hid dirty linked worktrees**
   (`src/git_janitor/git.py:217`). For worktrees named by appending U+0085,
   U+2028 or U+2029 and `side` to the primary path, `splitlines()` truncated
   each path to the primary path. The scanner skipped all three, with no
   errors and no dirty findings. The test at
   `tests/test_reliability_hunt.py:74` first observed zero linked worktrees;
   it now observes all three and their modified files. Split on Git's LF
   delimiter instead. No new refusal: ordinary Unicode, spaces and these
   separator characters remain accepted.

3. **Case-distinct paths made frozen reports nondeterministic**
   (`src/git_janitor/reconciliation.py:432`, `:443`, `:499`, `:901`). Sorting
   path sets by casefold alone left `Task`/`task` and `Repo`/`repo` tied, so
   Python hash seeds changed JSON array order. The test at
   `tests/test_reliability_hunt.py:91` first produced two different reports
   in each of three modes: registered project, unregistered GitHub repo and
   local-only inventory. Twelve fresh processes per mode now agree exactly.
   Use the original path as a secondary sort key. Both case-distinct paths
   remain in the report; no identity, grouping or admission rule changed.

## Checked and rejected

- Ordinary worktree names with spaces and Unicode (`space dir`, `ação`):
  scanner retained paths and dirty files (`src/git_janitor/git.py:221`, `:539`).
- Symlink traversal cycle: terminates at configured depth; children are sorted
  (`src/git_janitor/git.py:158`, `:161`). No traversal redesign needed.
- Pipe in a commit subject: the bounded split preserves the complete subject
  (`src/git_janitor/git.py:435`). Branch-name pipes were already a first-hunt
  residual and were excluded.
- Successful divergence command returning nonnumeric output: records
  `upstream divergence unavailable` rather than silently accepting a count
  (`src/git_janitor/git.py:471`). Existing inspection errors block cleanup
  suggestions (`src/git_janitor/autonomy.py:207`); no new rejection needed.
- Delivery-queue ordering has explicit priority, target, category and identity
  tie breakers (`src/git_janitor/delivery_queue.py:98`); no additional queue
  sorting bug found. Prior deletion-proof and timeout findings were excluded.

## Validation

Installed the existing pinned dev extras into an ignored `.venv` and created
`runtime/test-tmp` for isolated fixture directories.

Affected tests:

```sh
TMPDIR="$PWD/runtime/test-tmp" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_git_parsing test_qualification_git test_reconciliation test_reconciliation_integration test_closeout_facts test_reliability_hunt
```

Result: **64 tests passed**, including all four new tests.

Documented full gate, with Git configuration and temporary files isolated:

```sh
TMPDIR="$PWD/runtime/test-tmp" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 make check PYTHON=.venv/bin/python
```

Ruff passed. The suite ran **464 tests: 18 failures, 61 errors, 16 skips**.
The sandbox rejects receipt ancestors as untrusted and makes the default
merge-lock directory read-only. To establish the baseline, exported the
untouched HEAD source/tests into an ignored temporary directory and ran:

```sh
TMPDIR="$PWD/runtime/test-tmp" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 make -C runtime/hunt-baseline check PYTHON="$PWD/.venv/bin/python"
```

The untouched revision ran **460 tests with the same 18 failures, 61 errors
and 16 skips**; individual failure/error test identities also match exactly.
These environment failures prevent a green full gate here;
receipt validation was not weakened. Temporary baseline files were removed
after comparison. No commit was attempted because `.git` is read-only.
