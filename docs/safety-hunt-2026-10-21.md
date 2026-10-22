# Cleanup safety hunt — 2026-10-21

Scope: branch/worktree deletion proofs, preservation, fail-closed handling,
default/nested branch names, dry-run versus execute parity. Every finding was
reproduced on generated temporary repositories; regressions live in
`tests/test_cleanup_safety.py`. Line numbers refer to this revision.

## Fixed

1. **`git-safe-delete` deleted the local default branch.** No proof compared
   the target to the default branch; with `HEAD` on a feature branch,
   `git-safe-delete main --execute` passed every proof and ran
   `git branch -d main`. The execution engine and scanner already refuse this.
   Fix: proof `not_default_branch` compares the full name against `origin/HEAD`
   (`safe_delete.py:118-127`). Nested defaults (`origin/release/2026`) match by
   full name. Still accepted: branches that merely resemble it (`main-fix`,
   `feature/main`, `2026` when the default is `release/2026`).

   Follow-up (review): when `origin/HEAD` was missing or its lookup timed out,
   `_default_ref` guessed `origin/main`. In a `master` repository with a stale
   `origin/main` that already contains `master`, every proof passed for
   `master`. Fix: an unreadable `origin/HEAD` now fails `not_default_branch`
   with the reason and the repair command (`safe_delete.py:421-431`). This is
   a new refusal for clones without `origin/HEAD` (e.g. `git init` +
   `git remote add` + `fetch`); one `git remote set-head origin <default>`
   restores normal behaviour. The tool cannot otherwise tell which branch is
   the default, so it cannot prove any target is not the default. Still
   accepted: any repository whose `origin/HEAD` is set, including nested
   defaults and defaults other than `main`.

2. **Failed upstream probe counted as "upstream gone".** Any non-zero exit of
   `git rev-parse --verify --quiet <upstream>` (timeout 124, fatal 128) set
   `upstream_gone=True`, which satisfies `upstream_gone_or_ancestor` and is
   written to the receipt. Fix: only exit 1 (ref missing) proves gone
   (`safe_delete.py:184-186`), matching the scanner (`git.py:459`). Still
   accepted: a configured upstream whose remote-tracking ref is absent.

3. **Malformed merged-PR readback counted as merge proof or crashed.** A
   successful `gh pr list --state merged` returning `[{}]`, `[{"number": 3}]`
   or `mergeCommit: {}` passed `merged_pr_readback_or_ancestry` with no merge
   commit to compare, which also skipped `squash_tree_equivalence`. `null`,
   non-list or non-object entries raised `AttributeError`/`KeyError`, leaving
   no ledger record. Fix: non-list/non-object payloads read as "no readback";
   a readback counts only when it names a merge commit (`safe_delete.py:215-221`,
   `465-497`). This is the merged-list twin of the open-PR fix in #3. Still
   accepted: `mergeCommit: {"oid": sha}` with any extra fields, and a bare-SHA
   `mergeCommit` (the old code intended to accept it but crashed on it). A
   failed `gh` call still falls back to ancestry proof, as before.

4. **`preserve-branch` silently overwrote an earlier preservation.** Bundle
   names flatten `/` to `-` and use the checkout directory name, so
   `feature/x` and `feature-x` (or two repos both named `app`) share one file.
   Re-preserving a rewritten branch also replaced the old history. Deleting a
   branch on the strength of the first bundle would then lose it. Fix: an
   existing bundle is replaced only by the same ref at the same head or at a
   descendant (`safe_delete.py:315-318`, `366-384`). Still accepted: first
   preservation, identical reruns, reruns after the branch fast-forwarded.
   Refused, with the reason in `error`: a different ref in the same file, a
   non-descendant head, an unreadable existing bundle. Naming is unchanged.

   Follow-up (review): two concurrent runs (`feature/x` and `feature-x`) could
   both see no bundle; the later `git bundle create` then replaced the first
   run's bundle, and both returned `ok=True`. Fix: the check, bundle, verify and
   manifest steps run under an exclusive `<name>.preserve-lock` file created
   with `O_EXCL` and removed afterwards (`safe_delete.py:283-298`). A colliding
   run fails with "preservation in progress" instead of waiting. The name
   avoids `<bundle>.lock`, which git uses itself. If a run is killed, the
   stale lock blocks that one preservation name until it is removed; the error
   names the file. Sequential runs behave as before.

5. **Execution engine missed nested default branches.** `_short_ref` kept
   only the last path segment, so with default `origin/release/2026` the
   preflight did not recognise `release/2026` as the default branch and
   wrongly refused an unrelated branch named `2026` (`execute.py:612-614`).
   `decide_branch_cleanup` already blocks the first case upstream
   (`autonomy.py:227`), so this was defence-in-depth only. Fix: strip only the
   `origin/` prefix, as `git.py:301` does.

6. **Orphaned linked worktree reported clean, hiding local-only commits.**
   When a linked worktree's `.git` file is missing, git lists it as
   `prunable`. `git status`/`diff`/`cherry` run inside it then describe the
   enclosing primary checkout. Reproduced: a worktree with one local-only
   commit and an uncommitted file under an ignored `.worktrees/` reported no
   dirt, 0 unique commits, a tree matching the default, and no error.
   `classify.py:155,181` then suppressed branch findings because the branch
   counts as checked out in a linked worktree. Closeout had the same blind
   spot for dirty files. Fix: keep git's `prunable` marker
   (`git.py:27`, `228-229`, `615`). The scanner records an error instead of
   inspecting it (`git.py:525-528`), which blocks automation through
   `repo.errors`; closeout records `worktree-N-prunable` and reports
   `complete=false` (`closeout_facts.py:307-309`). Still inspected as before:
   healthy, locked and detached worktrees. A worktree whose folder is gone was
   already an error.

