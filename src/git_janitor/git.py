from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import TYPE_CHECKING

from .models import BranchState, CommandResult, LinkedWorktreeState, RepoState

if TYPE_CHECKING:
    from .config import ScannerConfig


GITHUB_REMOTE_RE = re.compile(
    r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

# General scans expose untracked work without enumerating ignored build trees.
STATUS_FLAGS = ("--untracked-files=normal", "--ignore-submodules=none")
# Deletion proofs must also account for files that Git would otherwise ignore.
CLEANUP_STATUS_FLAGS = ("--untracked-files=all", "--ignored", "--ignore-submodules=none")


def fast_forward_blockers(
    path: Path, head: str, upstream: str, runner=None, timeout: int = 45,
) -> list[str]:
    """Check incoming additions without enumerating ignored local trees.

    Ignored files are clean worktree state, but Git normally overwrites them
    during a merge. Inspect only paths newly tracked by the target, including
    file/directory and symlink obstructions in their parents. HEAD entries
    removed by the target are safe to replace. Execution must also use
    --no-overwrite-ignore to protect files created after this check.
    """
    if runner is None:
        runner = run_command
    additions = runner(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
         "--name-only", "--diff-filter=A", "-z", head, upstream, "--"],
        path, timeout,
    )
    if additions.returncode != 0:
        return ["incoming path inspection failed; fast-forward overwrite safety unknown"]
    removed_paths: set[str] | None = None
    for name in additions.stdout.split("\0"):
        if not name:
            continue
        parts = Path(name).parts
        if Path(name).is_absolute() or ".." in parts:
            return ["incoming path is invalid; fast-forward overwrite safety unknown"]
        current = path
        for index, part in enumerate(parts):
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                break
            except OSError:
                return ["incoming path inspection failed; fast-forward overwrite safety unknown"]
            if index == len(parts) - 1 or not stat.S_ISDIR(mode):
                if removed_paths is None:
                    trees = []
                    for revision in (head, upstream):
                        tree = runner(
                            ["git", "ls-tree", "-r", "--name-only", "-z", revision, "--"],
                            path, timeout,
                        )
                        if tree.returncode != 0:
                            return ["HEAD/target tree inspection failed; "
                                    "fast-forward overwrite safety unknown"]
                        trees.append(set(filter(None, tree.stdout.split("\0"))))
                    removed_paths = trees[0] - trees[1]
                obstruction = "/".join(parts[:index + 1])
                if any(_tracked_obstruction(path, obstruction, tracked)
                       for tracked in removed_paths):
                    # Git will remove this HEAD entry before creating the addition.
                    break
                return ["fast-forward would overwrite local files; preservation required"]
    return []


def _tracked_obstruction(root: Path, name: str, tracked: str) -> bool:
    if name == tracked:
        return True
    if name.casefold() != tracked.casefold():
        return False
    try:
        local, original = (root / name).lstat(), (root / tracked).lstat()
        if not local.st_ino or (local.st_dev, local.st_ino) != (original.st_dev, original.st_ino):
            return False
        # Case aliases share an entry on insensitive filesystems. Two actual
        # spellings (including hard links) are distinct local files to preserve.
        parent = root
        for part, old_part in zip(Path(name).parts, Path(tracked).parts):
            if part != old_part:
                names = os.listdir(parent)
                if part in names and old_part in names:
                    return False
            parent /= part
        return True
    except OSError:
        return False


def ignored_cleanup_blockers(path: Path, timeout: int = 45) -> list[str]:
    """Inspect a cleanup target without expanding ignored directory contents.

    Keep this evidence separate from ordinary scan errors: ignored build output
    blocks deletion, but does not make unrelated fast-forwards unsafe.
    """
    ignored = run_command(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard",
         "--directory", "--no-empty-directory", "-z", "--", "."],
        cwd=path, timeout=timeout,
    )
    if ignored.returncode != 0:
        return ["ignored-file inspection failed; cleanup safety unknown"]
    return [f"ignored files require preservation before cleanup: {name}"
            for name in ignored.stdout.split("\0") if name]


