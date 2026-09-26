from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import tomllib

from .scan_plans import SavedScanPlan, parse_scan_plans


DEFAULT_EXCLUDES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".dart_tool",
    "build",
    "dist",
    ".pytest_cache",
    "__pycache__",
}
DEFAULT_PROJECT_REGISTRY_PATH = (
    Path("~/.config/git-janitor/projects.json").expanduser()
)
_INVENTORY_KEYS = frozenset({"project_registry_path"})


@dataclass(frozen=True)
class ScannerConfig:
    scan_roots: list[Path] = field(default_factory=list)
    repos: list[Path] = field(default_factory=list)
    max_depth: int = 3
    fetch_prune: bool = True
    default_branch: str = "main"
    github_author: str = "@me"
    stale_branch_days: int = 14
    command_timeout_seconds: int = 45
    exclude_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDES))
    high_risk_patterns: list[str] = field(default_factory=list)
    auto_merge_green_prs: bool = False
    auto_delete_merged_branches: bool = False
    auto_mark_drafts_ready: bool = False
    auto_fast_forward_default_branch: bool = False
    apply_categories: frozenset[str] = frozenset()
    ledger_path: str | None = None
    scan_plans: dict[str, SavedScanPlan] = field(default_factory=dict)
    project_registry_path: Path = DEFAULT_PROJECT_REGISTRY_PATH


def load_config(path: str | Path) -> ScannerConfig:
    config_path = Path(path).expanduser()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    scanner = raw.get("scanner", {})
    actions = raw.get("actions", {})
    inventory = raw.get("inventory", {})
    project_registry_path = _project_registry_path(inventory)

    return ScannerConfig(
        scan_roots=[Path(p).expanduser() for p in scanner.get("scan_roots", [])],
        repos=[Path(p).expanduser() for p in scanner.get("repos", [])],
        max_depth=int(scanner.get("max_depth", 3)),
        fetch_prune=bool(scanner.get("fetch_prune", True)),
        default_branch=str(scanner.get("default_branch", "main")),
        github_author=str(scanner.get("github_author", "@me")),
        stale_branch_days=int(scanner.get("stale_branch_days", 14)),
        command_timeout_seconds=int(scanner.get("command_timeout_seconds", 45)),
        exclude_dirs=set(scanner.get("exclude_dirs", DEFAULT_EXCLUDES)),
        high_risk_patterns=[
            str(pattern).lower() for pattern in scanner.get("high_risk_patterns", [])
        ],
        auto_merge_green_prs=bool(actions.get("auto_merge_green_prs", False)),
        auto_delete_merged_branches=bool(
            actions.get("auto_delete_merged_branches", False)
        ),
        auto_mark_drafts_ready=bool(actions.get("auto_mark_drafts_ready", False)),
        auto_fast_forward_default_branch=bool(
            actions.get("auto_fast_forward_default_branch", False)
        ),
        apply_categories=frozenset(str(category) for category in actions.get("apply_categories", [])),
        ledger_path=(
            str(actions["ledger_path"])
            if actions.get("ledger_path") is not None
            else None
        ),
        scan_plans=parse_scan_plans(raw.get("scan_plans", {})),
        project_registry_path=project_registry_path,
    )


def _project_registry_path(raw: object) -> Path:
    if not isinstance(raw, Mapping):
        raise ValueError("inventory must be a TOML table")
    unknown = sorted(set(raw) - _INVENTORY_KEYS)
    if unknown:
        raise ValueError(f"inventory has unknown key(s): {', '.join(unknown)}")
    value = raw.get("project_registry_path", str(DEFAULT_PROJECT_REGISTRY_PATH))
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("inventory.project_registry_path must be a non-empty path string")
    return Path(value).expanduser()
