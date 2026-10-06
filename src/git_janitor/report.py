from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
import json

from .models import Finding, ScanReport
from .delivery_queue import build_delivery_queue


def render_markdown(report: ScanReport) -> str:
    lines: list[str] = [
        "# Git Janitor Report",
        "",
        f"Generated: {report.generated_at}",
        "",
        f"Repos scanned: {len(report.repos)}",
        f"Open PRs inspected: {len(report.pull_requests)}",
        f"Findings: {len(report.findings)}",
        "",
    ]

    if report.scan_plan is not None:
        lines.extend(_render_scan_plan(report))
    if report.reconciliation is not None:
        lines.extend(_render_reconciliation(report))

    if report.errors:
        lines.extend(["## Scanner Errors", ""])
        for error in report.errors:
            lines.append(f"- {error}")
        lines.append("")

    queue = build_delivery_queue(report)
    if queue:
        lines.extend(["## Ranked Delivery and Hygiene Queue", "", "This queue is inspection-only. Existing explicit task authority must be verified before action; unknown ownership and publication are preserved.", ""])
        for item in queue:
            lines.extend([
                f"{item.rank}. {item.title}",
                f"   - Lane/status: `{item.lane}` / `{item.status}`; ID `{item.item_id}`.",
                f"   - Next: {item.next_action}",
                f"   - Authority: {item.authorization}",
                f"   - Preserve: {item.preservation}",
                f"   - Unknown: {'; '.join(item.unknowns)}.",
                f"   - Evidence ({item.observed_at}): {'; '.join(item.evidence)}",
            ])
        lines.append("")

    if not report.findings:
        lines.extend(["## Status", "", _empty_status(report), ""])
        if report.automation_decisions:
            lines.extend(_render_automation_decisions(report))
        if report.execution_results:
            lines.extend(_render_execution_results(report))
        lines.extend(_render_guardrails(report))
        return "\n".join(lines).rstrip() + "\n"

    counts = Counter(finding.severity for finding in report.findings)
    lines.extend(
        [
            "## Summary",
            "",
            f"- High: {counts.get('high', 0)}",
            f"- Medium: {counts.get('medium', 0)}",
            f"- Low: {counts.get('low', 0)}",
            "",
        ]
    )

    for severity in ["high", "medium", "low"]:
        group = [finding for finding in report.findings if finding.severity == severity]
        if not group:
            continue
        lines.extend([f"## {severity.title()} Priority", ""])
        for finding in group:
            lines.extend(_render_finding(finding))
        lines.append("")

    if report.automation_decisions:
        lines.extend(_render_automation_decisions(report))
    if report.execution_results:
        lines.extend(_render_execution_results(report))

    lines.extend(_render_guardrails(report))
    return "\n".join(lines).rstrip() + "\n"


def render_json(report: ScanReport) -> str:
    payload = report.to_dict()
    payload["delivery_queue"] = [asdict(item) for item in build_delivery_queue(report)]
    return json.dumps(payload, indent=2, sort_keys=True)


def report_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _render_finding(finding: Finding) -> list[str]:
    target = f" ({finding.repo_path})" if finding.repo_path else ""
    if finding.url:
        title = f"- [{finding.title}]({finding.url}){target}"
    else:
        title = f"- {finding.title}{target}"
    lines = [title, f"  - Category: `{finding.category}`", f"  - Detail: {finding.detail}"]
    if finding.recommended_action:
        lines.append(f"  - Recommended: {finding.recommended_action}")
    return lines


def _render_scan_plan(report: ScanReport) -> list[str]:
    plan = report.scan_plan
    lines = [
        "## Saved Scan Plan",
        "",
        f"- Name: `{plan.name}`",
    ]
    if plan.description:
        lines.append(f"- Description: {plan.description}")
    lines.extend(
        [
            f"- Projects: {', '.join(f'`{value}`' for value in plan.project_ids)}",
            "- Finding categories: "
            + ", ".join(f"`{value}`" for value in plan.finding_categories),
            "- Authority: report-only; this plan cannot authorize scanner execution.",
            "",
        ]
    )
    return lines