def cleanup_blockers(path: Path, runner=None, timeout: int = 45) -> list[str]:
    """Fail closed on saved work and nested repositories needing separate proof.

    Stash subjects are not a reliable branch identity (and can contain private
    text). Inspect only object IDs and conservatively preserve all stashed work.
    A superproject's status cannot prove ignored files or unpublished commits in
    its submodules are preserved, even with --ignore-submodules=none.
    """
    return (stash_cleanup_blockers(path, runner, timeout)
            + submodule_cleanup_blockers(path, runner, timeout))


def stash_cleanup_blockers(path: Path, runner=None, timeout: int = 45) -> list[str]:
    if runner is None:
        def runner(args, cwd, timeout):
            return run_command(args, cwd=cwd, timeout=timeout)
    blockers: list[str] = []
    stashes = runner(["git", "stash", "list", "--format=%H"], path, timeout)
    if stashes.returncode != 0:
        blockers.append("stash inspection failed; cleanup safety unknown")
    elif stashes.stdout:
        blockers.append("stashed work requires preservation before cleanup")
    return blockers


def submodule_cleanup_blockers(path: Path, runner=None, timeout: int = 45) -> list[str]:
    if runner is None:
        def runner(args, cwd, timeout):
            return run_command(args, cwd=cwd, timeout=timeout)
    blockers: list[str] = []
    index = runner(["git", "ls-files", "--stage", "-z"], path, timeout)
    if index.returncode != 0:
        blockers.append("submodule inspection failed; cleanup safety unknown")
    elif any(record.startswith("160000 ") for record in index.stdout.split("\0")):
        blockers.append("submodules require separate preservation proof before cleanup")
    return blockers


