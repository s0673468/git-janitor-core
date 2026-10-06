from __future__ import annotations

from pathlib import Path
import json
from typing import TYPE_CHECKING, Any

from .git import run_command
from .models import PullRequestState

if TYPE_CHECKING:
    from .config import ScannerConfig


SUCCESS_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
FAILURE_CONCLUSIONS = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "FAILURE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
STALE_CONCLUSIONS = {"STALE"}
PENDING_STATES = {"EXPECTED", "PENDING", "QUEUED", "REQUESTED", "WAITING", "IN_PROGRESS"}
# Legacy commit statuses report `state`, not `conclusion`. A CheckRun `status`
# is never ERROR or FAILURE, so matching these cannot misread one.
FAILURE_STATES = {"ERROR", "FAILURE"}
# gh's GraphQL author serialization also uses this exact GitHub App spelling.
# Do not infer bot ownership from branch names or arbitrary login prefixes.
DEDICATED_UPDATER_BOT_LOGINS = {"dependabot[bot]", "app/dependabot", "renovate[bot]"}
PR_COLLECTION_LIMIT = 1000


def gh_available(timeout: int = 10) -> bool:
    result = run_command(["gh", "auth", "status"], timeout=timeout)
    return result.returncode == 0


def list_pull_requests(
    repo_full_name: str,
    config: ScannerConfig,
    cwd: Path | None = None,
) -> tuple[list[PullRequestState], list[str]]:
    return _list_pull_requests(
        repo_full_name,
        config,
        cwd=cwd,
        author=config.github_author,
        include_files=True,
    )


def list_open_pull_requests(
    repo_full_name: str,
    config: ScannerConfig,
    cwd: Path | None = None,
) -> tuple[list[PullRequestState], list[str]]:
    return _list_pull_requests(
        repo_full_name,
        config,
        cwd=cwd,
        author=None,
        include_files=False,
    )


def _list_pull_requests(
    repo_full_name: str,
    config: ScannerConfig,
    cwd: Path | None = None,
    *,
    author: str | None,
    include_files: bool,
) -> tuple[list[PullRequestState], list[str]]:
    fields = [
        "number",
        "title",
        "url",
        "headRefName",
        "headRefOid",
        "baseRefName",
        "isDraft",
        "mergeStateStatus",
        "reviewDecision",
        "statusCheckRollup",
        "updatedAt",
        "author",
    ]
    args = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo_full_name,
        "--state",
        "open",
        "--limit",
        str(PR_COLLECTION_LIMIT),
        "--json",
        ",".join(fields),
    ]
    if author:
        args[5:5] = ["--author", author]

    result = run_command(
        args,
        cwd=cwd,
        timeout=config.command_timeout_seconds,
    )
    if result.returncode != 0:
        return [], [f"{repo_full_name}: {result.stderr or 'gh pr list failed'}"]

    try:
        raw_prs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], [f"{repo_full_name}: failed to parse gh output: {exc}"]
    if not isinstance(raw_prs, list):
        return [], [f"{repo_full_name}: gh pr list returned a non-array response"]

    prs: list[PullRequestState] = []
    errors: list[str] = []
    if len(raw_prs) >= PR_COLLECTION_LIMIT:
        errors.append(f"{repo_full_name}: PR collection reached {PR_COLLECTION_LIMIT}; coverage may be truncated")
    for index, raw in enumerate(raw_prs):
        if (
            not isinstance(raw, dict)
            or type(raw.get("number")) is not int
            or raw["number"] <= 0
            or not isinstance(raw.get("headRefName"), str)
        ):
            errors.append(f"{repo_full_name}: invalid PR record at index {index}; coverage is incomplete")
            continue
        if include_files and _authored_by_dedicated_dependency_updater(raw):
            continue
        number = int(raw["number"])
        pr = PullRequestState(
            repo=repo_full_name,
            number=number,
            title=raw.get("title") or "",
            url=raw.get("url"),
            head_ref=raw.get("headRefName") or "",
            base_ref=raw.get("baseRefName"),
            is_draft=bool(raw.get("isDraft")),
            merge_state=raw.get("mergeStateStatus"),
            review_decision=raw.get("reviewDecision"),
            check_status=classify_check_rollup(raw.get("statusCheckRollup")),
            updated_at=raw.get("updatedAt"),
            head_oid=raw.get("headRefOid"),
            author=(raw.get("author") or {}).get("login") if isinstance(raw.get("author"), dict) else None,
        )
        if include_files:
            _enrich_pr_files(pr, config, cwd)
        prs.append(pr)

    return prs, errors


