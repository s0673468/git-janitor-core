from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
import re

from .git import _same_path
from .models import Finding


MAX_SCAN_PLANS = 100
MAX_PROJECTS_PER_PLAN = 100
MAX_FINDING_CATEGORIES_PER_PLAN = 100
MAX_PLAN_NAME_LENGTH = 100
MAX_PROJECT_ID_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 500

FINDING_CATEGORIES = frozenset(
    {
        "branch-without-upstream",
        "dirty-worktree",
        "detached-worktree",
        "diverged-upstream",
        "fetch-prune-failed",
        "green-draft-pr",
        "green-high-risk-pr",
        "green-mergeable-pr",
        "merged-local-branch",
        "pr-blocked-or-needs-review",
        "pr-conflicted",
        "pr-failing",
        "pr-inspection-warning",
        "pr-pending",
        "pr-stale-ci",
        "scanner-error",
        "stale-linked-worktree",
        "unpushed-commits",
    }
)

EVIDENCE_GAP_FINDING_CATEGORIES = frozenset(
    {
        "fetch-prune-failed",
        "pr-inspection-warning",
        "pr-stale-ci",
        "scanner-error",
    }
)

_ALLOWED_PLAN_KEYS = frozenset({"description", "finding_categories", "project_ids"})
_PLAN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


class ScanPlanError(ValueError):
    """A saved scan plan is malformed or cannot be resolved safely."""


@dataclass(frozen=True)
class SavedScanPlan:
    name: str
    project_ids: tuple[str, ...]
    finding_categories: tuple[str, ...]
    description: str | None = None


def parse_scan_plans(raw: object) -> dict[str, SavedScanPlan]:
    if not isinstance(raw, Mapping):
        raise ScanPlanError("scan_plans must be a TOML table")
    if len(raw) > MAX_SCAN_PLANS:
        raise ScanPlanError(f"scan_plans may define at most {MAX_SCAN_PLANS} plans")

    plans: dict[str, SavedScanPlan] = {}
    for name, value in raw.items():
        plan_name = _parse_plan_name(name)
        if not isinstance(value, Mapping):
            raise ScanPlanError(f"saved scan plan {plan_name!r} must be a TOML table")

        unknown_keys = sorted(set(value) - _ALLOWED_PLAN_KEYS)
        if unknown_keys:
            rendered = ", ".join(str(key) for key in unknown_keys)
            raise ScanPlanError(f"saved scan plan {plan_name!r} has unknown key(s): {rendered}")

        project_ids = _parse_project_ids(plan_name, value.get("project_ids"))
        finding_categories = _parse_finding_categories(
            plan_name,
            value.get("finding_categories"),
        )
        description = _parse_description(plan_name, value.get("description"))
        plans[plan_name] = SavedScanPlan(
            name=plan_name,
            project_ids=project_ids,
            finding_categories=finding_categories,
            description=description,
        )
    return plans


def require_scan_plan(
    plans: Mapping[str, SavedScanPlan],
    name: str,
) -> SavedScanPlan:
    try:
        return plans[name]
    except KeyError as exc:
        raise ScanPlanError(f"unknown saved scan plan: {name!r}") from exc


