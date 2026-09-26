# Execution engine

`git-janitor` remains read-only by default. The execution engine is an opt-in
apply layer that consumes `AutomationDecision` objects after a scan and performs
only the narrow mutations described here.

The engine exists to remove hand-run follow-up for proven safe `auto-act`
decisions while keeping the scanner conservative. It does not widen the decision
layer. It re-checks every precondition immediately before acting, writes an
append-only audit record for every attempted action, and refuses high-risk or
unsupported categories.

## Modes

The CLI has 3 execution states:

- default: no execution engine is built and no ledger entries are written
- `--dry-run`: the engine re-checks preconditions and records what it would do,
  but runs no mutating command
- `--apply`: the engine re-checks preconditions, then runs one supported command
  when all gates pass

`--dry-run` and `--apply` are mutually exclusive.

## Dual gate

An action can run only when all of these are true:

- the decision disposition is `auto-act`
- the category is supported by this engine
- the category is present in the configured `apply_categories` allowlist
- the matching action flag in `[actions]` is enabled
- the optional CLI `--apply-categories` filter still includes the category
- fresh precondition checks pass

The CLI filter can only narrow the configured allowlist. It cannot enable a
category that config did not allow.

The matching flags are:

- `auto_fast_forward_default_branch` for `fast-forward-default-branch`
- `auto_delete_merged_branches` for `delete-merged-branch`
- `auto_merge_green_prs` for `merge-green-pr`
- `auto_mark_drafts_ready` for `mark-draft-ready`

The default config has an empty `apply_categories` list and all action flags set
to `false`, so `--apply` executes nothing until a human enables both gates.

## Supported categories

### `fast-forward-default-branch`

Command:

```sh
git pull --ff-only
```

Preconditions:

- worktree is clean
- current branch is the repo default branch
- upstream is resolved
- current branch is behind upstream
- current branch is not ahead of upstream

The engine records `HEAD` before and after the pull.

Rollback hint: inspect the before and after refs; reset only with explicit human
approval if the fast-forward must be undone.

### `delete-merged-branch`

Command:

```sh
git branch -d <branch>
```

Preconditions:

- worktree is clean
- branch name is resolved from the decision
- branch is not current
- branch is not the default branch
- branch is still merged to the default ref
- `unique_commit_count == 0`

The engine uses lowercase `-d`. It never uses `-D`.

Rollback hint: recreate the branch from the recorded before ref if deletion was
wrong.

### `merge-green-pr`

Command:

```sh
gh pr merge <number> --squash --repo <owner/repo>
```

Preconditions:

- `gh pr view` reports the PR is still open
- the PR is not draft
- checks classify as success
- merge state is in `GREEN_MERGE_STATES`
- `pr_risk_reasons` returns no high-risk reasons

The engine records the PR state before and after the command.

Rollback hint: revert the squash merge commit after validating the revert and its tests.

### `mark-draft-ready`

Command:

```sh
gh pr ready <number> --repo <owner/repo>
```

Preconditions:

- `gh pr view` reports the PR is still open
- the PR is still draft
- checks classify as success
- `pr_risk_reasons` returns no high-risk reasons

The engine records the PR state before and after the command.

Rollback hint: convert the PR back to draft in GitHub if it was marked ready too
early.

## Categories never executed

The engine never executes:

- `stale-linked-worktree-cleanup`
- `low-risk-maintainer-fix`
- runner and workflow categories
- any category with disposition `needs-approval`
- any category with disposition `report-blocked`
- any category with disposition `wait`
- any category with disposition `no-op`
- any PR decision that has high-risk reasons

`stale-linked-worktree-cleanup` is a `needs-approval` decision by design. It can
be reported, but it is outside this engine.

Workflow, runner, deploy, sync, provenance, secrets, migration, schema, storage,
background automation, and public/private access categories are out of scope for
execution even if another policy later marks them as `auto-act`.

## TOCTOU handling

The engine treats the decision snapshot as stale. Before each mutation it runs
fresh read-only git or GitHub commands through the shared command seam:

- local git state uses `git_janitor.git.run_command`
- GitHub state uses `gh` commands through the same injected runner
- check rollups use the existing `classify_check_rollup` helper
- PR risk uses the existing `pr_risk_reasons` helper

If any fresh check no longer matches the safe state, the result is `drifted`.
The engine writes a ledger entry and runs no mutation.

## Result statuses

Each decision produces one execution result:

- `skipped`: unsupported, non-`auto-act`, disallowed, or config-disabled
- `drifted`: preconditions changed before mutation
- `would-apply`: dry-run mode found the action safe and records the exact command
- `applied`: apply mode ran the command and it returned exit code 0
- `failed`: apply mode ran the command and it returned a non-zero exit code

Batch execution is independent. A skipped, drifted, or failed action does not
stop later decisions from being evaluated.

## Ledger schema

Every attempted action appends one JSON object to the configured JSONL ledger.
The engine never truncates or rewrites the file.

Each entry contains:

- `timestamp`: ISO 8601 UTC timestamp
- `repo`: local repo path or GitHub repo name
- `category`: action category
- `disposition`: original decision disposition
- `mode`: `dry-run` or `apply`
- `command`: exact command list that would run or did run
- `before`: refs, branch state, or PR state captured before the action
- `after`: refs, branch state, or PR state captured after the action, when
  available
- `exit_code`: command exit code, or `null` when no mutating command ran
- `status`: one of the result statuses
- `rollback_hint`: concise human rollback guidance
- `detail`: skip, drift, or failure reason

The ledger path is configured with `ledger_path`. If it is omitted, execution
mode writes to `reports/execution-audit.jsonl`. Tests always use a temporary
path.

## Partial failure

The executor handles each decision independently. It records every result and
continues after failures, because each supported command is scoped to one branch
or one PR.

The final report surfaces the execution results so a human can see what was
skipped, drifted, applied, or failed.
