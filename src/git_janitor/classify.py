from __future__ import annotations

from datetime import datetime, timezone
import re

from .config import ScannerConfig
from .models import Finding, LinkedWorktreeState, PullRequestState, RepoState


GREEN_MERGE_STATES = {"CLEAN", "HAS_HOOKS"}
BLOCKED_MERGE_STATES = {"BEHIND", "BLOCKED", "DIRTY", "DRAFT", "UNKNOWN", "UNSTABLE"}
BUILT_IN_HIGH_RISK_TERMS = {
    "access",
    "auth",
    "background",
    "ci",
    "credential",
    "deploy",
    "deployment",
    "launchagent",
    "launchd",
    "migration",
    "provenance",
    "publish",
    "publishing",
    "runner",
    "schema",
    "secret",
    "storage",
    "sync",
    "token",
    "workflow",
}


def classify_report(
    repos: list[RepoState],
    prs: list[PullRequestState],
    config: ScannerConfig,
    now: datetime | None = None,
) -> list[Finding]:
    now = now or datetime.now(timezone.utc)
    findings: list[Finding] = []
    for repo in repos:
        findings.extend(classify_repo(repo, config, now))
    for pr in prs:
        findings.extend(classify_pr(pr, config))
    return sorted(findings, key=_finding_sort_key)


