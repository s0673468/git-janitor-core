# Detached operations retain branch ownership

A paused rebase or bisect can detach a worktree's HEAD while retaining the branch
it started from. The worktree listing alone no longer advertises that ownership.
The compare-and-delete path therefore checks every worktree's qualified Git
directory for `rebase-merge/head-name`, `rebase-apply/head-name` and `BISECT_START`.
Either the short branch name or its full local ref holds cleanup.

Unreadable Git directories, malformed markers, aliases and special files do not
prove that cleanup is safe. State is read through directory descriptors without
following aliases or blocking on a FIFO. The ordinary `git branch -d` path keeps
Git's own branch safety checks.

`pr-shepherd finish` checks operation ownership before removing the task worktree
and again immediately before deleting its branch ref. If new detached work starts
after removal, cleanup preserves the ref and does not try to recreate a worktree
for the operation's branch. The delivery ledger records that partial cleanup;
existing source, receipt, ignored-file, index and saved-work checks still apply.
