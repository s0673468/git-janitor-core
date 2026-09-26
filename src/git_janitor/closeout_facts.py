from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from .git import parse_github_remote, parse_status_porcelain, parse_worktree_porcelain, run_command
from .models import CommandResult


Runner = Callable[[list[str], Path | None, int], CommandResult]
Clock = Callable[[], datetime]
SCHEMA_VERSION = 1


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner = run_command,
    clock: Clock | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Collect current, read-only Git and GitHub evidence for session closeout."
    )
    parser.add_argument(
        "--repo",
        action="append",
        type=Path,
        required=True,
        help="Repository path to inspect. Repeat for multiple repositories.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    args = parser.parse_args(argv)

    clock = clock or _utc_now
    repos = [collect_repo_facts(path, runner=runner, clock=clock) for path in args.repo]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _timestamp(clock()),
        "complete": all(repo["complete"] for repo in repos),
        "repos": repos,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if payload["complete"] else 3


def collect_repo_facts(
    repo_path: Path,
    *,
    runner: Runner = run_command,
    clock: Clock | None = None,
) -> dict[str, Any]:
    clock = clock or _utc_now
    repo_path = repo_path.expanduser().absolute()
    errors: list[str] = []
    error_details: dict[str, str] = {}
    warnings: list[str] = []

    fetch = runner(["git", "fetch", "--prune", "origin"], repo_path, 120)
    fetch_completed_at = _timestamp(clock())
    fetch_ok = fetch.returncode == 0
    if not fetch_ok:
        _error(errors, error_details, "fetch-failed", fetch.stderr or fetch.stdout)

    remote = runner(["git", "remote", "get-url", "origin"], repo_path, 30)
    github_repo = parse_github_remote(remote.stdout) if remote.returncode == 0 else None
    if not github_repo:
        _error(
            errors,
            error_details,
            "github-repo-unresolved",
            remote.stderr or remote.stdout or "origin is missing or is not a GitHub remote",
        )

    github_archived: bool | None = None
    default_branch: str | None = None
    github_meta_ok = False
    if github_repo:
        github_meta = runner(
            [
                "gh",
                "repo",
                "view",
                github_repo,
                "--json",
                "nameWithOwner,isArchived,defaultBranchRef",
            ],
            repo_path,
            45,
        )
        if github_meta.returncode == 0:
            payload = _json_object(github_meta.stdout)
            default = payload.get("defaultBranchRef")
            branch_name = default.get("name") if isinstance(default, dict) else None
            archived = payload.get("isArchived")
            if isinstance(branch_name, str) and branch_name and isinstance(archived, bool):
                default_branch = branch_name
                github_archived = archived
                github_meta_ok = True
            else:
                _error(
                    errors,
                    error_details,
                    "github-metadata-incomplete",
                    "GitHub metadata omitted defaultBranchRef.name or isArchived",
                )
        else:
            _error(
                errors,
                error_details,
                "github-metadata-failed",
                github_meta.stderr or github_meta.stdout,
            )

    origin_head = runner(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        repo_path,
        30,
    )
    origin_head_ref = origin_head.stdout if origin_head.returncode == 0 else None

    remote_default_ref = f"origin/{default_branch}" if default_branch else None
    remote_default_head: str | None = None
    remote_default_ok = False
    if remote_default_ref:
        default_head = runner(
            ["git", "rev-parse", "--verify", remote_default_ref],
            repo_path,
            30,
        )
        if default_head.returncode == 0 and default_head.stdout:
            remote_default_head = default_head.stdout
            remote_default_ok = True
        else:
            _error(
                errors,
                error_details,
                "remote-default-ref-missing",
                default_head.stderr or default_head.stdout or remote_default_ref,
            )
        if origin_head_ref and origin_head_ref != remote_default_ref:
            warnings.append("origin-head-mismatch")
    else:
        _error(
            errors,
            error_details,
            "default-branch-unresolved",
            "live GitHub default branch metadata is unavailable",
        )

    checkout = _checkout_facts(
        repo_path,
        remote_default_ref=remote_default_ref if remote_default_ok else None,
        remote_default_head=remote_default_head,
        runner=runner,
        errors=errors,
        error_details=error_details,
    )
    _checkout_warnings(checkout, default_branch, warnings)

    worktrees = _worktree_facts(
        repo_path,
        remote_default_ref=remote_default_ref if remote_default_ok else None,
        remote_default_head=remote_default_head,
        runner=runner,
        errors=errors,
        error_details=error_details,
    )

    open_prs: list[dict[str, Any]] = []
    if github_repo:
        open_prs = _open_pull_requests(
            github_repo,
            repo_path=repo_path,
            runner=runner,
            errors=errors,
            error_details=error_details,
        )

    authoritative_ref = (
        remote_default_ref
        if fetch_ok and github_meta_ok and remote_default_ok
        else None
    )
    return {
        "path": str(repo_path),
        "complete": not errors,
        "github_repo": github_repo,
        "github_archived": github_archived,
        "fetch": {
            "status": "succeeded" if fetch_ok else "failed",
            "completed_at": fetch_completed_at,
            "detail": fetch.stderr or fetch.stdout,
        },
        "default_branch": default_branch,
        "origin_head_ref": origin_head_ref,
        "remote_default_ref": remote_default_ref,
        "remote_default_head": remote_default_head,
        "authoritative_measurement_ref": authoritative_ref,
        "checkout": checkout,
        "worktrees": worktrees,
        "open_prs": open_prs,
        "warnings": warnings,
        "errors": errors,
        "error_details": error_details,
    }


def _checkout_facts(
    repo_path: Path,
    *,
    remote_default_ref: str | None,
    remote_default_head: str | None,
    runner: Runner,
    errors: list[str],
    error_details: dict[str, str],
) -> dict[str, Any]:
    branch_result = runner(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], repo_path, 30)
    branch = branch_result.stdout if branch_result.returncode == 0 else None

    head_result = runner(["git", "rev-parse", "HEAD"], repo_path, 30)
    head = head_result.stdout if head_result.returncode == 0 else None
    if not head:
        _error(
            errors,
            error_details,
            "checkout-head-unresolved",
            head_result.stderr or head_result.stdout,
        )

    status = runner(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
        repo_path,
        30,
    )
    dirty_files: list[str] = []
    untracked_files: list[str] = []
    if status.returncode == 0:
        dirty_files, untracked_files, _ahead, _behind, _upstream = parse_status_porcelain(
            status.stdout
        )
    else:
        _error(
            errors,
            error_details,
            "checkout-status-failed",
            status.stderr or status.stdout,
        )

    comparison = _compare_ref(
        "HEAD",
        remote_default_ref,
        cwd=repo_path,
        runner=runner,
        error_prefix="checkout",
        errors=errors,
        error_details=error_details,
    )
    return {
        "branch": branch,
        "head": head,
        "dirty_files": dirty_files,
        "untracked_files": untracked_files,
        "ahead_of_remote_default": comparison["ahead"],
        "behind_remote_default": comparison["behind"],
        "head_matches_remote_default": bool(
            head and remote_default_head and head == remote_default_head
        ),
        "tree_matches_remote_default": comparison["tree_matches"],
    }