def classify_repo(
    repo: RepoState,
    config: ScannerConfig,
    now: datetime,
) -> list[Finding]:
    findings: list[Finding] = []
    if repo.errors:
        findings.append(
            Finding(
                severity="high",
                category="scanner-error",
                title=f"{repo.name}: scanner could not inspect repository cleanly",
                detail="; ".join(repo.errors),
                repo_path=repo.path,
                recommended_action="Inspect this repo manually.",
            )
        )

    dirty_count = len(repo.dirty_files)
    untracked_count = len(repo.untracked_files)
    if dirty_count or untracked_count:
        findings.append(
            Finding(
                severity="high",
                category="dirty-worktree",
                title=f"{repo.name}: local worktree has uncommitted changes",
                detail=f"{dirty_count} modified/staged files, {untracked_count} untracked files.",
                repo_path=repo.path,
                recommended_action="Preserve local changes; confirm ownership and intent before committing or moving them.",
            )
        )

    if repo.ahead:
        findings.append(
            Finding(
                severity="high",
                category="unpushed-commits",
                title=f"{repo.name}: current branch is ahead of upstream",
                detail=f"{repo.current_branch or 'current branch'} is ahead by {repo.ahead} commit(s).",
                repo_path=repo.path,
                recommended_action="Preserve the commits; confirm ownership and existing delivery authority before publishing.",
            )
        )

    if repo.ahead and repo.behind:
        findings.append(_divergence_finding(
            repo.path, repo.name, repo.current_branch or "current branch", repo.ahead, repo.behind,
        ))

    current_upstream_gone = any(branch.current and branch.upstream_gone for branch in repo.branches)
    if repo.current_branch == "HEAD":
        findings.append(_detached_finding(repo.path, repo.name, repo.head_oid, repo.head_unique_commit_count))
    elif ((repo.upstream is None and repo.tracking_configured is not True)
          or current_upstream_gone) and repo.head_unique_commit_count:
        findings.append(_unpublished_finding(
            repo.path, repo.name, repo.current_branch or "current branch",
            repo.head_unique_commit_count, repo.default_ref or repo.default_branch,
            upstream_gone=current_upstream_gone,
            tracking_configured=repo.tracking_configured,
        ))

    for worktree in repo.linked_worktrees:
        branch_label = worktree.branch or "detached HEAD"
        if worktree.dirty_files or worktree.untracked_files:
            findings.append(Finding(
                severity="high",
                category="dirty-worktree",
                title=f"{repo.name}: linked worktree {branch_label} has uncommitted changes",
                detail=(f"{len(worktree.dirty_files)} modified/staged files, "
                        f"{len(worktree.untracked_files)} untracked files at {worktree.path}."),
                repo_path=worktree.path,
                recommended_action="Preserve this worktree and its files; confirm ownership and activity before delivery or cleanup.",
            ))
        if worktree.branch is None:
            findings.append(_detached_finding(
                worktree.path, repo.name, worktree.head, worktree.unique_commit_count,
            ))
        elif ((worktree.upstream is None and worktree.tracking_configured is not True)
              or worktree.upstream_gone) and worktree.unique_commit_count:
            findings.append(_unpublished_finding(
                worktree.path, repo.name, worktree.branch, worktree.unique_commit_count,
                worktree.default_ref or repo.default_ref or repo.default_branch,
                upstream_gone=worktree.upstream_gone,
                tracking_configured=worktree.tracking_configured,
            ))
        if worktree.ahead:
            findings.append(Finding(
                severity="high", category="unpushed-commits",
                title=f"{repo.name}: linked worktree {branch_label} is ahead of upstream",
                detail=f"{branch_label} is ahead by {worktree.ahead} commit(s) at {worktree.path}.",
                repo_path=worktree.path,
                recommended_action="Preserve the commits; confirm task ownership and delivery authorization before publishing.",
            ))
        if worktree.ahead and worktree.behind:
            findings.append(_divergence_finding(
                worktree.path, repo.name, branch_label, worktree.ahead, worktree.behind,
            ))
        if worktree.upstream_gone:
            findings.append(_classify_stale_linked_worktree(repo, worktree))

    linked_branch_names = {worktree.branch for worktree in repo.linked_worktrees}
    for branch in repo.branches:
        if branch.current:
            continue
        if branch.ahead and branch.name not in linked_branch_names:
            findings.append(Finding(
                severity="high", category="unpushed-commits",
                title=f"{repo.name}: local branch {branch.name} is ahead of upstream",
                detail=f"{branch.name} is ahead by {branch.ahead} commit(s) of {branch.upstream}.",
                repo_path=repo.path,
                recommended_action="Preserve the commits; confirm task ownership and delivery authorization before publishing.",
            ))
            if branch.behind:
                findings.append(_divergence_finding(
                    repo.path, repo.name, branch.name, branch.ahead, branch.behind,
                ))
        if branch.name == repo.default_branch:
            continue
        if branch.merged_to_default and branch.unique_commit_count == 0 and not repo.errors:
            findings.append(
                Finding(
                    severity="low",
                    category="merged-local-branch",
                    title=f"{repo.name}: local branch {branch.name} appears merged",
                    detail=f"{branch.name} is an ancestor of {repo.default_ref or repo.default_branch}.",
                    repo_path=repo.path,
                    recommended_action="Preserve until current merge, publication, inactivity and ownership proof are complete; ancestry alone does not authorize cleanup.",
                )
            )
        elif ((branch.upstream is None and branch.tracking_configured is not True)
              or branch.upstream_gone) and branch.unique_commit_count and branch.name not in linked_branch_names:
            findings.append(
                _unpublished_finding(
                    repo.path, repo.name, branch.name, branch.unique_commit_count,
                    repo.default_ref or repo.default_branch,
                    upstream_gone=branch.upstream_gone,
                    tracking_configured=branch.tracking_configured,
                )
            )

    if repo.fetch_prune_status and repo.fetch_prune_status != "ok":
        findings.append(
            Finding(
                severity="medium",
                category="fetch-prune-failed",
                title=f"{repo.name}: fetch/prune failed",
                detail=repo.fetch_prune_status,
                repo_path=repo.path,
                recommended_action="Check network/authentication or run git fetch --prune origin manually.",
            )
        )
    return findings


def _unpublished_finding(
    path: str, name: str, branch: str, unique: int, base: str,
    *, upstream_gone: bool = False, tracking_configured: bool | None = None,
) -> Finding:
    upstream_state = (
        "missing local upstream ref ([gone]); remote existence and publication unknown"
        if upstream_gone else
        "no tracking configured" if tracking_configured is False else
        "local upstream unresolved; tracking configuration unknown"
    )
    return Finding(
        severity="medium",
        category=("scanner-error" if tracking_configured is None and not upstream_gone
                  else "branch-without-upstream"),
        title=f"{name}: local branch {branch} has {upstream_state}",
        detail=f"{branch} has {unique} unique commit(s) versus {base} and {upstream_state}.",
        repo_path=path,
        recommended_action="Preserve the commits; confirm task ownership and intent before publishing or changing the branch.",
    )


