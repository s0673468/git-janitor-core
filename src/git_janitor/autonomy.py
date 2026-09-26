from __future__ import annotations

from dataclasses import dataclass

from .classify import GREEN_MERGE_STATES, pr_risk_reasons
from .config import ScannerConfig
from .models import AutomationDecision, BranchState, LinkedWorktreeState, PullRequestState, RepoState


AUTO_ACT = "auto-act"
NEEDS_APPROVAL = "needs-approval"
NO_OP = "no-op"
REPORT_BLOCKED = "report-blocked"
WAIT = "wait"


@dataclass(frozen=True)
class AutomationPolicy:
    auto_merge_green_prs: bool = False
    auto_mark_drafts_ready: bool = False
    auto_delete_merged_branches: bool = False
    auto_fast_forward_default_branch: bool = False
    auto_fix_workflows: bool = False
    auto_repair_runner_tools: bool = False
    apply_categories: frozenset[str] = frozenset()
    ledger_path: str | None = None
    min_safe_score: int = 4

    @classmethod
    def from_config(cls, config: ScannerConfig) -> AutomationPolicy:
        return cls(
            auto_merge_green_prs=config.auto_merge_green_prs,
            auto_mark_drafts_ready=config.auto_mark_drafts_ready,
            auto_delete_merged_branches=config.auto_delete_merged_branches,
            auto_fast_forward_default_branch=getattr(
                config,
                "auto_fast_forward_default_branch",
                False,
            ),
            apply_categories=frozenset(getattr(config, "apply_categories", ())),
            ledger_path=getattr(config, "ledger_path", None),
        )


@dataclass(frozen=True)
class TouchedRepoCandidate:
    repo: str
    candidate: str
    safety_score: int | None
    recent_files: tuple[str, ...] = ()
    repo_errors: tuple[str, ...] = ()
    dirty_files: tuple[str, ...] = ()
    fast_forward: tuple[str, ...] = ()
    remote_state: tuple[str, ...] = ()
    publication_boundary: tuple[str, ...] = ()
    pr_lookup_errors: tuple[str, ...] = ()
    open_codex_prs: tuple[str, ...] = ()
    stale_ci: tuple[str, ...] = ()
    approval_only_findings: tuple[str, ...] = ()
    permission_sensitive: bool = False
    changed_paths: tuple[str, ...] = ()
    preserved_paths: tuple[str, ...] = ()
    score_components: tuple[str, ...] = ()