def select_project_paths(
    plan: SavedScanPlan,
    *,
    canonical_project_paths: Mapping[str, str | Path | None],
    discovered_repo_paths: Iterable[str | Path],
) -> list[Path]:
    """Resolve a plan to an exact subset of paths already discovered for the scan."""

    discovered_counts: dict[Path, int] = {}
    for raw_path in discovered_repo_paths:
        path = _canonical_path(raw_path, label="discovered repository")
        discovered_counts[path] = discovered_counts.get(path, 0) + 1

    selected: list[Path] = []
    selected_by_path: dict[Path, str] = {}
    for project_id in plan.project_ids:
        if project_id not in canonical_project_paths:
            raise ScanPlanError(
                f"saved scan plan {plan.name!r} references missing project {project_id!r}"
            )
        raw_path = canonical_project_paths[project_id]
        if raw_path is None:
            raise ScanPlanError(
                f"saved scan plan {plan.name!r} project {project_id!r} has no canonical local path"
            )
        path = _canonical_path(raw_path, label=f"project {project_id!r}")
        matching_paths = [candidate for candidate in discovered_counts if _same_path(path, candidate)]
        matches = sum(discovered_counts[candidate] for candidate in matching_paths)
        if matches == 0:
            raise ScanPlanError(
                f"saved scan plan {plan.name!r} project {project_id!r} is not in the "
                "discovered scan scope"
            )
        if matches != 1:
            raise ScanPlanError(
                f"saved scan plan {plan.name!r} project {project_id!r} has an ambiguous "
                "discovered path"
            )
        discovered_path = matching_paths[0]
        if prior_project := selected_by_path.get(discovered_path):
            raise ScanPlanError(
                f"saved scan plan {plan.name!r} has ambiguous canonical path shared by "
                f"{prior_project!r} and {project_id!r}"
            )
        selected_by_path[discovered_path] = project_id
        selected.append(discovered_path)
    return selected


def filter_findings(plan: SavedScanPlan, findings: Iterable[Finding]) -> list[Finding]:
    """Narrow findings without hiding categories that prove inspection gaps."""

    included = set(plan.finding_categories) | EVIDENCE_GAP_FINDING_CATEGORIES
    return [finding for finding in findings if finding.category in included]


def _parse_plan_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise ScanPlanError("saved scan plan names must be strings")
    if not raw or len(raw) > MAX_PLAN_NAME_LENGTH or not _PLAN_NAME_RE.fullmatch(raw):
        raise ScanPlanError(f"invalid saved scan plan name: {raw!r}")
    return raw


def _parse_project_ids(plan_name: str, raw: object) -> tuple[str, ...]:
    values = _parse_required_string_list(
        plan_name,
        "project_ids",
        raw,
        maximum=MAX_PROJECTS_PER_PLAN,
    )
    for project_id in values:
        if len(project_id) > MAX_PROJECT_ID_LENGTH or not _PROJECT_ID_RE.fullmatch(project_id):
            raise ScanPlanError(
                f"saved scan plan {plan_name!r} has malformed project id {project_id!r}"
            )
    return values


def _parse_finding_categories(plan_name: str, raw: object) -> tuple[str, ...]:
    values = _parse_required_string_list(
        plan_name,
        "finding_categories",
        raw,
        maximum=MAX_FINDING_CATEGORIES_PER_PLAN,
    )
    unknown = sorted(set(values) - FINDING_CATEGORIES)
    if unknown:
        raise ScanPlanError(
            f"saved scan plan {plan_name!r} has unknown finding categories: "
            f"{', '.join(unknown)}"
        )
    return values


def _parse_required_string_list(
    plan_name: str,
    key: str,
    raw: object,
    *,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ScanPlanError(f"saved scan plan {plan_name!r} requires {key} as an array")
    if not raw:
        raise ScanPlanError(f"saved scan plan {plan_name!r} requires non-empty {key}")
    if len(raw) > maximum:
        raise ScanPlanError(
            f"saved scan plan {plan_name!r} may contain at most {maximum} {key} entries"
        )

    values: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ScanPlanError(
                f"saved scan plan {plan_name!r} requires exact non-empty strings in {key}"
            )
        if value in seen:
            raise ScanPlanError(
                f"saved scan plan {plan_name!r} has duplicate {key} entry {value!r}"
            )
        seen.add(value)
        values.append(value)
    return tuple(values)


def _parse_description(plan_name: str, raw: object) -> str | None:
    if raw is None:
        return None
    if (
        not isinstance(raw, str)
        or not raw
        or raw != raw.strip()
        or len(raw) > MAX_DESCRIPTION_LENGTH
        or any(character in raw for character in "\x00\r\n")
    ):
        raise ScanPlanError(f"saved scan plan {plan_name!r} has an invalid description")
    return raw


def _canonical_path(raw: str | Path, *, label: str) -> Path:
    if isinstance(raw, str) and (not raw or raw != raw.strip()):
        raise ScanPlanError(f"{label} path is empty or malformed")
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ScanPlanError(f"{label} path cannot be resolved") from exc
