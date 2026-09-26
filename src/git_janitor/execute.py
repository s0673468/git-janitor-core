from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from . import git
from .autonomy import AUTO_ACT, AutomationPolicy
from .classify import GREEN_MERGE_STATES, pr_risk_reasons
from .config import ScannerConfig
from .github import classify_check_rollup
from .ledger import AuditLedger, LedgerEntry
from .models import AutomationDecision, CommandResult, PullRequestState


MODE_DRY_RUN = "dry-run"
MODE_APPLY = "apply"
STATUS_SKIPPED = "skipped"
STATUS_DRIFTED = "drifted"
STATUS_WOULD_APPLY = "would-apply"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"

SUPPORTED_CATEGORIES = frozenset(
    {
        "fast-forward-default-branch",
        "delete-merged-branch",
        "merge-green-pr",
        "mark-draft-ready",
    }
)
FORBIDDEN_COMMAND_ARGS = frozenset({"-D", "--force", "--no-verify"})

_PR_TITLE_RE = re.compile(r"^(?P<repo>[^#]+)#(?P<number>\d+):")
_PR_VIEW_FIELDS = (
    "title,url,headRefName,baseRefName,isDraft,mergeStateStatus,"
    "reviewDecision,statusCheckRollup,state,files,additions,deletions"
)

Runner = Callable[[list[str], Path | None, int], CommandResult]


@dataclass(frozen=True)
class ExecutionResult:
    decision: AutomationDecision
    status: str
    command: list[str]
    before: dict[str, Any]
    after: dict[str, Any]
    exit_code: int | None
    rollback_hint: str
    detail: str


@dataclass(frozen=True)
class _Preflight:
    command: list[str]
    repo: str
    before: dict[str, Any]
    after: dict[str, Any]
    rollback_hint: str
    detail: str = ""


