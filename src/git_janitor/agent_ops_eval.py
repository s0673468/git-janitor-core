from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .autonomy import (
    AUTO_ACT,
    AutomationPolicy,
    TouchedRepoCandidate,
    decide_branch_cleanup,
    decide_linked_worktree_cleanup,
    decide_pull_request,
    decide_runner_failure,
    decide_touched_repo_candidate,
    decide_workflow_parse_error,
)
from .config import ScannerConfig
from .fleet import plan_touched_repo_maintainer
from .models import (
    AutomationDecision,
    BranchState,
    LinkedWorktreeState,
    PullRequestState,
    RepoState,
)


REQUIRED_AGENT_OPS_DOMAINS = frozenset(
    {
        "touched-repo maintenance",
        "pr babysitting",
        "ci failure triage",
        "stale branches",
        "dirty worktrees",
        "workflow blockers",
    }
)


@dataclass(frozen=True)
class AgentOpsEvalResult:
    domain: str
    source: str
    case_name: str
    expected_dispositions: tuple[str, ...]
    expected_categories: tuple[str, ...]
    actual_dispositions: tuple[str, ...]
    actual_categories: tuple[str, ...]
    default_policy_read_only: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.actual_dispositions == self.expected_dispositions
            and self.actual_categories == self.expected_categories
        )

    @property
    def has_auto_act(self) -> bool:
        return AUTO_ACT in self.actual_dispositions

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "source": self.source,
            "case": self.case_name,
            "expected_dispositions": list(self.expected_dispositions),
            "expected_categories": list(self.expected_categories),
            "actual_dispositions": list(self.actual_dispositions),
            "actual_categories": list(self.actual_categories),
            "default_policy_read_only": self.default_policy_read_only,
            "passed": self.passed,
        }


def run_agent_ops_eval(manifest_path: str | Path) -> list[AgentOpsEvalResult]:
    """Run the agent-ops manifest against fixture-backed pure decision functions."""
    manifest = _load_json(Path(manifest_path))
    fixture_dir = Path(manifest_path).parent
    source_cases: dict[str, dict[str, dict[str, Any]]] = {}
    results: list[AgentOpsEvalResult] = []

    for entry in manifest["cases"]:
        source = entry["source"]
        if source not in source_cases:
            source_cases[source] = {
                case["name"]: case for case in _load_json(fixture_dir / source)["cases"]
            }

        case_name = entry["case"]
        try:
            case = source_cases[source][case_name]
        except KeyError as error:
            raise ValueError(f"Missing fixture case {case_name!r} in {source}") from error

        decisions = _evaluate_case(source, case, fixture_dir)
        expected_dispositions, expected_categories = _expected(entry, case)
        results.append(
            AgentOpsEvalResult(
                domain=entry["domain"],
                source=source,
                case_name=case_name,
                expected_dispositions=expected_dispositions,
                expected_categories=expected_categories,
                actual_dispositions=tuple(decision.disposition for decision in decisions),
                actual_categories=tuple(decision.category for decision in decisions),
                default_policy_read_only=bool(entry.get("default_policy_read_only", False)),
            )
        )

    return results


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected object at {path}")
    return loaded


def _evaluate_case(
    source: str,
    case: dict[str, Any],
    fixture_dir: Path,
) -> tuple[AutomationDecision, ...]:
    if source == "pull_requests.json":
        config = ScannerConfig(
            high_risk_patterns=[
                pattern.lower() for pattern in case.get("high_risk_patterns", [])
            ]
        )
        return (
            decide_pull_request(
                _pull_request(case["pr"]),
                config,
                _policy(case.get("policy", {})),
            ),
        )

    if source == "stale_branches.json":
        return (
            decide_branch_cleanup(
                _repo(case["repo"]),
                _branch(case["branch"]),
                _policy(case.get("policy", {})),
            ),
        )

    if source == "linked_worktrees.json":
        return (
            decide_linked_worktree_cleanup(
                _repo(case["repo"]),
                _linked_worktree(case["worktree"]),
            ),
        )

    if source == "touched_repo_maintainer.json":
        return (
            decide_touched_repo_candidate(
                _touched_repo_candidate(case["candidate"]),
                _policy(case.get("policy", {})),
            ),
        )

    if source == "touched_repo_maintainer_plan.json":
        state_path = _fixture_state_path(
            fixture_dir,
            case.get("state_path", ".agent-ops-eval-state-does-not-exist.json"),
        )
        return tuple(
            plan_touched_repo_maintainer(
                [_touched_repo_candidate(candidate) for candidate in case["candidates"]],
                _policy(case.get("policy", {})),
                max_auto_prs=case["max_auto_prs"],
                max_changed_repos=case["max_changed_repos"],
                memory_path=case["memory_path"],
                state_path=str(state_path),
            )
        )

    if source == "runner_failures.json":
        decision = decide_runner_failure(
            case["log"],
            _policy(case.get("policy", {})),
        )
        if decision is None:
            raise ValueError(f"Fixture case {case['name']!r} produced no runner decision")
        return (decision,)

    if source == "workflow_errors.json":
        return (
            decide_workflow_parse_error(
                case["message"],
                case["workflow_path"],
                _policy(case.get("policy", {})),
            ),
        )

    raise ValueError(f"Unsupported agent-ops fixture source: {source}")


