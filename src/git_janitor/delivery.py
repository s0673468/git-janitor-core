"""Explicit, resumable PR delivery; no scanner/autonomous cleanup entry point."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .git import parse_github_remote, parse_worktree_porcelain, run_command
from .safe_delete import evaluate_delete


class DeliveryBlocked(RuntimeError):
    pass


def finish_delivery(
    *, repo: str, pr: int, worktree: Path, expected_head: str, ledger_path: Path,
    merge_options: dict[str, Any], runner=run_command,
) -> int:
    from .pr_shepherd import append_ledger, guarded_merge

    evidence: dict[str, Any] = {
        "tool": "pr-shepherd finish", "repo": repo, "pr": pr,
        "worktree": str(worktree), "expected_head": expected_head,
        "remote_branch": "preserved",
    }

    def record(status: str, detail: str) -> None:
        if not ledger_path.is_relative_to(worktree):
            append_ledger(ledger_path, {**evidence, "status": status, "detail": detail})
        print(detail)

    def run(args: list[str], cwd: Path | None = None) -> str:
        result = runner(args, cwd, 60)
        if result.returncode:
            raise DeliveryBlocked(result.stderr or f"command failed: {args[:3]}")
        return result.stdout.strip()

    def payload(args: list[str]) -> Any:
        try:
            return json.loads(run(args))
        except (ValueError, TypeError) as exc:
            raise DeliveryBlocked("invalid GitHub readback") from exc

    def read_pr() -> dict[str, Any]:
        value = payload([
            "gh", "pr", "view", str(pr), "--repo", repo, "--json",
            "state,headRefOid,headRefName,baseRefName,mergeCommit,isCrossRepository",
        ])
        if not isinstance(value, dict) or value.get("isCrossRepository") is not False:
            raise DeliveryBlocked("same-repository PR identity unproved")
        if value.get("headRefOid") != expected_head:
            raise DeliveryBlocked("PR head changed; preserving task resources")
        return value

    try:
        if not worktree.is_absolute() or worktree != worktree.resolve():
            raise DeliveryBlocked("--worktree must be an absolute, non-symlink task path")
        if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
            raise DeliveryBlocked("--expected-head must be a full commit SHA")
        # Discover the main checkout even when resuming after worktree removal.
        anchor = worktree if worktree.exists() else worktree.parent
        common = Path(run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], anchor))
        root = common.parent
        if common.name != ".git" or worktree == root or root not in worktree.parents:
            raise DeliveryBlocked("only task worktrees under the repository may be cleaned")
        if not (worktree.is_relative_to(root / ".worktrees") or
                worktree.is_relative_to(root / ".claude" / "worktrees")):
            raise DeliveryBlocked("worktree must be in the repository's task worktree directory")
        if ledger_path.is_relative_to(worktree):
            raise DeliveryBlocked("delivery ledger must be outside the worktree being removed")
        if parse_github_remote(run(["git", "remote", "get-url", "origin"], root)) != repo:
            raise DeliveryBlocked("repository origin does not match --repo")
        info = payload(["gh", "api", f"repos/{repo}"])
        default = info.get("default_branch") if isinstance(info, dict) else None
        if not isinstance(default, str) or not default:
            raise DeliveryBlocked("live default branch unavailable")
        state = read_pr()
        branch = state.get("headRefName")
        if (not isinstance(branch, str) or not branch or branch == default or
                state.get("baseRefName") != default):
            raise DeliveryBlocked("task branch/default target identity unproved")
        run(["git", "check-ref-format", f"refs/heads/{branch}"], root)
        evidence.update(branch=branch, default_branch=default)

        def check_task() -> None:
            listing = run(["git", "worktree", "list", "--porcelain"], root)
            entries = parse_worktree_porcelain(listing)
            matches = [entry for entry in entries if entry.path == str(worktree)]
            if not matches or matches[0].head != expected_head or matches[0].branch != branch:
                raise DeliveryBlocked("task worktree ownership/head changed")
            raw = next((block for block in listing.split("\n\n") if block.startswith(f"worktree {worktree}\n")), "")
            if any(line.startswith(("locked", "prunable")) for line in raw.splitlines()):
                raise DeliveryBlocked("locked or prunable worktree preserved")
            if any((worktree / name).exists() for name in (".no-cleanup", ".git-janitor-preserve", ".understand-anything")):
                raise DeliveryBlocked("worktree preservation marker present")
            if run(["git", "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"], worktree):
                raise DeliveryBlocked("dirty or ignored task-local state preserved")

        if state.get("state") == "OPEN":
            check_task()
            record("started", "invoking existing guarded merge")
            code = guarded_merge(repo=repo, pr=pr, ledger_path=ledger_path, runner=runner,
                                 expected_head=expected_head, preserve_head_branch=True, **merge_options)
            if code:
                record("preserved", f"guarded merge stopped with exit {code}")
                return code
            state = read_pr()
        if state.get("state") != "MERGED":
            record("pending", "merge is pending; rerun finish after required checks pass")
            return 2
        merge = state.get("mergeCommit")
        merge_sha = merge.get("oid") if isinstance(merge, dict) else None
        if not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", merge_sha):
            raise DeliveryBlocked("merged commit identity unavailable")
        evidence["merge_commit"] = merge_sha
        run(["git", "fetch", "--prune", "origin"], root)
        run(["git", "merge-base", "--is-ancestor", merge_sha, f"origin/{default}"], root)
        run(["git", "diff", "--quiet", merge_sha, expected_head], root)
        # Update only the default checkout/ref; never switch the user's current branch.
        entries = parse_worktree_porcelain(run(["git", "worktree", "list", "--porcelain"], root))
        default_paths = [Path(entry.path) for entry in entries if entry.branch == default]
        remote_head = run(["git", "rev-parse", f"refs/remotes/origin/{default}"], root)
        local_head = run(["git", "rev-parse", f"refs/heads/{default}"], root)
        run(["git", "merge-base", "--is-ancestor", local_head, remote_head], root)
        for checkout in default_paths:
            status_args = ["git", "status", "--porcelain=v1", "--untracked-files=all"]
            if worktree.is_relative_to(checkout):
                # The exact owned task subtree is checked independently below;
                # keep all unrelated default-checkout dirt visible.
                status_args += ["--", ".",
                                f":(exclude,literal){worktree.relative_to(checkout).as_posix()}"]
            if run(status_args, checkout):
                raise DeliveryBlocked("dirty default checkout preserved")
            run(["git", "merge", "--ff-only", remote_head], checkout)
        if not default_paths and local_head != remote_head:
            run(["git", "update-ref", f"refs/heads/{default}", remote_head, local_head], root)
        evidence["synced_default"] = remote_head
        if not worktree.exists():
            remaining = runner(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], root, 30)
            if remaining.returncode == 1:
                record("complete", "merged and verified; task cleanup already complete; remote branch preserved")
                return 0
            # A prior interrupted cleanup may have removed the worktree only.
        else:
            check_task()
        proof = evaluate_delete(
            branch, repo_path=root, runner=runner,
            owned_worktree=worktree if worktree.exists() else None,
            expected_head=expected_head, verified_merge_commit=merge_sha,
        )
        evidence["cleanup_proof"] = proof.as_dict()
        if not proof.passed:
            raise DeliveryBlocked("branch cleanup proof incomplete; preserved (see ledger)")
        record("cleanup_proved", "merge tree and task ownership verified; removing exact local resources")
        if worktree.exists():
            check_task()
            evidence["worktree_removal_started"] = True
            run(["git", "worktree", "remove", str(worktree)], root)
            evidence["worktree_removed"] = True
        final_entries = parse_worktree_porcelain(run(["git", "worktree", "list", "--porcelain"], root))
        if any(entry.branch == branch for entry in final_entries):
            raise DeliveryBlocked("branch was claimed by another worktree; local ref preserved")
        if run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], root) != expected_head:
            raise DeliveryBlocked("branch advanced during cleanup; local ref preserved")
        run(proof.command, root)
        if worktree.exists():
            raise DeliveryBlocked("worktree removal readback failed")
        remaining = runner(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], root, 30)
        if remaining.returncode != 1:
            raise DeliveryBlocked("local branch removal readback failed")
        record("complete", "merged, tree verified, default synced, task worktree/local branch removed; remote branch preserved")
        return 0
    except (DeliveryBlocked, RuntimeError, OSError) as exc:
        status = "preserved"
        if evidence.get("worktree_removal_started") and not worktree.exists():
            # Git cannot atomically combine filesystem removal and ref CAS.
            # Restore a still-unclaimed preserved branch after a race/failure;
            # never overwrite a recreated path or force a second checkout.
            status = "partial_cleanup"
            listing = runner(["git", "worktree", "list", "--porcelain"], root, 30)
            remaining = runner(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], root, 30)
            if (listing.returncode == 0 and remaining.returncode == 0
                    and not any(entry.branch == branch for entry in parse_worktree_porcelain(listing.stdout))):
                restored = runner(["git", "worktree", "add", str(worktree), branch], root, 60)
                evidence["worktree_restored"] = restored.returncode == 0
                if restored.returncode == 0:
                    status = "preserved"
        record(status, str(exc))
        return 3