class ActionExecutor:
    def __init__(
        self,
        *,
        policy: AutomationPolicy,
        ledger: AuditLedger,
        mode: str,
        runner: Runner | None = None,
        repo_paths: Mapping[str, Path] | None = None,
        config: ScannerConfig | None = None,
        apply_categories: frozenset[str] | None = None,
    ) -> None:
        if mode not in {MODE_DRY_RUN, MODE_APPLY}:
            raise ValueError(f"unsupported execution mode: {mode}")
        self.policy = policy
        self.ledger = ledger
        self.mode = mode
        self.runner = runner or git.run_command
        self.repo_paths = dict(repo_paths or {})
        self.config = config or ScannerConfig()
        self.apply_categories = (
            policy.apply_categories
            if apply_categories is None
            else policy.apply_categories & apply_categories
        )

    def execute(self, decision: AutomationDecision) -> ExecutionResult:
        skip = self._skip_reason(decision)
        if skip:
            return self._record(
                decision,
                status=STATUS_SKIPPED,
                command=[],
                before={},
                after={},
                exit_code=None,
                rollback_hint="No rollback needed; no command ran.",
                detail=skip,
                repo=_decision_repo(decision),
            )

        preflight = self._preflight(decision)
        if preflight.detail:
            return self._record(
                decision,
                status=STATUS_DRIFTED,
                command=preflight.command,
                before=preflight.before,
                after=preflight.after,
                exit_code=None,
                rollback_hint=preflight.rollback_hint,
                detail=preflight.detail,
                repo=preflight.repo,
            )

        if self.mode == MODE_DRY_RUN:
            return self._record(
                decision,
                status=STATUS_WOULD_APPLY,
                command=preflight.command,
                before=preflight.before,
                after=preflight.before,
                exit_code=None,
                rollback_hint=preflight.rollback_hint,
                detail="dry run; no mutating command ran",
                repo=preflight.repo,
            )

        result = self._run(preflight.command, _command_cwd(decision, self.repo_paths))
        after = self._after_state(decision, preflight)
        status = STATUS_APPLIED if result.returncode == 0 else STATUS_FAILED
        detail = result.stderr or result.stdout or ("applied" if result.returncode == 0 else "failed")
        return self._record(
            decision,
            status=status,
            command=preflight.command,
            before=preflight.before,
            after=after,
            exit_code=result.returncode,
            rollback_hint=preflight.rollback_hint,
            detail=detail,
            repo=preflight.repo,
        )

    def _skip_reason(self, decision: AutomationDecision) -> str | None:
        if decision.disposition != AUTO_ACT:
            return f"decision disposition is {decision.disposition}, not auto-act"
        if decision.category not in SUPPORTED_CATEGORIES:
            return f"unsupported execution category: {decision.category}"
        if decision.category in {"fast-forward-default-branch", "delete-merged-branch"}:
            repo_name = _local_repo_name(decision)
            if repo_name not in self.repo_paths:
                return f"no unique repo path configured for {repo_name}"
        if decision.category in {"merge-green-pr", "mark-draft-ready"} and any(
            item.startswith("risk=") for item in decision.evidence
        ):
            return "decision evidence carries high-risk PR reasons"
        if decision.category not in self.apply_categories:
            return f"category {decision.category} is not in the execution allowlist"
        flag = _flag_for_category(decision.category)
        if not getattr(self.policy, flag):
            return f"config flag {flag} is disabled"
        return None

    def _preflight(self, decision: AutomationDecision) -> _Preflight:
        if decision.category == "fast-forward-default-branch":
            return self._preflight_fast_forward(decision)
        if decision.category == "delete-merged-branch":
            return self._preflight_delete_branch(decision)
        if decision.category == "merge-green-pr":
            return self._preflight_merge_pr(decision)
        if decision.category == "mark-draft-ready":
            return self._preflight_mark_ready(decision)
        raise AssertionError(f"unsupported execution category after skip checks: {decision.category}")

    def _preflight_fast_forward(self, decision: AutomationDecision) -> _Preflight:
        repo_name = _local_repo_name(decision)
        path = self._repo_path(repo_name)
        command = ["git", "pull", "--ff-only"]
        before_head = self._run(["git", "rev-parse", "HEAD"], path)
        branch = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
        status = self._run(
            ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
            path,
        )
        before = {
            "repo": str(path),
            "head": before_head.stdout if before_head.returncode == 0 else None,
            "branch": branch.stdout if branch.returncode == 0 else None,
        }
        detail = _first_error(before_head, branch, status)
        if detail:
            return _Preflight(command, str(path), before, before, _ff_rollback(before), detail)

        dirty, untracked, ahead, behind, upstream = git.parse_status_porcelain(status.stdout)
        before.update(
            {
                "dirty_files": dirty,
                "untracked_files": untracked,
                "ahead": ahead,
                "behind": behind,
                "upstream": upstream,
            }
        )
        expected_branch = _evidence_value(decision, "branch") or self.config.default_branch
        expected_upstream = _evidence_value(decision, "upstream")
        detail = _fast_forward_drift_detail(
            current_branch=branch.stdout,
            expected_branch=expected_branch,
            expected_upstream=expected_upstream,
            dirty=dirty,
            untracked=untracked,
            ahead=ahead,
            behind=behind,
            upstream=upstream,
        )
        return _Preflight(command, str(path), before, before, _ff_rollback(before), detail)

    def _preflight_delete_branch(self, decision: AutomationDecision) -> _Preflight:
        repo_name, branch_name = _branch_target(decision)
        path = self._repo_path(repo_name)
        command = ["git", "branch", "-d", branch_name]
        branch_ref = self._run(["git", "rev-parse", branch_name], path)
        current = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
        status = self._run(
            ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
            path,
        )
        default_ref = _evidence_value(decision, "default_ref") or f"origin/{self.config.default_branch}"
        before = {
            "repo": str(path),
            "branch": branch_name,
            "branch_ref": branch_ref.stdout if branch_ref.returncode == 0 else None,
            "current_branch": current.stdout if current.returncode == 0 else None,
            "default_ref": default_ref,
        }
        detail = _first_error(branch_ref, current, status)
        if detail:
            return _Preflight(command, str(path), before, before, _branch_rollback(before), detail)

        dirty, untracked, _ahead, _behind, _upstream = git.parse_status_porcelain(status.stdout)
        before.update({"dirty_files": dirty, "untracked_files": untracked})
        default_branch = _short_ref(default_ref)
        detail = _delete_branch_drift_detail(
            branch_name=branch_name,
            current_branch=current.stdout,
            default_branch=default_branch,
            dirty=dirty,
            untracked=untracked,
        )
        if detail:
            return _Preflight(command, str(path), before, before, _branch_rollback(before), detail)

        merged = self._run(["git", "merge-base", "--is-ancestor", branch_name, default_ref], path)
        if merged.returncode != 0:
            return _Preflight(
                command,
                str(path),
                before,
                before,
                _branch_rollback(before),
                "branch is no longer merged to the default ref",
            )
        cherry = self._run(["git", "cherry", "-v", default_ref, branch_name], path)
        if cherry.returncode != 0:
            return _Preflight(
                command,
                str(path),
                before,
                before,
                _branch_rollback(before),
                cherry.stderr or "failed to re-check unique branch commits",
            )
        unique_commit_count = _unique_commit_count(cherry.stdout)
        before["unique_commit_count"] = unique_commit_count
        if unique_commit_count != 0:
            return _Preflight(
                command,
                str(path),
                before,
                before,
                _branch_rollback(before),
                f"branch has {unique_commit_count} unique commit(s)",
            )
        return _Preflight(command, str(path), before, before, _branch_rollback(before))

    def _preflight_merge_pr(self, decision: AutomationDecision) -> _Preflight:
        target = _pr_target(decision)
        command = ["gh", "pr", "merge", str(target["number"]), "--squash", "--repo", target["repo"]]
        before = self._read_pr_state(target["repo"], target["number"])
        detail = before.pop("_error", "")
        if not detail:
            detail = _merge_pr_drift_detail(before)
        return _Preflight(
            command,
            target["repo"],
            before,
            before,
            "Revert the squash merge commit after validating the revert and its tests.",
            detail,
        )

    def _preflight_mark_ready(self, decision: AutomationDecision) -> _Preflight:
        target = _pr_target(decision)
        command = ["gh", "pr", "ready", str(target["number"]), "--repo", target["repo"]]
        before = self._read_pr_state(target["repo"], target["number"])
        detail = before.pop("_error", "")
        if not detail:
            detail = _mark_ready_drift_detail(before)
        return _Preflight(
            command,
            target["repo"],
            before,
            before,
            "Convert the PR back to draft in GitHub if it was marked ready too early.",
            detail,
        )

    def _read_pr_state(self, repo: str, number: int) -> dict[str, Any]:
        result = self._run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                _PR_VIEW_FIELDS,
            ],
            None,
        )
        if result.returncode != 0:
            return {"repo": repo, "number": number, "_error": result.stderr or "gh pr view failed"}
        try:
            raw = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            return {"repo": repo, "number": number, "_error": f"failed to parse gh pr view: {exc}"}

        files = [
            item.get("path")
            for item in raw.get("files", [])
            if isinstance(item, dict) and item.get("path")
        ]
        pr = PullRequestState(
            repo=repo,
            number=number,
            title=raw.get("title") or "",
            url=raw.get("url"),
            head_ref=raw.get("headRefName") or "",
            base_ref=raw.get("baseRefName"),
            is_draft=bool(raw.get("isDraft")),
            merge_state=raw.get("mergeStateStatus"),
            review_decision=raw.get("reviewDecision"),
            check_status=classify_check_rollup(raw.get("statusCheckRollup")),
            changed_files=files,
            additions=raw.get("additions"),
            deletions=raw.get("deletions"),
        )
        risk_reasons = pr_risk_reasons(pr, self.config)
        return {
            "repo": repo,
            "number": number,
            "state": raw.get("state"),
            "is_draft": pr.is_draft,
            "merge_state": pr.merge_state,
            "review_decision": pr.review_decision,
            "check_status": pr.check_status,
            "changed_files": files,
            "risk_reasons": risk_reasons,
            "url": pr.url,
        }

    def _after_state(self, decision: AutomationDecision, preflight: _Preflight) -> dict[str, Any]:
        if decision.category == "fast-forward-default-branch":
            repo_name = _local_repo_name(decision)
            path = self._repo_path(repo_name)
            head = self._run(["git", "rev-parse", "HEAD"], path)
            return {"head": head.stdout if head.returncode == 0 else None}
        if decision.category == "delete-merged-branch":
            repo_name, branch_name = _branch_target(decision)
            path = self._repo_path(repo_name)
            branch = self._run(["git", "rev-parse", "--verify", "--quiet", branch_name], path)
            return {"branch": branch_name, "exists": branch.returncode == 0, "ref": branch.stdout}
        if decision.category in {"merge-green-pr", "mark-draft-ready"}:
            target = _pr_target(decision)
            state = self._read_pr_state(target["repo"], target["number"])
            state.pop("_error", None)
            return state
        return preflight.after

    def _record(
        self,
        decision: AutomationDecision,
        *,
        status: str,
        command: list[str],
        before: dict[str, Any],
        after: dict[str, Any],
        exit_code: int | None,
        rollback_hint: str,
        detail: str,
        repo: str,
    ) -> ExecutionResult:
        result = ExecutionResult(
            decision=decision,
            status=status,
            command=command,
            before=before,
            after=after,
            exit_code=exit_code,
            rollback_hint=rollback_hint,
            detail=detail,
        )
        self.ledger.record(
            LedgerEntry(
                repo=repo,
                category=decision.category,
                disposition=decision.disposition,
                mode=self.mode,
                command=command,
                before=before,
                after=after,
                exit_code=exit_code,
                status=status,
                rollback_hint=rollback_hint,
                detail=detail,
            )
        )
        return result

    def _run(self, args: list[str], cwd: Path | None) -> CommandResult:
        return self.runner(args, cwd, self.config.command_timeout_seconds)

    def _repo_path(self, repo_name: str) -> Path:
        try:
            return self.repo_paths[repo_name]
        except KeyError as exc:
            raise ValueError(f"no repo path configured for {repo_name}") from exc