def _worktree_facts(
    repo_path: Path,
    *,
    remote_default_ref: str | None,
    remote_default_head: str | None,
    runner: Runner,
    errors: list[str],
    error_details: dict[str, str],
) -> list[dict[str, Any]]:
    result = runner(["git", "worktree", "list", "--porcelain"], repo_path, 30)
    if result.returncode != 0:
        _error(
            errors,
            error_details,
            "worktree-inventory-failed",
            result.stderr or result.stdout,
        )
        return []

    facts: list[dict[str, Any]] = []
    primary = _normalized_path(repo_path)
    for index, entry in enumerate(parse_worktree_porcelain(result.stdout)):
        if _normalized_path(Path(entry.path)) == primary:
            continue
        worktree_path = Path(entry.path)
        prefix = f"worktree-{index}"
        status = runner(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            worktree_path,
            30,
        )
        dirty_files: list[str] = []
        untracked_files: list[str] = []
        if status.returncode == 0:
            dirty_files, untracked_files, _ahead, _behind, _upstream = parse_status_porcelain(
                status.stdout
            )
        else:
            _error(
                errors,
                error_details,
                f"{prefix}-status-failed",
                status.stderr or status.stdout,
            )
        ref = entry.head or "HEAD"
        comparison = _compare_ref(
            ref,
            remote_default_ref,
            cwd=worktree_path,
            runner=runner,
            error_prefix=prefix,
            errors=errors,
            error_details=error_details,
        )
        facts.append(
            {
                "path": entry.path,
                "branch": entry.branch,
                "head": entry.head,
                "detached": entry.branch is None,
                "dirty_files": dirty_files,
                "untracked_files": untracked_files,
                "ahead_of_remote_default": comparison["ahead"],
                "behind_remote_default": comparison["behind"],
                "head_matches_remote_default": bool(
                    entry.head and remote_default_head and entry.head == remote_default_head
                ),
                "tree_matches_remote_default": comparison["tree_matches"],
            }
        )
    return facts