def _fixture_state_path(fixture_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        raise ValueError(f"Fixture state_path must be relative to {fixture_dir}: {raw_path}")
    resolved_fixture_dir = fixture_dir.resolve()
    resolved_path = (resolved_fixture_dir / path).resolve()
    if resolved_path != resolved_fixture_dir and resolved_fixture_dir not in resolved_path.parents:
        raise ValueError(f"Fixture state_path escapes {fixture_dir}: {raw_path}")
    return resolved_path


def _expected(
    manifest_entry: dict[str, Any],
    source_case: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    expected = manifest_entry.get("expected", source_case.get("expected", {}))
    if "dispositions" in expected:
        dispositions = tuple(expected["dispositions"])
    else:
        dispositions = (expected["disposition"],)
    if "categories" in expected:
        categories = tuple(expected["categories"])
    else:
        categories = (expected["category"],)
    return dispositions, categories


def _policy(raw: dict[str, Any]) -> AutomationPolicy:
    normalized = dict(raw)
    if "apply_categories" in normalized:
        normalized["apply_categories"] = frozenset(normalized["apply_categories"])
    return AutomationPolicy(**normalized)


def _pull_request(raw: dict[str, Any]) -> PullRequestState:
    return PullRequestState(
        repo=raw["repo"],
        number=raw["number"],
        title=raw["title"],
        url=raw.get("url"),
        head_ref=raw["head_ref"],
        base_ref=raw.get("base_ref"),
        is_draft=raw["is_draft"],
        merge_state=raw.get("merge_state"),
        review_decision=raw.get("review_decision"),
        check_status=raw["check_status"],
        changed_files=list(raw.get("changed_files", [])),
    )


def _repo(raw: dict[str, Any]) -> RepoState:
    return RepoState(
        path=raw["path"],
        name=raw["name"],
        default_branch=raw.get("default_branch", "main"),
        default_ref=raw.get("default_ref"),
        remote_url=raw.get("remote_url"),
        github_repo=raw.get("github_repo"),
        dirty_files=list(raw.get("dirty_files", [])),
        untracked_files=list(raw.get("untracked_files", [])),
        ahead=raw.get("ahead", 0),
        behind=raw.get("behind", 0),
        upstream=raw.get("upstream"),
        fetch_prune_status=raw.get("fetch_prune_status"),
        errors=list(raw.get("errors", [])),
    )


def _branch(raw: dict[str, Any]) -> BranchState:
    return BranchState(
        name=raw["name"],
        upstream=raw.get("upstream"),
        merged_to_default=raw.get("merged_to_default", False),
        unique_commit_count=raw.get("unique_commit_count"),
        current=raw.get("current", False),
    )


def _linked_worktree(raw: dict[str, Any]) -> LinkedWorktreeState:
    return LinkedWorktreeState(
        path=raw["path"],
        branch=raw.get("branch"),
        head=raw.get("head"),
        upstream=raw.get("upstream"),
        upstream_gone=raw.get("upstream_gone", False),
        default_ref=raw.get("default_ref"),
        tree_matches_default=raw.get("tree_matches_default"),
        unique_commit_count=raw.get("unique_commit_count"),
        dirty_files=list(raw.get("dirty_files", [])),
        untracked_files=list(raw.get("untracked_files", [])),
        errors=list(raw.get("errors", [])),
    )


def _touched_repo_candidate(raw: dict[str, Any]) -> TouchedRepoCandidate:
    return TouchedRepoCandidate(
        repo=raw["repo"],
        candidate=raw["candidate"],
        safety_score=raw.get("safety_score"),
        recent_files=tuple(raw.get("recent_files", [])),
        repo_errors=tuple(raw.get("repo_errors", [])),
        dirty_files=tuple(raw.get("dirty_files", [])),
        fast_forward=tuple(raw.get("fast_forward", [])),
        remote_state=tuple(raw.get("remote_state", [])),
        publication_boundary=tuple(raw.get("publication_boundary", [])),
        pr_lookup_errors=tuple(raw.get("pr_lookup_errors", [])),
        open_codex_prs=tuple(raw.get("open_codex_prs", [])),
        stale_ci=tuple(raw.get("stale_ci", [])),
        approval_only_findings=tuple(raw.get("approval_only_findings", [])),
        permission_sensitive=raw.get("permission_sensitive", False),
        changed_paths=tuple(raw.get("changed_paths", [])),
        preserved_paths=tuple(raw.get("preserved_paths", [])),
        score_components=tuple(raw.get("score_components", [])),
    )