def execute_decisions(
    decisions: Sequence[AutomationDecision],
    *,
    policy: AutomationPolicy,
    ledger: AuditLedger,
    mode: str,
    runner: Runner | None = None,
    repo_paths: Mapping[str, Path] | None = None,
    config: ScannerConfig | None = None,
    apply_categories: frozenset[str] | None = None,
) -> list[ExecutionResult]:
    executor = ActionExecutor(
        policy=policy,
        ledger=ledger,
        mode=mode,
        runner=runner,
        repo_paths=repo_paths,
        config=config,
        apply_categories=apply_categories,
    )
    return [executor.execute(decision) for decision in decisions]


def _flag_for_category(category: str) -> str:
    return {
        "fast-forward-default-branch": "auto_fast_forward_default_branch",
        "delete-merged-branch": "auto_delete_merged_branches",
        "merge-green-pr": "auto_merge_green_prs",
        "mark-draft-ready": "auto_mark_drafts_ready",
    }[category]


def _command_cwd(
    decision: AutomationDecision,
    repo_paths: Mapping[str, Path],
) -> Path | None:
    if decision.category in {"merge-green-pr", "mark-draft-ready"}:
        return None
    return repo_paths[_local_repo_name(decision)]


def _local_repo_name(decision: AutomationDecision) -> str:
    repo, _separator, _rest = decision.title.partition(":")
    if not repo:
        raise ValueError(f"could not parse repo from decision title: {decision.title}")
    return repo