def _detached_finding(path: str, name: str, head: str | None, unique: int | None) -> Finding:
    return Finding(
        severity="high" if unique else "medium", category="detached-worktree",
        title=f"{name}: checkout has detached HEAD",
        detail=f"HEAD={head or 'unknown'}; unique_commit_count={unique if unique is not None else 'unknown'}; path={path}.",
        repo_path=path,
        recommended_action="Preserve HEAD and all local work; confirm ownership before attaching a branch or considering cleanup.",
    )


def _divergence_finding(path: str, name: str, branch: str, ahead: int, behind: int) -> Finding:
    return Finding(
        severity="high", category="diverged-upstream",
        title=f"{name}: {branch} diverged from upstream",
        detail=f"{branch} is ahead by {ahead} and behind by {behind} commit(s).",
        repo_path=path,
        recommended_action="Preserve both histories; determine ownership and integrate only within an authorized delivery task.",
    )


def _classify_stale_linked_worktree(
    repo: RepoState,
    worktree: LinkedWorktreeState,
) -> Finding:
    exact_safe = _linked_worktree_exact_cleanup_conditions(worktree)
    base = worktree.default_ref or repo.default_ref or repo.default_branch
    branch = worktree.branch or "unknown branch"
    upstream = worktree.upstream or "unknown upstream"
    detail = (
        f"{branch} tracks {upstream} [gone] at {worktree.path}; "
        f"clean={not worktree.dirty_files and not worktree.untracked_files}, "
        f"tree_matches_{base}={worktree.tree_matches_default}, "
        f"unique_commit_count={worktree.unique_commit_count}."
    )
    if worktree.errors:
        detail = f"{detail} Inspection errors: {'; '.join(worktree.errors)}."

    return Finding(
        severity="low" if exact_safe else "medium",
        category="stale-linked-worktree",
        title=f"{repo.name}: linked worktree {branch} has a gone upstream",
        detail=detail,
        repo_path=worktree.path,
        recommended_action=(
            "Cleanup is approval-required after exact safe conditions are met."
            if exact_safe
            else "Do not clean automatically; exact safe conditions were not met."
        ),
    )


def _linked_worktree_exact_cleanup_conditions(worktree: LinkedWorktreeState) -> bool:
    return (
        worktree.upstream_gone
        and not worktree.errors
        and not worktree.dirty_files
        and not worktree.untracked_files
        and worktree.tree_matches_default is True
        and worktree.unique_commit_count == 0
    )


