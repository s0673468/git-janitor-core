from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .autonomy import (
    AUTO_ACT,
    WAIT,
    AutomationDecision,
    AutomationPolicy,
    TouchedRepoCandidate,
    decide_touched_repo_candidate,
)
from .models import PullRequestState, RepoState


CODEX_PR_PREFIXES = ("codex/", "codex-automation/")
GENERATED_DIR_EXCLUDES = {
    ".dart_tool",
    ".git",
    ".gradle",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
PRESERVED_GENERATED_DIRS = {
    ".understand-anything",
}
DEFAULT_MEMORY_PATH = str(Path("~/.local/state/git-janitor/maintainer/memory.md").expanduser())
DEFAULT_STATE_PATH = str(Path("~/.local/state/git-janitor/maintainer/state.json").expanduser())


@dataclass(frozen=True)
class TouchedRepoMaintainerConfig:
    touched_window_hours: int = 24
    active_window_minutes: int = 60
    max_auto_prs: int = 2
    max_changed_repos: int = 2
    max_file_evidence: int = 12
    memory_path: str = DEFAULT_MEMORY_PATH
    state_path: str = DEFAULT_STATE_PATH
    exclude_dirs: frozenset[str] = frozenset()


def build_touched_repo_candidates(
    repos: list[RepoState],
    pull_requests: list[PullRequestState],
    config: TouchedRepoMaintainerConfig,
    *,
    now: datetime | None = None,
    pr_lookup_errors_by_repo: dict[str, tuple[str, ...]] | None = None,
    approval_only_findings_by_repo: dict[str, tuple[str, ...]] | None = None,
) -> list[TouchedRepoCandidate]:
    now = now or datetime.now().astimezone()
    pr_lookup_errors_by_repo = pr_lookup_errors_by_repo or {}
    approval_only_findings_by_repo = approval_only_findings_by_repo or {}
    touched_delta = timedelta(hours=config.touched_window_hours)
    active_delta = timedelta(minutes=config.active_window_minutes)
    candidates: list[TouchedRepoCandidate] = []

    for repo in repos:
        scope_evidence = _scope_evidence(repo, touched_delta, config, now)
        if not scope_evidence:
            continue

        candidates.append(
            TouchedRepoCandidate(
                repo=repo.name,
                candidate="repo fleet maintainer review",
                safety_score=None,
                recent_files=tuple(
                    _recent_files(repo, active_delta, config, now, prefix="recent-file")
                ),
                repo_errors=tuple(_repo_errors(repo)),
                dirty_files=tuple(_actionable_dirty_files(repo)),
                fast_forward=tuple(_fast_forward_state(repo)),
                remote_state=tuple(_remote_state(repo)),
                publication_boundary=tuple(_publication_boundary(repo)),
                pr_lookup_errors=_repo_lookup_errors(repo, pr_lookup_errors_by_repo),
                open_codex_prs=tuple(_open_codex_prs(repo, pull_requests)),
                stale_ci=tuple(_stale_ci_prs(repo, pull_requests)),
                approval_only_findings=_repo_lookup_errors(
                    repo,
                    approval_only_findings_by_repo,
                ),
                changed_paths=tuple(scope_evidence),
                preserved_paths=tuple(_preserved_paths(repo)),
            )
        )

    return candidates


def plan_touched_repo_maintainer(
    candidates: list[TouchedRepoCandidate],
    policy: AutomationPolicy | None = None,
    *,
    max_auto_prs: int = 2,
    max_changed_repos: int = 2,
    memory_path: str = DEFAULT_MEMORY_PATH,
    state_path: str = DEFAULT_STATE_PATH,
) -> list[AutomationDecision]:
    policy = policy or AutomationPolicy()
    state = _load_state(state_path)
    decisions: list[AutomationDecision] = []
    auto_pr_count = 0
    changed_repos: set[str] = set()

    for candidate in candidates:
        decision = decide_touched_repo_candidate(candidate, policy)
        decision = _apply_state_suppression(decision, candidate, state)
        if decision.disposition == AUTO_ACT:
            opens_pr = decision.category == "low-risk-maintainer-fix"
            changes_repo = decision.category in {
                "fast-forward-default-branch",
                "low-risk-maintainer-fix",
            }
            pr_cap_reached = opens_pr and auto_pr_count >= max_auto_prs
            repo_cap_reached = changes_repo and len(changed_repos) >= max_changed_repos
            if pr_cap_reached or repo_cap_reached:
                decision = AutomationDecision(
                    disposition=WAIT,
                    category="maintainer-run-cap-reached",
                    title=f"{candidate.repo}: maintainer run cap reached",
                    reason=(
                        f"This pass is capped at {max_auto_prs} PRs and "
                        f"{max_changed_repos} changed repos."
                    ),
                    recommended_action="Report the scored candidate and leave it for a later pass.",
                    evidence=(
                        f"candidate={candidate.candidate}",
                        f"safety_score={candidate.safety_score}",
                        *candidate.changed_paths,
                    ),
                )
            else:
                if opens_pr:
                    auto_pr_count += 1
                if changes_repo:
                    changed_repos.add(candidate.repo)

        decisions.append(decision)

    repeated_noop = _repeated_noop_decision(candidates, decisions, state)
    if repeated_noop:
        decisions.append(repeated_noop)
    decisions.append(_memory_write_decision(memory_path, state_path, candidates))
    return decisions


def _scope_evidence(
    repo: RepoState,
    touched_delta: timedelta,
    config: TouchedRepoMaintainerConfig,
    now: datetime,
) -> list[str]:
    evidence: list[str] = []
    commit_evidence = _latest_commit_evidence(repo, touched_delta, now)
    if commit_evidence:
        evidence.append(commit_evidence)
    evidence.extend(_recent_files(repo, touched_delta, config, now, prefix="touched-file"))
    return evidence[: config.max_file_evidence]


def _latest_commit_evidence(
    repo: RepoState,
    touched_delta: timedelta,
    now: datetime,
) -> str | None:
    current_branch = repo.current_branch or repo.default_branch
    branches = sorted(
        repo.branches,
        key=lambda branch: branch.name != current_branch,
    )
    for branch in branches:
        if not branch.last_commit_iso:
            continue
        commit_time = _parse_git_datetime(branch.last_commit_iso)
        if commit_time and now - commit_time <= touched_delta:
            return f"recent-commit:{branch.name}:{branch.last_commit_iso}"
    return None


def _parse_git_datetime(value: str) -> datetime | None:
    cleaned = value.strip()
    if len(cleaned) > 5 and cleaned[-5] in {"+", "-"} and cleaned[-3] != ":":
        cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _recent_files(
    repo: RepoState,
    delta: timedelta,
    config: TouchedRepoMaintainerConfig,
    now: datetime,
    *,
    prefix: str,
) -> list[str]:
    root = Path(repo.path)
    threshold = now.timestamp() - delta.total_seconds()
    matches: list[tuple[float, str]] = []
    excludes = set(config.exclude_dirs) | GENERATED_DIR_EXCLUDES | PRESERVED_GENERATED_DIRS

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in excludes]
        for filename in filenames:
            path = Path(dirpath) / filename
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                continue
            if modified_at < threshold:
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                relative = path
            matches.append((modified_at, f"{prefix}:{relative}"))

    matches.sort(reverse=True)
    return [entry for _, entry in matches[: config.max_file_evidence]]


