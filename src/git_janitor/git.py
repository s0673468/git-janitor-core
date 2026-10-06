from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import TYPE_CHECKING

from .models import BranchState, CommandResult, LinkedWorktreeState, RepoState

if TYPE_CHECKING:
    from .config import ScannerConfig


GITHUB_REMOTE_RE = re.compile(
    r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class WorktreeEntry:
    path: str
    head: str | None = None
    branch: str | None = None


def run_command(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = 45,
) -> CommandResult:
    command = list(args)
    if command[:1] == ["gh"] and (pinned_gh := os.environ.get("GIT_JANITOR_GH_EXECUTABLE")):
        if (
            not os.path.isabs(pinned_gh)
            or os.path.abspath(pinned_gh) != pinned_gh
            or any(character in pinned_gh for character in "\r\n")
        ):
            return CommandResult(
                args=args,
                returncode=127,
                stdout="",
                stderr="pinned gh executable path is invalid",
            )
        command[0] = pinned_gh
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # Preserve exact diff bytes for reproducible source and historical receipt hashes.
        # Other commands retain the historical rstrip behavior; leading status
        # columns remain significant and must survive.
        stdout = (
            completed.stdout
            if command[:2] == ["git", "diff"]
            else completed.stdout.rstrip()
        )
        return CommandResult(
            args=command,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=completed.stderr.strip(),
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            args=command,
            returncode=124,
            stdout=(exc.stdout or "").rstrip() if isinstance(exc.stdout, str) else "",
            stderr=f"command timed out after {timeout}s",
        )
    except OSError as exc:
        return CommandResult(args=command, returncode=127, stdout="", stderr=str(exc))


def discover_repos(
    config: ScannerConfig,
    *,
    errors: list[str] | None = None,
) -> list[Path]:
    found: dict[str, Path] = {}
    physical_checkouts: set[tuple[int, int]] = set()

    def register_checkout(path: Path) -> None:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            resolved = path.absolute()
            if errors is not None:
                errors.append(f"could not resolve checkout path {path}: {exc}")
        try:
            checkout_stat = resolved.stat()
            # Directory identity distinguishes linked checkouts even though
            # they share a Git common directory. Do not case-fold names: they
            # can denote distinct directories on a case-sensitive filesystem.
            if checkout_stat.st_ino:
                identity = (checkout_stat.st_dev, checkout_stat.st_ino)
                if identity in physical_checkouts:
                    return
                physical_checkouts.add(identity)
            elif errors is not None:
                errors.append(f"checkout identity unavailable for {path}: filesystem inode unknown")
        except OSError as exc:
            if errors is not None:
                errors.append(f"checkout identity unavailable for {path}: {exc}")
        # Unknown physical identity preserves each distinct path spelling.
        found[str(resolved)] = resolved

    for repo in config.repos:
        if (repo / ".git").exists():
            register_checkout(repo)
        elif errors is not None:
            errors.append(f"configured repository is missing or not a Git checkout: {repo}")

    for root in config.scan_roots:
        if not root.exists():
            if errors is not None:
                errors.append(f"configured scan root is missing: {root}")
            continue
        if not root.is_dir():
            if errors is not None:
                errors.append(f"configured scan root is not a directory: {root}")
            continue
        for path in _walk_git_repos(
            root.resolve(),
            config.max_depth,
            config.exclude_dirs,
            errors=errors,
        ):
            register_checkout(path)

    return [found[key] for key in sorted(found)]


def _walk_git_repos(
    root: Path,
    max_depth: int,
    excludes: set[str],
    *,
    errors: list[str] | None = None,
) -> list[Path]:
    repos: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if current.name in excludes:
            continue
        if (current / ".git").exists():
            repos.append(current)
            continue
        if depth >= max_depth:
            continue
        try:
            children = sorted(p for p in current.iterdir() if p.is_dir())
        except OSError as exc:
            if errors is not None:
                errors.append(f"could not inspect scan path {current}: {exc}")
            continue
        for child in reversed(children):
            if child.name not in excludes:
                stack.append((child, depth + 1))
    return repos


def parse_github_remote(remote_url: str | None) -> str | None:
    if not remote_url:
        return None
    match = GITHUB_REMOTE_RE.search(remote_url.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def parse_status_porcelain(output: str) -> tuple[list[str], list[str], int, int, str | None]:
    dirty: list[str] = []
    untracked: list[str] = []
    ahead = 0
    behind = 0
    upstream: str | None = None

    for line in output.splitlines():
        if line.startswith("## "):
            upstream = _parse_upstream(line)
            ahead_match = re.search(r"ahead (\d+)", line)
            behind_match = re.search(r"behind (\d+)", line)
            ahead = int(ahead_match.group(1)) if ahead_match else 0
            behind = int(behind_match.group(1)) if behind_match else 0
            continue
        if not line:
            continue
        path = line[3:] if len(line) > 3 else line
        if line.startswith("?? "):
            untracked.append(path)
        else:
            dirty.append(path)
    return dirty, untracked, ahead, behind, upstream


def status_upstream_gone(output: str) -> bool:
    for line in output.splitlines():
        if line.startswith("## "):
            return "[gone]" in line
    return False


def parse_worktree_porcelain(output: str) -> list[WorktreeEntry]:
    entries: list[WorktreeEntry] = []
    current: dict[str, str | None] = {}
    for line in output.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(_worktree_entry(current))
            current = {"path": line.removeprefix("worktree ")}
            continue
        if not current:
            continue
        if line.startswith("HEAD "):
            current["head"] = line.removeprefix("HEAD ")
        elif line.startswith("branch "):
            current["branch"] = _short_branch_name(line.removeprefix("branch "))

    if current:
        entries.append(_worktree_entry(current))
    return entries


def _parse_upstream(branch_header: str) -> str | None:
    if "..." not in branch_header:
        return None
    right = branch_header.split("...", 1)[1]
    return right.split(" ", 1)[0].strip() or None


def scan_repo(path: Path, config: ScannerConfig) -> RepoState:
    repo = RepoState(path=str(path), name=path.name, default_branch=config.default_branch)

    if config.fetch_prune:
        fetch = run_command(
            ["git", "fetch", "--prune", "origin"],
            cwd=path,
            timeout=config.command_timeout_seconds,
        )
        repo.fetch_prune_status = (
            "ok" if fetch.returncode == 0
            else fetch.stderr or fetch.stdout or f"git fetch failed (exit {fetch.returncode})"
        )
        if fetch.returncode != 0:
            repo.errors.append(f"remote freshness unproved: {repo.fetch_prune_status}")

    remote = run_command(
        ["git", "remote", "get-url", "origin"],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if remote.returncode == 0:
        repo.remote_url = remote.stdout
        repo.github_repo = parse_github_remote(remote.stdout)

    branch = run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if branch.returncode == 0:
        repo.current_branch = branch.stdout
    else:
        repo.errors.append(branch.stderr or "failed to identify current branch")

    head = run_command(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if head.returncode == 0:
        repo.head_oid = head.stdout
    else:
        repo.errors.append(head.stderr or "failed to identify checkout HEAD")

    default_ref = _default_ref(path, config)
    if default_ref:
        default_head = run_command(
            ["git", "rev-parse", "--verify", default_ref],
            cwd=path,
            timeout=config.command_timeout_seconds,
        )
        if default_head.returncode == 0:
            repo.default_oid = default_head.stdout
        else:
            repo.errors.append(default_head.stderr or f"failed to verify remote default ref {default_ref}")
            default_ref = None
    repo.default_ref = default_ref
    repo.default_branch = default_ref.removeprefix("origin/") if default_ref else config.default_branch
    if default_ref is None:
        repo.errors.append("remote default ref unavailable; merge and unique-commit comparisons unknown")
    elif repo.head_oid:
        repo.head_unique_commit_count = _unique_commit_count(
            path, repo.default_oid or default_ref, repo.head_oid, config, errors=repo.errors,
        )

    status = run_command(
        ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if status.returncode == 0:
        dirty, untracked, ahead, behind, upstream = parse_status_porcelain(status.stdout)
        repo.dirty_files = dirty
        repo.untracked_files = untracked
        repo.ahead = ahead
        repo.behind = behind
        repo.upstream = upstream
    else:
        repo.errors.append(status.stderr or "failed to read git status")

    repo.branches = _scan_branches(
        path, default_ref, repo.current_branch, config, errors=repo.errors,
    )
    for branch_state in repo.branches:
        if branch_state.current:
            repo.tracking_configured = branch_state.tracking_configured
            repo.tracking_remote = branch_state.tracking_remote
            repo.tracking_merge = branch_state.tracking_merge
            break
    repo.linked_worktrees, worktree_errors = _scan_linked_worktrees(path, default_ref, config)
    repo.errors.extend(worktree_errors)
    return repo


def _default_ref(path: Path, config: ScannerConfig) -> str | None:
    origin_head = run_command(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if origin_head.returncode == 0 and origin_head.stdout:
        return origin_head.stdout

    fallback = f"origin/{config.default_branch}"
    exists = run_command(
        ["git", "rev-parse", "--verify", "--quiet", fallback],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if exists.returncode == 0:
        return fallback
    return None


def _tracking_configuration(
    path: Path, branch: str, config: ScannerConfig, errors: list[str],
) -> tuple[bool | None, str | None, str | None]:
    """Read configured tracking without inventing an upstream from a remote name."""
    values: list[str | None] = []
    present = False
    failed = False
    for key in ("remote", "merge"):
        result = run_command(
            ["git", "config", "--get", f"branch.{branch}.{key}"],
            cwd=path, timeout=config.command_timeout_seconds,
        )
        if result.returncode == 0:
            present = True
            value = result.stdout or None
            # A branch remote may be a credential-bearing URL rather than a
            # configured remote name. Config stdout/stderr must not leak it.
            if value and key == "remote" and not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value):
                value = "[remote location redacted]"
            elif value and key == "merge" and not re.fullmatch(r"refs/[A-Za-z0-9_./-]+", value):
                value = "[merge target redacted]"
            values.append(value)
        elif result.returncode == 1:
            values.append(None)
        else:
            failed = True
            values.append(None)
            errors.append(
                f"branch {branch}: tracking configuration {key} inspection failed "
                f"(exit {result.returncode}); comparison unknown"
            )
    return None if failed else present, values[0], values[1]


def _tracking_comparison_gap(
    branch: str, configured: bool | None, upstream: str | None, gone: bool,
    errors: list[str],
) -> None:
    if configured is True and upstream is None:
        errors.append(
            f"branch {branch}: tracking configured but local upstream mapping unavailable; "
            "publication and comparison unknown"
        )
    elif gone:
        errors.append(
            f"branch {branch}: local upstream ref unavailable ([gone]); "
            "remote existence, publication and comparison unknown"
        )


def _scan_branches(
    path: Path,
    default_ref: str | None,
    current_branch: str | None,
    config: ScannerConfig,
    *,
    errors: list[str] | None = None,
) -> list[BranchState]:
    branches_raw = run_command(
        [
            "git",
            "for-each-ref",
            "--format=%(refname:short)|%(upstream:short)|%(committerdate:iso8601)|%(subject)",
            "refs/heads",
        ],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if branches_raw.returncode != 0:
        if errors is not None:
            errors.append(branches_raw.stderr or "failed to enumerate local branches")
        if not branches_raw.stdout:
            return []

    branches: list[BranchState] = []
    for line in branches_raw.stdout.splitlines():
        name, upstream, date, subject = (line.split("|", 3) + ["", "", "", ""])[:4]
        state = BranchState(
            name=name,
            upstream=upstream or None,
            last_commit_iso=date or None,
            last_subject=subject or None,
            current=name == current_branch,
        )
        inspection_errors = errors if errors is not None else []
        state.tracking_configured, state.tracking_remote, state.tracking_merge = (
            _tracking_configuration(path, name, config, inspection_errors)
        )
        if state.upstream:
            exists = run_command(
                ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{state.upstream}"],
                cwd=path,
                timeout=config.command_timeout_seconds,
            )
            # Local upstreams are also valid Git configurations.
            if exists.returncode != 0:
                exists = run_command(
                    ["git", "rev-parse", "--verify", "--quiet", state.upstream],
                    cwd=path,
                    timeout=config.command_timeout_seconds,
                )
            if exists.returncode == 1:
                state.upstream_gone = True
            elif exists.returncode != 0:
                if errors is not None:
                    errors.append(exists.stderr or f"failed to inspect upstream for {name}")
            else:
                counts = run_command(
                    ["git", "rev-list", "--left-right", "--count", f"{state.upstream}...{name}"],
                    cwd=path,
                    timeout=config.command_timeout_seconds,
                )
                try:
                    if counts.returncode != 0:
                        raise ValueError
                    state.behind, state.ahead = (int(part) for part in counts.stdout.split())
                except ValueError:
                    if errors is not None:
                        errors.append(counts.stderr or f"upstream divergence unavailable for {name}")
        _tracking_comparison_gap(
            name, state.tracking_configured, state.upstream, state.upstream_gone,
            inspection_errors,
        )
        if default_ref and name != default_ref:
            state.merged_to_default = _is_ancestor(path, name, default_ref, config, errors=errors)
            state.unique_commit_count = _unique_commit_count(path, default_ref, name, config, errors=errors)
        branches.append(state)
    return branches


def _scan_linked_worktrees(
    path: Path,
    default_ref: str | None,
    config: ScannerConfig,
) -> tuple[list[LinkedWorktreeState], list[str]]:
    worktrees_raw = run_command(
        ["git", "worktree", "list", "--porcelain"],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if worktrees_raw.returncode != 0:
        return [], [worktrees_raw.stderr or "failed to list git worktrees"]

    linked: list[LinkedWorktreeState] = []
    errors: list[str] = []
    for entry in parse_worktree_porcelain(worktrees_raw.stdout):
        worktree_path = Path(entry.path)
        if _same_path(worktree_path, path):
            continue
        state = _inspect_linked_worktree(entry, default_ref, config)
        linked.append(state)
        errors.extend(f"{state.path}: {error}" for error in state.errors)
    return linked, errors


def _inspect_linked_worktree(
    entry: WorktreeEntry,
    default_ref: str | None,
    config: ScannerConfig,
) -> LinkedWorktreeState:
    worktree_path = Path(entry.path)
    state = LinkedWorktreeState(
        path=entry.path,
        branch=entry.branch,
        head=entry.head,
        default_ref=default_ref,
    )
    status = run_command(
        ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
        cwd=worktree_path,
        timeout=config.command_timeout_seconds,
    )
    if status.returncode != 0:
        state.errors.append(status.stderr or "failed to read linked worktree status")
        return state

    dirty, untracked, ahead, behind, upstream = parse_status_porcelain(status.stdout)
    state.dirty_files = dirty
    state.untracked_files = untracked
    state.upstream = upstream
    state.ahead = ahead
    state.behind = behind
    state.upstream_gone = status_upstream_gone(status.stdout)
    if entry.branch:
        state.tracking_configured, state.tracking_remote, state.tracking_merge = (
            _tracking_configuration(worktree_path, entry.branch, config, state.errors)
        )
        _tracking_comparison_gap(
            entry.branch, state.tracking_configured, state.upstream,
            state.upstream_gone, state.errors,
        )

    if not default_ref:
        state.errors.append("default ref unavailable for linked worktree comparison")
        return state

    tree_diff = run_command(
        ["git", "diff", "--quiet", default_ref, "HEAD"],
        cwd=worktree_path,
        timeout=config.command_timeout_seconds,
    )
    if tree_diff.returncode == 0:
        state.tree_matches_default = True
    elif tree_diff.returncode == 1:
        state.tree_matches_default = False
    else:
        state.errors.append(tree_diff.stderr or "failed to compare linked worktree tree")

    state.unique_commit_count = _unique_commit_count(
        worktree_path, default_ref, "HEAD", config, errors=state.errors,
    )
    return state


def _is_ancestor(
    path: Path, branch: str, default_ref: str, config: ScannerConfig,
    *, errors: list[str] | None = None,
) -> bool:
    result = run_command(
        ["git", "merge-base", "--is-ancestor", branch, default_ref],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if result.returncode not in (0, 1) and errors is not None:
        errors.append(result.stderr or f"merge proof unavailable for {branch}")
    return result.returncode == 0


def _unique_commit_count(
    path: Path,
    default_ref: str,
    branch: str,
    config: ScannerConfig,
    *,
    errors: list[str] | None = None,
) -> int | None:
    result = run_command(
        ["git", "cherry", "-v", default_ref, branch],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if result.returncode != 0:
        if errors is not None:
            errors.append(result.stderr or f"unique-commit comparison unavailable for {branch}")
        return None
    return sum(1 for line in result.stdout.splitlines() if line.startswith("+"))


def _worktree_entry(raw: dict[str, str | None]) -> WorktreeEntry:
    return WorktreeEntry(
        path=raw.get("path") or "",
        head=raw.get("head"),
        branch=raw.get("branch"),
    )


def _short_branch_name(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def _same_path(left: Path, right: Path) -> bool:
    try:
        left_stat, right_stat = left.stat(), right.stat()
        if left_stat.st_ino and right_stat.st_ino:
            return (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)
    except OSError:
        pass
    # Missing or inaccessible identity is not proof that casing variants
    # alias. Retain the prior lexical fallback without case normalization.
    try:
        return left.resolve() == right.resolve()
    except (OSError, RuntimeError):
        return str(left) == str(right)