def decide_pull_request(
    pr: PullRequestState,
    config: ScannerConfig,
    policy: AutomationPolicy | None = None,
) -> AutomationDecision:
    policy = policy or AutomationPolicy.from_config(config)
    risk_reasons = pr_risk_reasons(pr, config)
    pr.risk_reasons = risk_reasons
    label = f"{pr.repo}#{pr.number}: {pr.title}"
    evidence = _pr_evidence(pr, risk_reasons)

    if pr.errors:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="incomplete-pr-inspection",
            title=f"{label} needs manual inspection",
            reason="PR metadata is incomplete, so autonomous action would be unsafe.",
            recommended_action="Open the PR and inspect the missing metadata before acting.",
            evidence=tuple(pr.errors),
        )

    if pr.check_status == "stale":
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="stale-ci",
            title=f"{label} has stale checks",
            reason="GitHub reports stale CI, so autonomous action would be unsafe.",
            recommended_action="Refresh or rerun CI before making a merge or maintainer decision.",
            evidence=evidence,
        )

    if pr.check_status == "failure":
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="ci-failure",
            title=f"{label} has failing checks",
            reason="Failing CI means the automation should report a blocker, not merge or mark ready.",
            recommended_action="Inspect the failing check logs and fix the underlying failure.",
            evidence=evidence,
        )

    if pr.merge_state == "DIRTY":
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="merge-conflict",
            title=f"{label} has merge conflicts",
            reason="GitHub reports mergeState=DIRTY.",
            recommended_action="Resolve conflicts before any merge decision.",
            evidence=evidence,
        )

    if pr.check_status == "pending":
        return AutomationDecision(
            disposition=WAIT,
            category="pending-checks",
            title=f"{label} still has pending checks",
            reason="Required checks have not reached a final state.",
            recommended_action="Wait for CI to finish before taking PR action.",
            evidence=evidence,
        )

    if pr.is_draft and pr.check_status == "success":
        if risk_reasons:
            return AutomationDecision(
                disposition=NEEDS_APPROVAL,
                category="high-risk-draft-pr",
                title=f"{label} is a green draft needing behavior inspection",
                reason="Draft readiness is ambiguous; path/title cues require inspection of changed behavior.",
                recommended_action="Preserve unattended state; the author should verify completion and behavioral risk within existing authority.",
                evidence=evidence,
            )
        if policy.auto_mark_drafts_ready:
            return AutomationDecision(
                disposition=AUTO_ACT,
                category="mark-draft-ready",
                title=f"{label} can be marked ready",
                reason="Checks are green, no high-risk patterns matched, and policy allows draft readiness.",
                recommended_action="Mark the completed draft PR ready for delivery.",
                evidence=evidence,
            )
        return AutomationDecision(
            disposition=NEEDS_APPROVAL,
            category="draft-needs-approval",
            title=f"{label} is a green draft",
            reason="The work may be intentionally incomplete.",
            recommended_action="Ask whether the draft should be marked ready.",
            evidence=evidence,
        )

    if pr.check_status == "success" and pr.merge_state in GREEN_MERGE_STATES:
        if risk_reasons:
            return AutomationDecision(
                disposition=NEEDS_APPROVAL,
                category="high-risk-pr",
                title=f"{label} is green with risk cues to inspect",
                reason="Path/title cues identify possible risk; the author must inspect reachable behavior and impact.",
                recommended_action="Preserve unattended state; the author applies the shared change tier and test requirements after inspection.",
                evidence=evidence,
            )
        if policy.auto_merge_green_prs:
            return AutomationDecision(
                disposition=AUTO_ACT,
                category="merge-green-pr",
                title=f"{label} can be squash-merged",
                reason="Checks are green, merge state is clean, and no high-risk patterns matched.",
                recommended_action="Squash-merge the ready PR.",
                evidence=evidence,
            )
        return AutomationDecision(
            disposition=NEEDS_APPROVAL,
            category="green-pr-needs-approval",
            title=f"{label} is green and mergeable",
            reason="Policy does not currently allow autonomous PR merges.",
            recommended_action="Ask for approval or enable auto_merge_green_prs.",
            evidence=evidence,
        )

    return AutomationDecision(
        disposition=NO_OP,
        category="no-pr-action",
        title=f"{label} has no autonomous action",
        reason="PR state does not match an automatic-safe or blocked path.",
        recommended_action="Leave unchanged unless a human asks for follow-up.",
        evidence=evidence,
    )


def decide_branch_cleanup(
    repo: RepoState,
    branch: BranchState,
    policy: AutomationPolicy | None = None,
) -> AutomationDecision:
    policy = policy or AutomationPolicy()
    label = f"{repo.name}:{branch.name}"
    evidence = (
        f"default_ref={repo.default_ref or repo.default_branch}",
        f"merged_to_default={branch.merged_to_default}",
        f"unique_commit_count={branch.unique_commit_count}",
        f"upstream={branch.upstream or 'none'}",
    )

    if repo.errors:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="scanner-error",
            title=f"{label} cannot be classified safely",
            reason="Repository inspection had errors.",
            recommended_action="Inspect scanner errors before deleting branches.",
            evidence=tuple(repo.errors),
        )

    if repo.dirty_files or repo.untracked_files:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="dirty-worktree",
            title=f"{label} is in a dirty repository",
            reason="Branch cleanup should not run while local work may be active.",
            recommended_action="Review the dirty worktree before considering branch deletion.",
            evidence=tuple(repo.dirty_files + repo.untracked_files),
        )

    if branch.current or branch.name == repo.default_branch:
        return AutomationDecision(
            disposition=NO_OP,
            category="protected-branch",
            title=f"{label} is not a cleanup target",
            reason="The branch is current or is the default branch.",
            recommended_action="Leave the branch unchanged.",
            evidence=evidence,
        )

    if branch.merged_to_default and branch.unique_commit_count == 0:
        if policy.auto_delete_merged_branches:
            return AutomationDecision(
                disposition=AUTO_ACT,
                category="delete-merged-branch",
                title=f"{label} can be deleted",
                reason="The branch is merged to the default ref and has no unique commits.",
                recommended_action=f"Delete exactly {branch.name} with git branch -d.",
                evidence=evidence,
            )
        return AutomationDecision(
            disposition=NEEDS_APPROVAL,
            category="delete-merged-branch",
            title=f"{label} appears safe to delete",
            reason="The branch is merged, but policy requires exact deletion approval.",
            recommended_action=f"Ask for approval to delete exactly {branch.name}.",
            evidence=evidence,
        )

    if branch.upstream is None and branch.unique_commit_count:
        return AutomationDecision(
            disposition=NEEDS_APPROVAL,
            category="branch-without-upstream",
            title=f"{label} has local-only commits",
            reason="The branch has unique commits and no upstream.",
            recommended_action="Ask whether to push/open a PR or keep the branch local.",
            evidence=evidence,
        )

    return AutomationDecision(
        disposition=NO_OP,
        category="no-branch-action",
        title=f"{label} has no autonomous cleanup",
        reason="The branch does not match a proven safe deletion path.",
        recommended_action="Leave the branch unchanged.",
        evidence=evidence,
    )