def _render_reconciliation(report: ScanReport) -> list[str]:
    result = report.reconciliation
    registry = result.registry
    github = result.github
    local = result.local
    github_repositories = sum(len(owner.repositories) for owner in github.owners)
    worktrees = sum(len(repo.worktree_paths) for repo in local.repositories)
    coverage = "complete" if result.complete else "incomplete"
    registry_coverage = "complete" if registry.complete else "incomplete"
    github_coverage = "complete" if github.complete else "incomplete"
    local_coverage = "complete" if local.complete else "incomplete"
    authenticated = (
        "yes" if github.authenticated is True else "no" if github.authenticated is False else "unknown"
    )
    digest = registry.sha256 or "unavailable"
    owners = ", ".join(
        f"{owner.owner} ({'complete' if owner.complete else 'incomplete'})"
        for owner in github.owners
    ) or "none"
    return [
        "## Inventory Reconciliation",
        "",
        f"- Evidence coverage: **{coverage}**",
        (
            f"- Registry: {registry_coverage}; {len(registry.projects)} projects; "
            f"schema `{registry.schema_version}`; SHA-256 `{digest}`"
        ),
        f"- Registry path: `{registry.path}`",
        (
            f"- GitHub: {github_coverage}; authenticated={authenticated}; "
            f"{github_repositories} repositories; observed {github.observed_at}"
        ),
        f"- GitHub owners: {owners}",
        (
            f"- Local: {local_coverage}; {len(local.repositories)} repositories; "
            f"{worktrees} linked worktrees; observed {local.observed_at}"
        ),
        "- Authority: evidence only; reconciliation findings are never executable decisions.",
        "",
    ]


def _empty_status(report: ScanReport) -> str:
    if report.reconciliation is not None:
        if not report.reconciliation.complete:
            return (
                "Inventory evidence is incomplete. No clean reconciliation conclusion "
                "is available."
            )
        return "Inventory reconciliation completed with no mismatches."
    if report.errors:
        return "Scanner evidence is incomplete. No clean audit conclusion is available."
    if report.scan_plan is not None:
        return f"No findings matched saved scan plan `{report.scan_plan.name}`."
    return "No forgotten local work or PR follow-ups found."


def _render_automation_decisions(report: ScanReport) -> list[str]:
    lines = ["## Automation Decisions", ""]
    for decision in report.automation_decisions:
        lines.extend(
            [
                f"- {decision.title}",
                f"  - Disposition: `{decision.disposition}`",
                f"  - Category: `{decision.category}`",
                f"  - Reason: {decision.reason}",
                f"  - Recommended: {decision.recommended_action}",
            ]
        )
        if decision.evidence:
            lines.append(f"  - Evidence: {'; '.join(decision.evidence)}")
    lines.append("")
    return lines


def _render_execution_results(report: ScanReport) -> list[str]:
    lines = ["## Execution Results", ""]
    for result in report.execution_results:
        command = _field(result, "command") or []
        command_text = " ".join(command) if command else "(none)"
        lines.extend(
            [
                f"- {_field(result, 'decision').title}",
                f"  - Status: `{_field(result, 'status')}`",
                f"  - Command: `{command_text}`",
                f"  - Detail: {_field(result, 'detail')}",
                f"  - Rollback: {_field(result, 'rollback_hint')}",
            ]
        )
    lines.append("")
    return lines


def _render_guardrails(report: ScanReport) -> list[str]:
    lines = ["## Automation Guardrails", ""]
    if report.execution_results:
        lines.append(
            "- Execution results above show every skipped, drifted, dry-run, applied, or failed action."
        )
        lines.append("- Mutating commands run only for `applied` or `failed` execution entries.")
    else:
        lines.append("- No merges, pushes, branch deletions, resets, or rebases were performed.")
    lines.extend(
        [
            "- `git fetch --prune origin` may have run where enabled in config.",
            "- Keep PRs with unresolved risk cues outside unattended mutation; the author inspects behavior and applies the shared tier within existing authority.",
            "",
        ]
    )
    return lines


def _field(result, name: str):
    return getattr(result, name)
