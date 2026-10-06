from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class BranchState:
    name: str
    upstream: str | None = None
    last_commit_iso: str | None = None
    last_subject: str | None = None
    merged_to_default: bool = False
    unique_commit_count: int | None = None
    current: bool = False
    ahead: int | None = None
    behind: int | None = None
    upstream_gone: bool = False


@dataclass
class LinkedWorktreeState:
    path: str
    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    upstream_gone: bool = False
    default_ref: str | None = None
    tree_matches_default: bool | None = None
    unique_commit_count: int | None = None
    dirty_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0


@dataclass
class RepoState:
    path: str
    name: str
    current_branch: str | None = None
    default_branch: str = "main"
    default_ref: str | None = None
    remote_url: str | None = None
    github_repo: str | None = None
    dirty_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0
    upstream: str | None = None
    branches: list[BranchState] = field(default_factory=list)
    linked_worktrees: list[LinkedWorktreeState] = field(default_factory=list)
    fetch_prune_status: str | None = None
    errors: list[str] = field(default_factory=list)
    head_oid: str | None = None
    head_unique_commit_count: int | None = None
    default_oid: str | None = None


@dataclass
class PullRequestState:
    repo: str
    number: int
    title: str
    url: str | None
    head_ref: str
    base_ref: str | None
    is_draft: bool
    merge_state: str | None
    review_decision: str | None
    check_status: str
    changed_files: list[str] = field(default_factory=list)
    additions: int | None = None
    deletions: int | None = None
    updated_at: str | None = None
    risk_reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    head_oid: str | None = None
    author: str | None = None


@dataclass
class Finding:
    severity: str
    category: str
    title: str
    detail: str
    repo_path: str | None = None
    url: str | None = None
    recommended_action: str | None = None


@dataclass(frozen=True)
class AutomationDecision:
    disposition: str
    category: str
    title: str
    reason: str
    recommended_action: str
    evidence: tuple[str, ...] = ()

    @property
    def is_autonomous(self) -> bool:
        return self.disposition == "auto-act"


@dataclass
class ScanReport:
    generated_at: str
    repos: list[RepoState]
    pull_requests: list[PullRequestState]
    findings: list[Finding]
    automation_decisions: list[AutomationDecision] = field(default_factory=list)
    execution_results: list[Any] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    scan_plan: Any | None = None
    reconciliation: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