def decide_linked_worktree_cleanup(
    repo: RepoState,
    worktree: LinkedWorktreeState,
) -> AutomationDecision:
    branch = worktree.branch or "unknown branch"
    label = f"{repo.name}:{branch}"
    evidence = _linked_worktree_evidence(repo, worktree)

    if not worktree.upstream_gone:
        return AutomationDecision(
            disposition=NO_OP,
            category="linked-worktree-active-upstream",
            title=f"{label} is not a stale linked worktree cleanup target",
            reason="The linked worktree branch does not have a gone upstream.",
            recommended_action="Leave the linked worktree unchanged.",
            evidence=evidence,
        )

    if worktree.errors:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="stale-linked-worktree-not-safe",
            title=f"{label} needs manual linked worktree inspection",
            reason="Linked worktree inspection had errors, so cleanup safety is unknown.",
            recommended_action="Inspect the linked worktree before proposing cleanup.",
            evidence=evidence,
        )

    if worktree.dirty_files or worktree.untracked_files:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="stale-linked-worktree-not-safe",
            title=f"{label} has local work in a stale linked worktree",
            reason="The upstream is gone, but the linked worktree is not clean.",
            recommended_action="Review the linked worktree's local changes before any cleanup request.",
            evidence=evidence,
        )

    if worktree.tree_matches_default is not True:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="stale-linked-worktree-not-safe",
            title=f"{label} does not meet exact linked worktree cleanup conditions",
            reason="The upstream is gone, but the tree diff against the current base is not empty.",
            recommended_action=(
                "Report the stale linked worktree and require manual inspection before cleanup."
            ),
            evidence=evidence,
        )

    if worktree.unique_commit_count != 0:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="stale-linked-worktree-not-safe",
            title=f"{label} has local commit history in a stale linked worktree",
            reason="The tree diff is empty, but unique local commits remain or could not be verified.",
            recommended_action=(
                "Report the stale linked worktree and require manual history review before cleanup."
            ),
            evidence=evidence,
        )

    return AutomationDecision(
        disposition=NEEDS_APPROVAL,
        category="stale-linked-worktree-cleanup",
        title=f"{label} is a stale linked worktree cleanup candidate",
        reason=(
            "Read-only checks found a gone upstream, a clean linked worktree, and an empty tree "
            "diff against the current base."
        ),
        recommended_action=(
            f"Ask for explicit approval before removing linked worktree {worktree.path} or deleting "
            f"branch {branch}."
        ),
        evidence=evidence,
    )