def _compare_ref(
    left_ref: str,
    remote_default_ref: str | None,
    *,
    cwd: Path,
    runner: Runner,
    error_prefix: str,
    errors: list[str],
    error_details: dict[str, str],
) -> dict[str, int | bool | None]:
    if not remote_default_ref:
        return {"ahead": None, "behind": None, "tree_matches": None}

    divergence = runner(
        [
            "git",
            "rev-list",
            "--left-right",
            "--count",
            f"{left_ref}...{remote_default_ref}",
        ],
        cwd,
        30,
    )
    ahead: int | None = None
    behind: int | None = None
    if divergence.returncode == 0:
        parts = divergence.stdout.replace("\t", " ").split()
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            ahead, behind = (int(part) for part in parts)
        else:
            _error(
                errors,
                error_details,
                f"{error_prefix}-divergence-malformed",
                divergence.stdout,
            )
    else:
        _error(
            errors,
            error_details,
            f"{error_prefix}-divergence-failed",
            divergence.stderr or divergence.stdout,
        )

    tree = runner(
        ["git", "diff", "--quiet", remote_default_ref, left_ref],
        cwd,
        30,
    )
    tree_matches: bool | None
    if tree.returncode == 0:
        tree_matches = True
    elif tree.returncode == 1:
        tree_matches = False
    else:
        tree_matches = None
        _error(
            errors,
            error_details,
            f"{error_prefix}-tree-comparison-failed",
            tree.stderr or tree.stdout,
        )
    return {"ahead": ahead, "behind": behind, "tree_matches": tree_matches}


def _open_pull_requests(
    github_repo: str,
    *,
    repo_path: Path,
    runner: Runner,
    errors: list[str],
    error_details: dict[str, str],
) -> list[dict[str, Any]]:
    result = runner(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{github_repo}/pulls?state=open&per_page=100",
        ],
        repo_path,
        60,
    )
    if result.returncode != 0:
        _error(
            errors,
            error_details,
            "open-pr-inventory-failed",
            result.stderr or result.stdout,
        )
        return []
    if not result.stdout.strip():
        _error(
            errors,
            error_details,
            "open-pr-inventory-malformed",
            "GitHub returned an empty response",
        )
        return []
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        _error(errors, error_details, "open-pr-inventory-malformed", str(exc))
        return []
    if (
        not isinstance(pages, list)
        or not pages
        or any(not isinstance(page, list) for page in pages)
    ):
        _error(
            errors,
            error_details,
            "open-pr-inventory-malformed",
            "expected a slurped list of pages",
        )
        return []

    pull_requests: list[dict[str, Any]] = []
    for raw in (item for page in pages for item in page):
        if not isinstance(raw, dict):
            _error(
                errors,
                error_details,
                "open-pr-inventory-malformed",
                "pull request record is not an object",
            )
            return []
        head = raw.get("head")
        base = raw.get("base")
        if (
            not isinstance(raw.get("number"), int)
            or not isinstance(head, dict)
            or not isinstance(base, dict)
            or not isinstance(head.get("ref"), str)
            or not isinstance(head.get("sha"), str)
            or not isinstance(base.get("ref"), str)
        ):
            _error(
                errors,
                error_details,
                "open-pr-inventory-malformed",
                "pull request record omitted required ref fields",
            )
            return []
        pull_requests.append(
            {
                "number": raw["number"],
                "title": raw.get("title") or "",
                "url": raw.get("html_url"),
                "head_ref": head["ref"],
                "head_sha": head["sha"],
                "base_ref": base["ref"],
                "is_draft": bool(raw.get("draft")),
                "updated_at": raw.get("updated_at"),
            }
        )
    return pull_requests


def _checkout_warnings(
    checkout: dict[str, Any],
    default_branch: str | None,
    warnings: list[str],
) -> None:
    if default_branch and checkout["branch"] != default_branch:
        warnings.append("checkout-not-default")
    ahead = checkout["ahead_of_remote_default"]
    behind = checkout["behind_remote_default"]
    if behind and not ahead:
        warnings.append("checkout-stale-vs-remote-default")
    elif ahead and behind:
        warnings.append("checkout-diverged-from-remote-default")
    if checkout["dirty_files"] or checkout["untracked_files"]:
        warnings.append("checkout-dirty")


def _error(
    errors: list[str],
    details: dict[str, str],
    code: str,
    detail: str,
) -> None:
    if code not in errors:
        errors.append(code)
    details[code] = detail.strip()


def _json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