7. **Squash-path `git branch -D` deleted work its proofs never saw.** Two
   reproduced shapes: (a) a same-name tag shadows the branch, because git
   resolves a bare name as `refs/tags/<n>` before `refs/heads/<n>`
   (`safe_delete.py:116`, `187`, `232`, `500`). With tag `topic` at the
   squash-merged state, branch `topic` one unpushed commit further, a gone
   upstream and a merged-PR readback, every proof passed and
   `git branch -D topic` dropped the extra commit. (b) The branch advanced
   between the proofs and the delete (the gap spans two `gh` calls), and the
   unpinned `-D` deleted the new, unproved head. Fix: the squash path now runs
   `git update-ref -d refs/heads/<branch> <proved sha>` (`safe_delete.py:247-252`),
   the same compare-and-delete the pinned `pr-shepherd` path already used. In
   both shapes git refuses the delete and the ledger records `failed`. The
   one existing fixture expectation that pinned `git branch -D`
   (`tests/fixtures/agent_ops/branch_cleanup_tools.json`, squash case) was
   updated to the new command; no other test changed. Still accepted: every
   squash-merged branch whose head did not move, and the plain `-d` path,
   which is unchanged (git's own merged check still guards it).

   Residuals: in shape (a) the proofs still inspect the tag, so a dry run
   reports `passed` and only the execute step refuses. Fixing that would mean
   switching every proof to `refs/heads/<n>`, which changes many pinned test
   commands; no work can be lost now. Unlike `branch -D`, `update-ref -d` does
   not refuse a branch that someone checks out between proof and delete. That
   worktree's HEAD becomes unborn, but its files stay on disk, its commits stay
   in its reflog, and their tree matches the merge commit.

## Reported, low likelihood, not fixed

These come from a read-only trace that was not reproduced. Each needs a
deliberately odd ref name; no work is lost.

- A local branch or tag literally named `origin/<default>` outranks
  `refs/remotes/origin/<default>` in `rev-parse` (`git.py:291`, `349`). The
  scanner and closeout then measure against it, and `symbolic-ref --short`
  can yield `remotes/origin/main`, which weakens the local-default guard. Harm
  is bounded by `git branch -d`. A fix would use full `refs/remotes/...` names;
  that changes many pinned scanner commands.
- A `|` in a branch name (git allows it) breaks the `for-each-ref` split
  (`git.py:434`). The branch is then misparsed or produces a merge-proof error.
- A merged branch checked out in a *dirty linked* worktree can still get a
  "can be deleted" decision (`autonomy.py:217-246`); git refuses to delete a
  checked-out branch, so only the report is wrong.

## Checked and rejected

- Timeouts generally (`git.py:72-78` → exit 124): every other proof accepts
  only exit 0 (`ancestor`, `diff --quiet`, `status`, `worktree list`), and the
  secret check treats any failure as risk.
- Dirty/active worktrees: `worktree_clean` covers the invoking checkout and
  `branch_not_checked_out_elsewhere` fails closed when `worktree list` fails
  (`safe_delete.py:138-174`). `git branch -d` and `git worktree remove`
  (no `--force`) add git's own refusal (`delivery.py:160`).
- Current branch reported as `heads/<n>` when a tag is ambiguous
  (`safe_delete.py:129-136`): the worktree-list proof still sees it.
- Dry-run versus execute parity: both modes share one preflight/proof report;
  dry-run returns before any mutating call (`execute.py:109-134`,
  `safe_delete.py:60-68`).
- `pr-shepherd finish` (`delivery.py`): resolved non-symlink path under the
  repository's task directory, exact-head CAS delete, ownership rechecks,
  worktree restore on partial failure. No gap found.
- Execution title parsing with `:` in a repo directory name
  (`execute.py:509-514`): a mis-split branch name is not a commit, so the
  ancestry recheck refuses it.
- Scanner `name != default_ref` (`git.py:481`) is always true for local
  `main`, but `classify.py:167` and `autonomy.py:227` protect it by
  `default_branch`. Scanner timeouts and failures record `repo.errors`, which
  block decisions (`autonomy.py:207`, `classify.py:169`). Scanner fallback to
  `origin/<configured default>` when `origin/HEAD` is missing
  (`git.py:347-355`) is verified to exist first. Rejecting that fallback
  would block the common clone-without-`origin/HEAD` shape.
- `backup_restore.py`: integrity, schema and row-count checks fail closed. No
  false match found.
- Preservation bundle path that is a symlink: `git bundle create` writes via
  lock-and-rename, replacing the link rather than its target.

## Test environment note

In this sandbox, `/home` appears owned by uid 65534. Watch-receipt tests that
correctly require trusted owner-only ancestors therefore error (59 before and
after this change, all in `WatchAlertBodyTests`/`PrShepherdWatchTests`). The
gate was run with `HOME` and `TMPDIR` redirected to private ignored folders so
no test touches real user state.