def decide_touched_repo_candidate(
    candidate: TouchedRepoCandidate,
    policy: AutomationPolicy | None = None,
) -> AutomationDecision:
    policy = policy or AutomationPolicy()

    if candidate.recent_files:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="recent-activity",
            title=f"{candidate.repo}: skip active worktree",
            reason="Files changed inside the 60-minute in-flight window.",
            recommended_action="Report the repo as skipped and do not run deep validation.",
            evidence=candidate.recent_files,
        )

    if candidate.publication_boundary:
        return AutomationDecision(
            disposition=NEEDS_APPROVAL,
            category="publication-boundary-blocked",
            title=f"{candidate.repo}: skip repo without a verified GitHub PR path",
            reason="Creating remotes, publishing local-only repos, or guessing GitHub routing needs explicit approval.",
            recommended_action="Report the publication boundary and leave the repo unchanged.",
            evidence=candidate.publication_boundary,
        )

    if candidate.repo_errors:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="repo-inspection-failed",
            title=f"{candidate.repo}: skip repo because local inspection is incomplete",
            reason="The planner could not inspect the repository cleanly.",
            recommended_action="Fix scanner/local Git inspection and rerun before editing this repo.",
            evidence=candidate.repo_errors,
        )

    if candidate.dirty_files:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="dirty-worktree",
            title=f"{candidate.repo}: skip dirty worktree",
            reason="Uncommitted local work is present outside preserved generated-state paths.",
            recommended_action="Report the dirty paths and do not edit this repo during the maintainer pass.",
            evidence=candidate.dirty_files,
        )

    if candidate.pr_lookup_errors:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="pr-hard-stop-lookup-failed",
            title=f"{candidate.repo}: skip repo because open PR state is incomplete",
            reason="The planner could not verify whether a Codex PR is already in flight.",
            recommended_action="Fix GitHub PR inspection and rerun before editing this repo.",
            evidence=candidate.pr_lookup_errors,
        )

    if candidate.open_codex_prs:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="in-flight-codex-pr",
            title=f"{candidate.repo}: skip repo with Codex PR in flight",
            reason="Open Codex-authored PRs are a hard stop for touched-repo maintenance.",
            recommended_action="Wait for the existing PR to merge or close before editing.",
            evidence=candidate.open_codex_prs,
        )

    if candidate.stale_ci:
        return AutomationDecision(
            disposition=REPORT_BLOCKED,
            category="stale-ci",
            title=f"{candidate.repo}: skip repo with stale CI",
            reason="Existing GitHub checks are stale, so the maintainer should not start a new edit yet.",
            recommended_action="Report the stale checks and refresh or rerun CI before scoring this repo.",
            evidence=candidate.stale_ci,
        )

    if candidate.fast_forward:
        return AutomationDecision(
            disposition=AUTO_ACT,
            category="fast-forward-default-branch",
            title=f"{candidate.repo}: default branch can be fast-forwarded",
            reason="The repo is clean, has a verified origin, has no open Codex PR, and is only behind its upstream.",
            recommended_action="Fast-forward the default branch with git pull --ff-only, then rerun the planner.",
            evidence=candidate.fast_forward,
        )

    if candidate.remote_state:
        return AutomationDecision(
            disposition=WAIT,
            category="remote-divergence",
            title=f"{candidate.repo}: refresh or resolve branch divergence before scoring",
            reason="The current branch is not aligned with its upstream.",
            recommended_action="Report the divergence; only continue after an explicit fast-forward or local-commit decision.",
            evidence=candidate.remote_state,
        )

    if candidate.approval_only_findings:
        return AutomationDecision(
            disposition=NEEDS_APPROVAL,
            category="approval-only-finding",
            title=f"{candidate.repo}: approval-only maintainer finding",
            reason="The strongest current finding needs explicit approval rather than an automatic patch.",
            recommended_action="Report the exact approval needed and leave the repo unchanged.",
            evidence=candidate.approval_only_findings,
        )

    if candidate.permission_sensitive:
        return AutomationDecision(
            disposition=NEEDS_APPROVAL,
            category="permission-sensitive",
            title=f"{candidate.repo}: {candidate.candidate} needs approval",
            reason="The candidate touches workflow, deploy, runner, sync, or other protected behavior.",
            recommended_action="Report the proposed fix, risk, validation plan, and rollback.",
            evidence=candidate.changed_paths,
        )

    if candidate.safety_score is None:
        return AutomationDecision(
            disposition=WAIT,
            category="needs-local-gate-and-score",
            title=f"{candidate.repo}: ready for maintainer review",
            reason="The repo passed fleet hard stops, but no score-qualified candidate was selected yet.",
            recommended_action=(
                "Read repo guidance, run the local gate, inspect recent changes, score the strongest "
                "candidate, then implement only if it is automatic-safe and score-qualified."
            ),
            evidence=candidate.changed_paths,
        )

    if candidate.safety_score < policy.min_safe_score:
        return AutomationDecision(
            disposition=NO_OP,
            category="score-below-threshold",
            title=f"{candidate.repo}: no automatic-safe candidate",
            reason=f"Safety score {candidate.safety_score} is below {policy.min_safe_score}.",
            recommended_action="Return a concise no-op/status report.",
            evidence=_touched_repo_evidence(candidate),
        )

    return AutomationDecision(
        disposition=AUTO_ACT,
        category="low-risk-maintainer-fix",
        title=f"{candidate.repo}: {candidate.candidate} can be fixed autonomously",
        reason="The candidate is low risk, locally reviewable, and score-qualified.",
        recommended_action=(
            "Implement the focused fix, validate locally, open a ready PR, babysit required checks, "
            "and append the run summary to automation memory."
        ),
        evidence=_touched_repo_evidence(candidate),
    )