def _authored_by_dedicated_dependency_updater(raw: dict[str, Any]) -> bool:
    """Keep dependency-bot-owned PRs out of autonomous action surfaces."""

    author = raw.get("author") or {}
    login = str(author.get("login") or "").lower() if isinstance(author, dict) else ""
    return login in DEDICATED_UPDATER_BOT_LOGINS


def classify_check_rollup(rollup: Any) -> str:
    if not rollup:
        return "unknown"

    conclusions: list[str] = []
    states: list[str] = []
    _collect_status_values(rollup, conclusions, states)

    normalized_conclusions = {item.upper() for item in conclusions if item}
    normalized_states = {item.upper() for item in states if item}

    if normalized_conclusions & FAILURE_CONCLUSIONS:
        return "failure"
    if normalized_states & FAILURE_STATES:
        return "failure"
    if normalized_conclusions & STALE_CONCLUSIONS:
        return "stale"
    if normalized_states & PENDING_STATES:
        return "pending"
    if normalized_conclusions and normalized_conclusions <= SUCCESS_CONCLUSIONS:
        return "success"
    if not normalized_conclusions and not normalized_states:
        return "unknown"
    return "unknown"


def _collect_status_values(
    value: Any,
    conclusions: list[str],
    states: list[str],
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "conclusion" and isinstance(item, str):
                conclusions.append(item)
            elif key in {"status", "state"} and isinstance(item, str):
                states.append(item)
            else:
                _collect_status_values(item, conclusions, states)
    elif isinstance(value, list):
        for item in value:
            _collect_status_values(item, conclusions, states)


def _enrich_pr_files(
    pr: PullRequestState,
    config: ScannerConfig,
    cwd: Path | None,
) -> None:
    result = run_command(
        [
            "gh",
            "pr",
            "view",
            str(pr.number),
            "--repo",
            pr.repo,
            "--json",
            "files,additions,deletions,changedFiles,headRefOid",
        ],
        cwd=cwd,
        timeout=config.command_timeout_seconds,
    )
    if result.returncode != 0:
        pr.errors.append(result.stderr or "gh pr view failed")
        return
    try:
        raw = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        pr.errors.append(f"failed to parse gh pr view output: {exc}")
        return
    if not isinstance(raw, dict):
        pr.errors.append("gh pr view returned a non-object response")
        return
    observed_head = raw.get("headRefOid")
    if pr.head_oid and observed_head and observed_head != pr.head_oid:
        pr.errors.append("PR head changed between list and file inspection; refresh this snapshot")
        pr.check_status = "stale"
    pr.additions = raw.get("additions")
    pr.deletions = raw.get("deletions")
    files = raw.get("files") or []
    pr.changed_files = [
        item.get("path") for item in files if isinstance(item, dict) and item.get("path")
    ]
    # `gh pr view --json files` caps the list; changedFiles carries the true
    # count. Without this comparison a truncated list looks complete and
    # pr_risk_reasons classifies on the visible slice only.
    changed_total = raw.get("changedFiles")
    if isinstance(changed_total, int) and changed_total > len(pr.changed_files):
        pr.errors.append(
            f"gh returned {len(pr.changed_files)} of {changed_total} changed files; "
            "risk classification ran on a truncated file list"
        )