def _decision_repo(decision: AutomationDecision) -> str:
    if "#" in decision.title:
        try:
            return _pr_target(decision)["repo"]
        except ValueError:
            return ""
    try:
        return _local_repo_name(decision)
    except ValueError:
        return ""


def _branch_target(decision: AutomationDecision) -> tuple[str, str]:
    prefix = decision.title.split(" can ", 1)[0].split(" appears ", 1)[0]
    repo, separator, branch = prefix.partition(":")
    if not separator or not repo or not branch:
        raise ValueError(f"could not parse branch target from decision title: {decision.title}")
    return repo, branch


def _pr_target(decision: AutomationDecision) -> dict[str, Any]:
    match = _PR_TITLE_RE.search(decision.title)
    if not match:
        raise ValueError(f"could not parse PR target from decision title: {decision.title}")
    return {"repo": match.group("repo"), "number": int(match.group("number"))}


def _evidence_value(decision: AutomationDecision, key: str) -> str | None:
    prefix = f"{key}="
    for item in decision.evidence:
        if item.startswith(prefix):
            return item.removeprefix(prefix)
    return None


def _first_error(*results: CommandResult) -> str:
    for result in results:
        if result.returncode != 0:
            return result.stderr or result.stdout or f"command failed: {' '.join(result.args)}"
    return ""