def decide_runner_failure(
    log: str,
    policy: AutomationPolicy | None = None,
) -> AutomationDecision | None:
    policy = policy or AutomationPolicy()
    lower = log.lower()

    if "gtar: command not found" in lower and "upload-pages-artifact" in lower:
        disposition = AUTO_ACT if policy.auto_repair_runner_tools else NEEDS_APPROVAL
        return AutomationDecision(
            disposition=disposition,
            category="runner-tool-missing",
            title="Self-hosted runner is missing gtar for Pages artifact upload",
            reason="The app logic already reached artifact upload; this is runner/workflow infrastructure.",
            recommended_action=(
                "Install gtar or adjust the workflow only when runner repair is explicitly authorized."
            ),
            evidence=("gtar: command not found", "actions/upload-pages-artifact"),
        )

    if "command not found" in lower and "runner" in lower:
        disposition = AUTO_ACT if policy.auto_repair_runner_tools else NEEDS_APPROVAL
        return AutomationDecision(
            disposition=disposition,
            category="runner-tool-missing",
            title="Self-hosted runner is missing a required tool",
            reason="Missing runner tools are infrastructure changes, not application fixes.",
            recommended_action="Report the missing tool and ask before changing runner setup.",
            evidence=("command not found",),
        )

    return None


def decide_workflow_parse_error(
    message: str,
    workflow_path: str,
    policy: AutomationPolicy | None = None,
) -> AutomationDecision:
    policy = policy or AutomationPolicy()
    lower = message.lower()
    is_workflow = workflow_path.startswith(".github/workflows/")
    parse_error = "dependency_file_not_parseable" in lower or "yaml" in lower

    if is_workflow and parse_error:
        disposition = AUTO_ACT if policy.auto_fix_workflows else NEEDS_APPROVAL
        return AutomationDecision(
            disposition=disposition,
            category="workflow-parse-error",
            title=f"{workflow_path} is not parseable",
            reason="Workflow YAML changes affect scheduled/background automation boundaries.",
            recommended_action=(
                "Fix and validate the workflow only when workflow edits are explicitly authorized."
            ),
            evidence=(message,),
        )

    return AutomationDecision(
        disposition=NO_OP,
        category="no-workflow-action",
        title=f"{workflow_path} has no autonomous workflow action",
        reason="The message does not match a protected workflow parse failure.",
        recommended_action="Leave unchanged unless a human asks for follow-up.",
        evidence=(message,),
    )


def _pr_evidence(pr: PullRequestState, risk_reasons: list[str]) -> tuple[str, ...]:
    evidence = [
        f"checks={pr.check_status}",
        f"mergeState={pr.merge_state or 'unknown'}",
        f"review={pr.review_decision or 'unknown'}",
        f"draft={pr.is_draft}",
    ]
    if pr.changed_files:
        evidence.append(f"files={', '.join(pr.changed_files)}")
    if risk_reasons:
        evidence.append(f"risk={', '.join(risk_reasons)}")
    return tuple(evidence)


def _touched_repo_evidence(candidate: TouchedRepoCandidate) -> tuple[str, ...]:
    evidence = list(candidate.changed_paths)
    if candidate.score_components:
        evidence.extend(f"score:{component}" for component in candidate.score_components)
    if candidate.preserved_paths:
        evidence.extend(f"preserved:{path}" for path in candidate.preserved_paths)
    if candidate.stale_ci:
        evidence.extend(f"stale-ci:{item}" for item in candidate.stale_ci)
    if candidate.approval_only_findings:
        evidence.extend(f"approval-only:{item}" for item in candidate.approval_only_findings)
    return tuple(evidence)


def _linked_worktree_evidence(
    repo: RepoState,
    worktree: LinkedWorktreeState,
) -> tuple[str, ...]:
    return (
        f"path={worktree.path}",
        f"branch={worktree.branch or 'unknown'}",
        f"upstream={worktree.upstream or 'unknown'}",
        f"upstream_gone={worktree.upstream_gone}",
        f"default_ref={worktree.default_ref or repo.default_ref or repo.default_branch}",
        f"tree_matches_default={worktree.tree_matches_default}",
        f"unique_commit_count={worktree.unique_commit_count}",
        f"dirty_files={len(worktree.dirty_files)}",
        f"untracked_files={len(worktree.untracked_files)}",
        *(f"error={error}" for error in worktree.errors),
    )
