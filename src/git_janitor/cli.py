from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import sys

from .autonomy import (
    AutomationPolicy,
    decide_branch_cleanup,
    decide_linked_worktree_cleanup,
    decide_pull_request,
)
from .classify import classify_report
from .config import load_config
from .execute import execute_decisions
from .fleet import (
    TouchedRepoMaintainerConfig,
    build_touched_repo_candidates,
    plan_touched_repo_maintainer,
)
from .git import discover_repos, run_command, scan_repo
from .github import gh_available, list_open_pull_requests, list_pull_requests
from .ledger import AuditLedger
from .models import PullRequestState, ScanReport
from .reconciliation import (
    collect_github_inventory,
    load_registry,
    reconcile_inventory,
    reconciliation_findings,
)
from .report import render_json, render_markdown, report_timestamp
from .scan_plans import (
    EVIDENCE_GAP_FINDING_CATEGORIES,
    ScanPlanError,
    filter_findings,
    require_scan_plan,
    select_project_paths,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan local Git and GitHub PR hygiene.")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    parser.add_argument("--out", help="Optional Markdown report output path")
    parser.add_argument("--json-out", help="Optional JSON report output path")
    parser.add_argument(
        "--plan",
        help="Run one saved, bounded project and finding audit from config.",
    )
    parser.add_argument(
        "--list-plans",
        action="store_true",
        help="List configured saved scan plans and exit.",
    )
    parser.add_argument(
        "--reconcile-inventory",
        action="store_true",
        help=(
            "Reconcile the canonical project registry with live authenticated GitHub "
            "and local checkout/worktree inventory."
        ),
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Disable git fetch --prune for this run, overriding config.",
    )
    parser.add_argument(
        "--touched-maintainer-plan",
        action="store_true",
        help="Emit read-only Repo Fleet Maintainer v2 planning decisions.",
    )
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--dry-run",
        action="store_true",
        help="Re-check and audit allowed auto-act decisions without mutating anything.",
    )
    execution.add_argument(
        "--apply",
        action="store_true",
        help="Apply allowed auto-act decisions after fresh precondition checks.",
    )
    parser.add_argument(
        "--apply-categories",
        action="append",
        default=[],
        help=(
            "Comma-separated execution categories to allow for this run. This only narrows "
            "the config allowlist."
        ),
    )
    parser.add_argument(
        "--touched-window-hours",
        type=int,
        default=24,
        help="Hours of repo activity that put a repo in touched-maintainer scope.",
    )
    parser.add_argument(
        "--active-window-minutes",
        type=int,
        default=60,
        help="Recent file activity window that blocks touched-maintainer edits.",
    )
    parser.add_argument(
        "--max-auto-prs",
        type=int,
        default=2,
        help="Maximum score-qualified PRs for one touched-maintainer pass.",
    )
    parser.add_argument(
        "--max-changed-repos",
        type=int,
        default=2,
        help="Maximum repos changed by one touched-maintainer pass.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    runner=None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.list_plans:
        if args.plan:
            parser.error("--list-plans cannot be combined with --plan")
        _print_scan_plans(config.scan_plans)
        return 0

    if args.reconcile_inventory:
        if args.apply or args.dry_run:
            parser.error("inventory reconciliation is evidence-only and cannot execute actions")
        if args.touched_maintainer_plan:
            parser.error("inventory reconciliation cannot emit touched-maintainer action plans")
        if args.apply_categories:
            parser.error("inventory reconciliation cannot accept execution category filters")
        if args.no_fetch or not config.fetch_prune:
            parser.error("inventory reconciliation requires live fetch freshness checks")

    plan = None
    if args.plan:
        if args.apply or args.dry_run:
            parser.error("saved scan plans are report-only and cannot run with execution modes")
        if args.reconcile_inventory:
            parser.error("saved scan plans cannot narrow a full inventory reconciliation")
        if args.touched_maintainer_plan:
            parser.error("saved scan plans cannot emit touched-maintainer action plans")
        if args.apply_categories:
            parser.error("saved scan plans cannot accept execution category filters")
        if args.no_fetch or not config.fetch_prune:
            parser.error("saved scan plans require live fetch freshness checks")
        try:
            plan = require_scan_plan(config.scan_plans, args.plan)
        except ScanPlanError as exc:
            parser.error(str(exc))

    if args.no_fetch:
        config = _without_fetch(config)
    policy = AutomationPolicy.from_config(config)

    repos = []
    prs: list[PullRequestState] = []
    open_prs: list[PullRequestState] = []
    open_pr_errors_by_repo: dict[str, tuple[str, ...]] = {}
    approval_only_findings_by_repo: dict[str, tuple[str, ...]] = {}
    errors: list[str] = []
    registry = None
    reconciliation = None
    github_inventory = None

    if plan or args.reconcile_inventory:
        registry = load_registry(config.project_registry_path, clock=clock)
        if plan and not registry.complete:
            parser.error("; ".join(registry.errors) or "project registry is incomplete")
        if args.reconcile_inventory and registry.complete:
            config = _with_reconciliation_repos(config, registry)

    discovery_errors: list[str] = []
    repo_paths = discover_repos(
        config,
        errors=discovery_errors,
    )
    if plan:
        try:
            repo_paths = select_project_paths(
                plan,
                canonical_project_paths=_canonical_project_paths(registry),
                discovered_repo_paths=repo_paths,
            )
        except ScanPlanError as exc:
            parser.error(str(exc))
    errors.extend(f"local discovery: {error}" for error in discovery_errors)
    for path in repo_paths:
        repo = scan_repo(path, config)
        repos.append(repo)

    if args.reconcile_inventory:
        github_inventory = collect_github_inventory(
            registry,
            runner=runner if runner is not None else run_command,
            clock=clock,
            timeout=config.command_timeout_seconds,
        )
        github_is_available = github_inventory.authenticated is True
    else:
        github_is_available = gh_available(timeout=10)

    if github_is_available:
        seen_repos: set[str] = set()
        for repo in repos:
            if not repo.github_repo or repo.github_repo in seen_repos:
                continue
            seen_repos.add(repo.github_repo)
            repo_prs, repo_errors = list_pull_requests(
                repo.github_repo,
                config,
                cwd=Path(repo.path),
            )
            prs.extend(repo_prs)
            errors.extend(repo_errors)
            if args.touched_maintainer_plan:
                repo_open_prs, repo_open_errors = list_open_pull_requests(
                    repo.github_repo,
                    config,
                    cwd=Path(repo.path),
                )
                open_prs.extend(repo_open_prs)
                errors.extend(repo_open_errors)
                if repo_open_errors:
                    open_pr_errors_by_repo[repo.name] = tuple(repo_open_errors)
    else:
        if args.reconcile_inventory and github_inventory.authenticated is None:
            errors.append(
                "project registry owner scope is incomplete; skipped GitHub PR inspection."
            )
        else:
            errors.append(
                "gh is unavailable or not authenticated; skipped GitHub PR inspection."
            )
        if args.touched_maintainer_plan:
            for repo in repos:
                if repo.github_repo:
                    open_pr_errors_by_repo[repo.name] = (
                        "gh is unavailable or not authenticated; skipped unfiltered PR hard-stop inspection.",
                    )

    findings = classify_report(repos, prs, config)
    if args.reconcile_inventory:
        reconciliation = reconcile_inventory(
            registry,
            github_inventory,
            repos,
            local_complete=not discovery_errors,
            local_errors=tuple(discovery_errors),
            clock=clock,
        )
        findings.extend(reconciliation_findings(reconciliation))
        errors.extend(reconciliation.errors)
    if plan:
        findings = filter_findings(plan, findings)
    automation_decisions = [decide_pull_request(pr, config, policy) for pr in prs]
    for repo in repos:
        for branch in repo.branches:
            has_local_only_commits = branch.upstream is None and bool(branch.unique_commit_count)
            is_cleanup_candidate = branch.merged_to_default or has_local_only_commits
            if branch.current or branch.name == repo.default_branch or not is_cleanup_candidate:
                continue
            decision = decide_branch_cleanup(repo, branch, policy)
            if decision.disposition != "no-op":
                automation_decisions.append(decision)
                if decision.disposition == "needs-approval":
                    approval_only_findings_by_repo.setdefault(repo.name, ())
                    approval_only_findings_by_repo[repo.name] = (
                        *approval_only_findings_by_repo[repo.name],
                        f"{decision.category}:{decision.title}",
                    )
        for worktree in repo.linked_worktrees:
            if not worktree.upstream_gone:
                continue
            decision = decide_linked_worktree_cleanup(repo, worktree)
            if decision.disposition != "no-op":
                automation_decisions.append(decision)

    if plan or args.reconcile_inventory:
        automation_decisions = []

    if args.touched_maintainer_plan:
        maintainer_config = TouchedRepoMaintainerConfig(
            touched_window_hours=args.touched_window_hours,
            active_window_minutes=args.active_window_minutes,
            max_auto_prs=args.max_auto_prs,
            max_changed_repos=args.max_changed_repos,
            exclude_dirs=frozenset(config.exclude_dirs),
        )
        candidates = build_touched_repo_candidates(
            repos,
            open_prs,
            maintainer_config,
            pr_lookup_errors_by_repo=open_pr_errors_by_repo,
            approval_only_findings_by_repo=approval_only_findings_by_repo,
        )
        automation_decisions.extend(
            plan_touched_repo_maintainer(
                candidates,
                policy,
                max_auto_prs=maintainer_config.max_auto_prs,
                max_changed_repos=maintainer_config.max_changed_repos,
                memory_path=maintainer_config.memory_path,
                state_path=maintainer_config.state_path,
            )
        )

    execution_results = []
    if args.apply or args.dry_run:
        execution_results = execute_decisions(
            automation_decisions,
            policy=policy,
            ledger=AuditLedger(_ledger_path(policy), clock=clock),
            mode="apply" if args.apply else "dry-run",
            runner=runner,
            repo_paths=_repo_paths_by_unique_name(repos),
            config=config,
            apply_categories=_apply_category_filter(args.apply_categories),
        )

    report = ScanReport(
        generated_at=report_timestamp(),
        repos=repos,
        pull_requests=prs,
        findings=findings,
        scan_plan=plan,
        reconciliation=reconciliation,
        automation_decisions=automation_decisions,
        execution_results=execution_results,
        errors=errors,
    )

    markdown = render_markdown(report)
    sys.stdout.write(markdown)

    if args.out:
        _write_text(Path(args.out), markdown)
    if args.json_out:
        _write_text(Path(args.json_out), render_json(report) + "\n")
    if reconciliation is not None and not reconciliation.complete:
        return 3
    if (
        errors
        or any(
            finding.category in EVIDENCE_GAP_FINDING_CATEGORIES
            for finding in findings
        )
    ):
        return 3
    return 0


def _write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _without_fetch(config):
    from dataclasses import replace

    return replace(config, fetch_prune=False)


def _with_reconciliation_repos(config, registry):
    from dataclasses import replace

    code_root = Path(registry.code_root).expanduser()
    canonical = [
        code_root / project.local_path
        for project in registry.projects
        if project.kind == "repository"
        and project.status != "archived"
        and project.local_path is not None
        and ((code_root / project.local_path) / ".git").exists()
    ]
    repos = {
        str(path.expanduser().resolve()): path.expanduser().resolve()
        for path in (*config.repos, *canonical)
    }
    return replace(config, repos=[repos[key] for key in sorted(repos)])


def _apply_category_filter(raw_values: list[str]) -> frozenset[str] | None:
    categories = {
        category.strip()
        for raw in raw_values
        for category in raw.split(",")
        if category.strip()
    }
    return frozenset(categories) if categories else None


def _ledger_path(policy: AutomationPolicy) -> Path:
    if policy.ledger_path:
        return Path(policy.ledger_path).expanduser()
    return Path("reports") / "execution-audit.jsonl"


def _repo_paths_by_unique_name(repos) -> dict[str, Path]:
    name_counts = Counter(repo.name for repo in repos)
    return {
        repo.name: Path(repo.path)
        for repo in repos
        if name_counts[repo.name] == 1
    }


def _canonical_project_paths(registry) -> dict[str, Path | None]:
    if registry is None or not registry.code_root:
        return {}
    root = Path(registry.code_root).expanduser()
    return {
        project.project_id: root / project.local_path if project.local_path else None
        for project in registry.projects
    }


def _print_scan_plans(plans) -> None:
    if not plans:
        sys.stdout.write("No saved scan plans configured.\n")
        return
    for name in sorted(plans):
        plan = plans[name]
        description = f" — {plan.description}" if plan.description else ""
        sys.stdout.write(f"{name}{description}\n")
        sys.stdout.write(f"  projects: {', '.join(plan.project_ids)}\n")
        sys.stdout.write(f"  findings: {', '.join(plan.finding_categories)}\n")