def _open_codex_prs(
    repo: RepoState,
    pull_requests: list[PullRequestState],
) -> list[str]:
    repo_keys = {repo.name}
    if repo.github_repo:
        repo_keys.add(repo.github_repo)

    matches: list[str] = []
    for pr in pull_requests:
        if pr.repo not in repo_keys:
            continue
        head_ref = pr.head_ref.lower()
        if not head_ref.startswith(CODEX_PR_PREFIXES):
            continue
        url = pr.url or f"{pr.repo}#{pr.number}"
        matches.append(f"{url}:{pr.head_ref}")
    return matches


def _repo_lookup_errors(
    repo: RepoState,
    errors_by_repo: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    errors: list[str] = []
    for key in (repo.name, repo.github_repo):
        if key and key in errors_by_repo:
            errors.extend(errors_by_repo[key])
    return tuple(errors)


def _stale_ci_prs(
    repo: RepoState,
    pull_requests: list[PullRequestState],
) -> list[str]:
    repo_keys = {repo.name}
    if repo.github_repo:
        repo_keys.add(repo.github_repo)

    matches: list[str] = []
    for pr in pull_requests:
        if pr.repo not in repo_keys or pr.check_status != "stale":
            continue
        url = pr.url or f"{pr.repo}#{pr.number}"
        matches.append(f"{url}:checks=stale")
    return matches


def _actionable_dirty_files(repo: RepoState) -> list[str]:
    return [
        path
        for path in (*repo.dirty_files, *repo.untracked_files)
        if not _is_preserved_path(path)
    ]


def _repo_errors(repo: RepoState) -> list[str]:
    errors = list(repo.errors)
    if repo.fetch_prune_status and repo.fetch_prune_status != "ok":
        errors.append(f"fetch_prune_status={repo.fetch_prune_status}")
    return errors


def _preserved_paths(repo: RepoState) -> list[str]:
    return [
        path
        for path in (*repo.dirty_files, *repo.untracked_files)
        if _is_preserved_path(path)
    ]


def _is_preserved_path(path: str) -> bool:
    normalized = path.rstrip("/")
    parts = Path(normalized).parts
    return any(part in PRESERVED_GENERATED_DIRS for part in parts)


def _publication_boundary(repo: RepoState) -> list[str]:
    if not repo.remote_url:
        return ["origin=missing", "github_repo=unresolved"]
    if not repo.github_repo:
        return [f"origin={repo.remote_url}", "github_repo=unresolved"]
    return []


def _fast_forward_state(repo: RepoState) -> list[str]:
    if not repo.behind:
        return []
    if _repo_errors(repo) or _actionable_dirty_files(repo) or _publication_boundary(repo):
        return []
    if repo.ahead:
        return []
    if not repo.current_branch or repo.current_branch != repo.default_branch:
        return []
    expected_upstream = repo.default_ref or f"origin/{repo.default_branch}"
    if repo.upstream != expected_upstream:
        return []
    return [
        f"branch={repo.current_branch}",
        f"upstream={repo.upstream}",
        f"behind={repo.behind}",
    ]


def _remote_state(repo: RepoState) -> list[str]:
    evidence: list[str] = []
    if repo.upstream is None and repo.github_repo:
        evidence.append("upstream=missing")
    if repo.ahead:
        evidence.append(f"ahead={repo.ahead}")
    if repo.behind and not _fast_forward_state(repo):
        evidence.append(f"behind={repo.behind}")
    return evidence


def _load_state(state_path: str) -> dict[str, Any]:
    path = Path(state_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _apply_state_suppression(
    decision: AutomationDecision,
    candidate: TouchedRepoCandidate,
    state: dict[str, Any],
) -> AutomationDecision:
    if decision.category != "publication-boundary-blocked":
        return decision
    if not _publication_boundary_seen(candidate, state):
        return decision
    return AutomationDecision(
        disposition="no-op",
        category="publication-boundary-already-reported",
        title=f"{candidate.repo}: publication boundary already reported",
        reason="The same missing or unresolved origin boundary is already recorded in maintainer state.",
        recommended_action="Keep the repo out of automatic maintainer edits until the user approves a publication path.",
        evidence=decision.evidence,
    )


def _publication_boundary_seen(
    candidate: TouchedRepoCandidate,
    state: dict[str, Any],
) -> bool:
    boundaries = state.get("publication_boundaries")
    if not isinstance(boundaries, dict):
        return False
    stored = boundaries.get(candidate.repo)
    fingerprint = _publication_boundary_fingerprint(candidate)
    if stored == fingerprint:
        return True
    if isinstance(stored, dict):
        return stored.get("fingerprint") == fingerprint
    return False


def _publication_boundary_fingerprint(candidate: TouchedRepoCandidate) -> str:
    digest = hashlib.sha256()
    digest.update(candidate.repo.encode())
    digest.update(b"\0")
    for entry in sorted(candidate.publication_boundary):
        digest.update(entry.encode())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _repeated_noop_decision(
    candidates: list[TouchedRepoCandidate],
    decisions: list[AutomationDecision],
    state: dict[str, Any],
) -> AutomationDecision | None:
    if not candidates:
        return None
    if not all(decision.category == "score-below-threshold" for decision in decisions):
        return None
    last_noop = state.get("last_score_below_threshold")
    if not isinstance(last_noop, dict):
        return None
    fingerprint = _touched_set_fingerprint(candidates)
    if last_noop.get("fingerprint") != fingerprint:
        return None
    try:
        count = int(last_noop.get("count", 0))
    except (TypeError, ValueError):
        return None
    if count < 1:
        return None
    return AutomationDecision(
        disposition=WAIT,
        category="repeated-noop-stop",
        title="Touched-repo maintainer should stop this repeated no-op loop",
        reason="The same touched repo set already produced no score-qualified candidate in the prior pass.",
        recommended_action="Return a concise no-op/status report instead of stretching for churn.",
        evidence=(f"touched_set={fingerprint}", f"prior_count={count}"),
    )


def _memory_write_decision(
    memory_path: str,
    state_path: str,
    candidates: list[TouchedRepoCandidate],
) -> AutomationDecision:
    fingerprint = _touched_set_fingerprint(candidates)
    return AutomationDecision(
        disposition=WAIT,
        category="automation-memory-required",
        title="Daily touched-repo maintainer memory and state must be updated",
        reason=(
            "Every fleet pass should leave durable evidence for the next persistent-agent run "
            "and update the repeated no-op fingerprint state."
        ),
        recommended_action=(
            "Append timestamp, repos scanned, scope evidence, hard stops, gates, scores, PR/check "
            "status, blockers, and final state; update the state JSON; then read both back."
        ),
        evidence=(f"memory={memory_path}", f"state={state_path}", f"touched_set={fingerprint}"),
    )


def _touched_set_fingerprint(candidates: list[TouchedRepoCandidate]) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(candidates, key=lambda item: item.repo):
        digest.update(candidate.repo.encode())
        digest.update(b"\0")
        for entry in sorted(candidate.changed_paths):
            digest.update(entry.encode())
            digest.update(b"\0")
    return digest.hexdigest()[:16]
