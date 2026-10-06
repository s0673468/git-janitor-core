"""Rank evidence and follow-ups without granting authority to act on repositories."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .models import ScanReport


@dataclass(frozen=True)
class QueueItem:
    rank: int
    item_id: str
    lane: str
    status: str
    category: str
    target: str
    title: str
    next_action: str
    authorization: str
    ownership: str
    preservation: str
    observed_at: str
    evidence: tuple[str, ...]
    unknowns: tuple[str, ...]


GAPS = frozenset({"scanner-error", "fetch-prune-failed", "pr-inspection-warning", "pr-stale-ci"})
LOCAL_WORK = frozenset({"dirty-worktree", "detached-worktree", "diverged-upstream", "unpushed-commits", "branch-without-upstream"})
CLEANUP = frozenset({"merged-local-branch", "stale-linked-worktree"})
INVENTORY = frozenset({"unregistered-github-repo", "unregistered-local-repo", "missing-canonical-checkout", "missing-github-repo", "lifecycle-mismatch", "canonical-remote-mismatch", "canonical-path-mismatch"})


def build_delivery_queue(report: ScanReport) -> list[QueueItem]:
    """Deterministic derivation of this report's evidence; never an execution plan."""
    entries: list[tuple[int, str, dict]] = []
    for index, finding in enumerate(report.findings):
        category = finding.category
        target = finding.repo_path or finding.url or "scan coverage"
        evidence = [f"/findings/{index}: {finding.detail}"]
        unknowns = ["ownership and current authorization"]
        for repo_index, repo in enumerate(report.repos):
            if finding.repo_path == repo.path or any(w.path == finding.repo_path for w in repo.linked_worktrees):
                evidence.append(f"/repos/{repo_index}: HEAD={repo.head_oid or 'unknown'}; default_ref={repo.default_ref or 'unknown'}; default_oid={repo.default_oid or 'unknown'}; fetch={repo.fetch_prune_status or 'not verified'}")
                for worktree_index, worktree in enumerate(repo.linked_worktrees):
                    if worktree.path == finding.repo_path:
                        evidence.append(f"/repos/{repo_index}/linked_worktrees/{worktree_index}: HEAD={worktree.head or 'unknown'}; dirty={len(worktree.dirty_files)}; untracked={len(worktree.untracked_files)}; inspection_errors={len(worktree.errors)}")
                unknowns.extend(["merge receipt and publication intent", "activity and opt-out markers"])
                break
        for pr_index, pr in enumerate(report.pull_requests):
            if finding.url and finding.url == pr.url:
                evidence.append(f"/pull_requests/{pr_index}: head={pr.head_oid or 'unknown'}; author={pr.author or 'unknown'}; checks={pr.check_status}; mergeState={pr.merge_state or 'unknown'}")
                unknowns.extend(["required exact-head gate and receipt", "current head/base after collection"])
                break

        if category in GAPS or category in INVENTORY:
            priority, lane, status = 0, "evidence", "coverage-gap"
            action = "Restore or refresh the missing source evidence; preserve affected work until collection is complete."
        elif category in LOCAL_WORK:
            priority, lane, status = 1, "hygiene", "preserve-local-work"
            action = "Inspect the exact worktree and commits, identify their owner, and record whether they should remain local or enter delivery."
        elif category in CLEANUP:
            priority, lane, status = 5, "hygiene", "preserve-until-proof"
            action = "Preserve this branch/worktree. Verify current merge, tree equivalence, publication, inactivity and ownership before proposing cleanup."
        elif category in {"green-mergeable-pr", "green-high-risk-pr", "green-draft-pr"}:
            priority, lane, status = 3, "delivery", "authorization-and-gate-required"
            action = "Identify the delivery owner and existing authority; verify the current head, required gate and receipt before any ready/merge action."
        elif category == "pr-pending":
            priority, lane, status = 4, "delivery", "waiting"
            action = "Inspect the current check or durable watcher; distinguish queued/not-run checks from actual failures."
        else:
            priority, lane, status = 2, "delivery", "inspect-blocker"
            action = finding.recommended_action or "Inspect the cited source and identify the next authorized step."

        identity = hashlib.sha256(f"{category}\0{target}\0{finding.title}".encode()).hexdigest()[:16]
        entries.append((priority, identity, dict(
            item_id=identity, lane=lane, status=status, category=category, target=target,
            title=finding.title, next_action=action,
            authorization="Inspection only. This report grants no mutation authority; use explicit task/session authorization.",
            ownership="Unverified; a branch name or PR author does not establish task ownership.",
            preservation="Keep existing changes, commits, branches and worktrees.",
            observed_at=report.generated_at, evidence=tuple(evidence), unknowns=tuple(unknowns),
        )))
    for index, error in enumerate(report.errors):
        identity = hashlib.sha256(f"error\0{error}".encode()).hexdigest()[:16]
        entries.append((0, identity, dict(
            item_id=identity, lane="evidence", status="coverage-gap", category="collection-error",
            target="scan coverage", title="Collection is incomplete", next_action="Restore the missing source and rerun this inventory before drawing a clean conclusion.",
            authorization="Inspection only; no credential or access changes authorized by this report.",
            ownership="Unverified", preservation="Preserve affected work.",
            observed_at=report.generated_at, evidence=(f"/errors/{index}: {error}",),
            unknowns=("unobserved repository or PR state",),
        )))
    entries.sort(key=lambda item: (item[0], item[2]["target"], item[2]["category"], item[1]))
    seen: set[str] = set()
    queue: list[QueueItem] = []
    for _, identity, values in entries:
        if identity not in seen:
            seen.add(identity)
            queue.append(QueueItem(rank=len(queue) + 1, **values))
    return queue
