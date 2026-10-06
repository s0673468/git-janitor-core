"""Render a saved scan snapshot without fetching, authentication or execution."""
from __future__ import annotations

import argparse
from dataclasses import fields
import json
from pathlib import Path

from .execute import ExecutionResult
from .models import AutomationDecision, BranchState, Finding, LinkedWorktreeState, PullRequestState, RepoState, ScanReport
from .reconciliation import (
    GithubInventory, GithubOwnerCoverage, GithubRepository, LocalInventory,
    LocalRepository, ReconciliationIssue, ReconciliationResult, ReconciliationRow,
    RegistryProject, RegistrySource,
)
from .report import render_json, render_markdown
from .scan_plans import SavedScanPlan


def _restore(cls, raw, **overrides):
    if not isinstance(raw, dict):
        raise ValueError(f"{cls.__name__} snapshot must be an object")
    names = {item.name for item in fields(cls)}
    extra = set(raw) - names
    if extra:
        raise ValueError(f"Unsupported {cls.__name__} snapshot fields: {', '.join(sorted(extra))}")
    return cls(**{**raw, **overrides})


def restore_report(raw: dict) -> ScanReport:
    """Rehydrate the existing dataclass serialization; never refresh observations."""
    if not isinstance(raw, dict):
        raise ValueError("report snapshot must be an object")
    payload = dict(raw)
    payload.pop("delivery_queue", None)  # Derived again from the saved evidence.
    repos = [
        _restore(RepoState, repo,
                 branches=[_restore(BranchState, branch) for branch in repo.get("branches", [])],
                 linked_worktrees=[_restore(LinkedWorktreeState, worktree) for worktree in repo.get("linked_worktrees", [])])
        for repo in payload.get("repos", [])
    ]
    plan = payload.get("scan_plan")
    reconciliation = payload.get("reconciliation")
    if reconciliation is not None:
        registry = reconciliation["registry"]
        github = reconciliation["github"]
        local = reconciliation["local"]
        reconciliation = _restore(
            ReconciliationResult, reconciliation,
            registry=_restore(RegistrySource, registry, projects=tuple(_restore(RegistryProject, item) for item in registry["projects"])),
            github=_restore(GithubInventory, github, owners=tuple(
                _restore(GithubOwnerCoverage, owner, repositories=tuple(_restore(GithubRepository, item) for item in owner["repositories"]))
                for owner in github["owners"])),
            local=_restore(LocalInventory, local, repositories=tuple(_restore(LocalRepository, item) for item in local["repositories"])),
            rows=tuple(_restore(
                ReconciliationRow, row,
                project=_restore(RegistryProject, row["project"]) if row.get("project") else None,
                github_repository=_restore(GithubRepository, row["github_repository"]) if row.get("github_repository") else None,
                issues=tuple(_restore(ReconciliationIssue, item) for item in row["issues"]),
            ) for row in reconciliation["rows"]),
        )
    return _restore(
        ScanReport, payload, repos=repos,
        pull_requests=[_restore(PullRequestState, item) for item in payload.get("pull_requests", [])],
        findings=[_restore(Finding, item) for item in payload.get("findings", [])],
        automation_decisions=[_restore(AutomationDecision, item) for item in payload.get("automation_decisions", [])],
        execution_results=[_restore(ExecutionResult, item, decision=_restore(AutomationDecision, item["decision"])) for item in payload.get("execution_results", [])],
        scan_plan=_restore(SavedScanPlan, plan) if plan is not None else None,
        reconciliation=reconciliation,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    outputs = [args.out, *([args.json_out] if args.json_out else [])]
    if args.snapshot.resolve() in {path.resolve() for path in outputs} or len({path.resolve() for path in outputs}) != len(outputs):
        parser.error("Snapshot and output paths must be distinct; preserve the original snapshot")
    try:
        report = restore_report(json.loads(args.snapshot.read_text(encoding="utf-8")))
        markdown = render_markdown(report)
        json_report = render_json(report)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    args.out.write_text(markdown, encoding="utf-8")
    if args.json_out:
        args.json_out.write_text(json_report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