@dataclass(frozen=True)
class WorktreeEntry:
    path: str
    head: str | None = None
    branch: str | None = None
    prunable: bool = False


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
    match = GITHUB_REMOTE_RE.fullmatch(remote_url.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def parse_status_porcelain(
    output: str, *, ignored_files: list[str] | None = None,
) -> tuple[list[str], list[str], int, int, str | None]:
    """Parse work separately from ignored files, optionally collecting the latter.

    Keep the five-value result for callers that only need ordinary work status.
    Cleanup callers must collect ignored_files and check them before deletion.
    """
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
        elif line.startswith("!! "):
            if ignored_files is not None:
                ignored_files.append(path)
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
    # Git separates records with LF; Unicode separators can be part of a path.
    for line in output.split("\n"):
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
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = "yes"

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
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if branch.returncode == 0:
        repo.current_branch = branch.stdout.removeprefix("refs/heads/")
    elif branch.returncode == 1:
        repo.current_branch = "HEAD"
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
            ["git", "rev-parse", "--verify", f"refs/remotes/{default_ref}"],
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
        ["git", "status", "--porcelain=v1", "--branch", *STATUS_FLAGS],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if status.returncode == 0:
        dirty, untracked, ahead, behind, upstream = parse_status_porcelain(
            status.stdout, ignored_files=repo.ignored_files,
        )
        repo.dirty_files = dirty
        repo.untracked_files = untracked
        repo.ahead = ahead
        repo.behind = behind
        repo.upstream = upstream
        if behind and not ahead and upstream and repo.head_oid and not (dirty or untracked):
            repo.errors.extend(fast_forward_blockers(
                path, repo.head_oid, upstream, timeout=config.command_timeout_seconds,
            ))
    else:
        repo.errors.append(status.stderr or "failed to read git status")

    shared_blockers = stash_cleanup_blockers(path, timeout=config.command_timeout_seconds)
    repo.cleanup_blockers.extend(shared_blockers)
    repo.cleanup_blockers.extend(submodule_cleanup_blockers(
        path, timeout=config.command_timeout_seconds,
    ))

    repo.branches = _scan_branches(
        path, default_ref, repo.current_branch, config, errors=repo.errors,
    )
    for branch_state in repo.branches:
        if branch_state.current:
            repo.tracking_configured = branch_state.tracking_configured
            repo.tracking_remote = branch_state.tracking_remote
            repo.tracking_merge = branch_state.tracking_merge
            break
    repo.linked_worktrees, worktree_errors = _scan_linked_worktrees(
        path, default_ref, config, shared_cleanup_blocked=bool(shared_blockers),
    )
    repo.errors.extend(worktree_errors)
    linked_branches = {worktree.branch for worktree in repo.linked_worktrees}
    if any(
        not branch.current and branch.name != repo.default_branch
        and branch.name not in linked_branches
        and branch.merged_to_default and branch.unique_commit_count == 0
        for branch in repo.branches
    ):
        repo.cleanup_blockers.extend(ignored_cleanup_blockers(path, config.command_timeout_seconds))
    return repo


def _default_ref(path: Path, config: ScannerConfig) -> str | None:
    origin_head = run_command(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        cwd=path,
        timeout=config.command_timeout_seconds,
    )
    if origin_head.returncode == 0 and origin_head.stdout.startswith("refs/remotes/origin/"):
        return origin_head.stdout.removeprefix("refs/remotes/")

    fallback = f"origin/{config.default_branch}"
    exists = run_command(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/{fallback}"],
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
            "--format=%(refname:lstrip=2)%00%(upstream:short)%00%(committerdate:iso8601)%00%(subject)",
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
    for line in branches_raw.stdout.split("\n"):
        if not line:
            continue
        name, upstream, date, subject = (line.split("\0", 3) + ["", "", "", ""])[:4]
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
            state.merged_to_default = _is_ancestor(
                path, f"refs/heads/{name}", f"refs/remotes/{default_ref}", config, errors=errors,
            )
            state.unique_commit_count = _unique_commit_count(
                path, f"refs/remotes/{default_ref}", f"refs/heads/{name}", config, errors=errors,
            )
        branches.append(state)
    return branches


def _scan_linked_worktrees(
    path: Path,
    default_ref: str | None,
    config: ScannerConfig,
    *,
    shared_cleanup_blocked: bool | None = None,
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
    if shared_cleanup_blocked is None:
        shared_cleanup_blocked = bool(stash_cleanup_blockers(
            path, timeout=config.command_timeout_seconds,
        ))
    for entry in parse_worktree_porcelain(worktrees_raw.stdout):
        worktree_path = Path(entry.path)
        if _same_path(worktree_path, path):
            continue
        state = _inspect_linked_worktree(entry, default_ref, config, shared_cleanup_blocked)
        linked.append(state)
        errors.extend(f"{state.path}: {error}" for error in state.errors)
    return linked, errors


def _inspect_linked_worktree(
    entry: WorktreeEntry,
    default_ref: str | None,
    config: ScannerConfig,
    shared_cleanup_blocked: bool,
) -> LinkedWorktreeState:
    worktree_path = Path(entry.path)
    state = LinkedWorktreeState(
        path=entry.path,
        branch=entry.branch,
        head=entry.head,
        default_ref=default_ref,
    )
    if entry.prunable:
        # A broken or missing .git link makes git read an enclosing checkout instead.
        state.errors.append("worktree is prunable; status and commit comparisons unknown")
        return state
    status = run_command(
        ["git", "status", "--porcelain=v1", "--branch", *STATUS_FLAGS],
        cwd=worktree_path,
        timeout=config.command_timeout_seconds,
    )
    if status.returncode != 0:
        state.errors.append(status.stderr or "failed to read linked worktree status")
        return state

    dirty, untracked, ahead, behind, upstream = parse_status_porcelain(
        status.stdout, ignored_files=state.ignored_files,
    )
    state.dirty_files = dirty
    state.untracked_files = untracked
    state.upstream = upstream
    state.ahead = ahead
    state.behind = behind
    state.upstream_gone = status_upstream_gone(status.stdout)
    # Stashes belong to the common repository, not an individual worktree.
    # Keep their detailed evidence once on RepoState and reference it here.
    if shared_cleanup_blocked:
        state.cleanup_blockers.append("shared saved-work blockers apply; see repository cleanup blockers")
    state.cleanup_blockers.extend(submodule_cleanup_blockers(
        worktree_path, timeout=config.command_timeout_seconds,
    ))
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
        ["git", "diff", "--quiet", f"refs/remotes/{default_ref}", "HEAD"],
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
        worktree_path, f"refs/remotes/{default_ref}", "HEAD", config, errors=state.errors,
    )
    if state.upstream_gone and state.tree_matches_default and state.unique_commit_count == 0:
        state.cleanup_blockers.extend(ignored_cleanup_blockers(
            worktree_path, config.command_timeout_seconds,
        ))
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
        prunable=bool(raw.get("prunable")),
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