def _fast_forward_drift_detail(
    *,
    current_branch: str,
    expected_branch: str,
    expected_upstream: str | None,
    dirty: list[str],
    untracked: list[str],
    ahead: int,
    behind: int,
    upstream: str | None,
) -> str:
    if dirty or untracked:
        return "worktree is no longer clean"
    if current_branch != expected_branch:
        return f"current branch is {current_branch}, not {expected_branch}"
    if not upstream:
        return "upstream is no longer resolved"
    if expected_upstream and upstream != expected_upstream:
        return f"upstream is {upstream}, not {expected_upstream}"
    if ahead:
        return f"branch is ahead by {ahead} commit(s)"
    if behind <= 0:
        return "branch is no longer behind upstream"
    return ""


def _delete_branch_drift_detail(
    *,
    branch_name: str,
    current_branch: str,
    default_branch: str,
    dirty: list[str],
    untracked: list[str],
) -> str:
    if dirty or untracked:
        return "worktree is no longer clean"
    if branch_name == current_branch:
        return "branch is now the current branch"
    if branch_name == default_branch:
        return "branch is the default branch"
    return ""


def _merge_pr_drift_detail(state: dict[str, Any]) -> str:
    if state.get("state") != "OPEN":
        return f"PR state is {state.get('state')}, not OPEN"
    if state.get("is_draft"):
        return "PR is now draft"
    if state.get("check_status") != "success":
        return f"checks are {state.get('check_status')}, not success"
    if state.get("merge_state") not in GREEN_MERGE_STATES:
        return f"merge state is {state.get('merge_state')}, not green"
    if state.get("risk_reasons"):
        return f"high-risk PR reasons: {', '.join(state['risk_reasons'])}"
    return ""


def _mark_ready_drift_detail(state: dict[str, Any]) -> str:
    if state.get("state") != "OPEN":
        return f"PR state is {state.get('state')}, not OPEN"
    if not state.get("is_draft"):
        return "PR is no longer draft"
    if state.get("check_status") != "success":
        return f"checks are {state.get('check_status')}, not success"
    if state.get("risk_reasons"):
        return f"high-risk PR reasons: {', '.join(state['risk_reasons'])}"
    return ""


def _unique_commit_count(cherry_stdout: str) -> int:
    return sum(1 for line in cherry_stdout.splitlines() if line.startswith("+"))


def _short_ref(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _ff_rollback(before: dict[str, Any]) -> str:
    head = before.get("head") or "the recorded before ref"
    return f"Inspect before={head} and after refs before any human-approved reset."


def _branch_rollback(before: dict[str, Any]) -> str:
    branch = before.get("branch") or "the branch"
    ref = before.get("branch_ref") or "the recorded before ref"
    return f"Recreate {branch} at {ref} if deletion was wrong."