def classify_pr(pr: PullRequestState, config: ScannerConfig) -> list[Finding]:
    findings: list[Finding] = []
    risk_reasons = pr_risk_reasons(pr, config)
    pr.risk_reasons = risk_reasons

    label = f"{pr.repo}#{pr.number}: {pr.title}"
    if pr.errors:
        findings.append(
            Finding(
                severity="medium",
                category="pr-inspection-warning",
                title=f"{label}: incomplete PR inspection",
                detail="; ".join(pr.errors),
                url=pr.url,
                recommended_action="Open the PR if the classification seems ambiguous.",
            )
        )
        return findings

    if pr.check_status == "stale":
        findings.append(
            Finding(
                severity="medium",
                category="pr-stale-ci",
                title=f"{label} has stale checks",
                detail=f"mergeState={pr.merge_state or 'unknown'}, review={pr.review_decision or 'unknown'}.",
                url=pr.url,
                recommended_action="Refresh or rerun CI before making a merge decision.",
            )
        )
        return findings

    if pr.check_status == "failure":
        findings.append(
            Finding(
                severity="high",
                category="pr-failing",
                title=f"{label} has failing checks",
                detail=f"mergeState={pr.merge_state or 'unknown'}, review={pr.review_decision or 'unknown'}.",
                url=pr.url,
                recommended_action="Fix CI or inspect the failing check logs.",
            )
        )
        return findings

    if pr.merge_state in {"DIRTY"}:
        findings.append(
            Finding(
                severity="high",
                category="pr-conflicted",
                title=f"{label} has merge conflicts",
                detail="GitHub reports mergeState=DIRTY.",
                url=pr.url,
                recommended_action="Rebase or merge the base branch and resolve conflicts.",
            )
        )
        return findings

    if pr.is_draft and pr.check_status == "success":
        findings.append(
            Finding(
                severity="medium",
                category="green-draft-pr",
                title=f"{label} is draft but checks are green",
                detail=_pr_detail(pr, risk_reasons),
                url=pr.url,
                recommended_action="If the work is complete, mark ready for review or merge flow.",
            )
        )
        return findings

    if pr.check_status == "success" and pr.merge_state in GREEN_MERGE_STATES:
        category = "green-high-risk-pr" if risk_reasons else "green-mergeable-pr"
        severity = "medium" if risk_reasons else "high"
        recommended = (
            "Author should inspect changed behavior and concrete impact, then apply the shared risk tier."
            if risk_reasons
            else "Likely ready for merge if this PR still matches your intent."
        )
        findings.append(
            Finding(
                severity=severity,
                category=category,
                title=f"{label} is green and mergeable",
                detail=_pr_detail(pr, risk_reasons),
                url=pr.url,
                recommended_action=recommended,
            )
        )
        return findings

    if pr.check_status == "pending":
        findings.append(
            Finding(
                severity="low",
                category="pr-pending",
                title=f"{label} still has pending checks",
                detail=f"mergeState={pr.merge_state or 'unknown'}, review={pr.review_decision or 'unknown'}.",
                url=pr.url,
                recommended_action="No action unless it remains pending for a long time.",
            )
        )
        return findings

    if pr.merge_state in BLOCKED_MERGE_STATES:
        findings.append(
            Finding(
                severity="medium",
                category="pr-blocked-or-needs-review",
                title=f"{label} is not ready to merge",
                detail=f"checks={pr.check_status}, mergeState={pr.merge_state or 'unknown'}, review={pr.review_decision or 'unknown'}.",
                url=pr.url,
                recommended_action="Inspect the PR if it has been sitting unexpectedly.",
            )
        )
    return findings


def pr_risk_reasons(pr: PullRequestState, config: ScannerConfig) -> list[str]:
    haystack = " ".join([pr.title, pr.head_ref, *(pr.changed_files or [])]).lower()
    reasons = [pattern for pattern in config.high_risk_patterns if pattern in haystack]
    reasons.extend(_built_in_pr_risk_reasons(pr))
    return sorted(set(reasons))


def _built_in_pr_risk_reasons(pr: PullRequestState) -> list[str]:
    reasons: list[str] = []
    title_and_branch = f"{pr.title} {pr.head_ref}".lower()
    for term in BUILT_IN_HIGH_RISK_TERMS:
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", title_and_branch):
            reasons.append(f"{term} protected change")

    for raw_path in pr.changed_files or []:
        path = raw_path.lower()
        if path.startswith(".github/"):
            reasons.append(".github workflow/config change")
        path_terms = set(re.split(r"[^a-z0-9]+", path))
        for term in BUILT_IN_HIGH_RISK_TERMS:
            if term in path_terms:
                reasons.append(f"{term} protected change")
    return reasons


def _pr_detail(pr: PullRequestState, risk_reasons: list[str]) -> str:
    size = ""
    if pr.additions is not None and pr.deletions is not None:
        size = f", +{pr.additions}/-{pr.deletions}"
    risk = f", risk_candidates={', '.join(risk_reasons)}" if risk_reasons else ""
    return (
        f"checks={pr.check_status}, mergeState={pr.merge_state or 'unknown'}, "
        f"review={pr.review_decision or 'unknown'}{size}{risk}."
    )


def _finding_sort_key(finding: Finding) -> tuple[int, str, str]:
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    return (severity_rank.get(finding.severity, 9), finding.category, finding.title)
