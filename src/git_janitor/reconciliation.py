from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable

from .git import _same_path, run_command
from .models import CommandResult, Finding, RepoState
from .repo_facts import list_repo_meta


DEFAULT_REGISTRY_PATH = Path(
    "~/.config/git-janitor/projects.json"
).expanduser()
REGISTRY_SCHEMA_VERSION = 1
VALID_KINDS = frozenset({"repository", "runtime", "surface"})
VALID_STATUSES = frozenset({"active", "retiring", "archived"})
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ROOT_FIELDS = frozenset({"schema_version", "code_root", "default_validation", "projects"})
PROJECT_FIELDS = frozenset(
    {
        "display_name",
        "kind",
        "status",
        "status_since",
        "local_path",
        "github_repo",
        "archive_path",
        "layer",
        "truth_store",
        "notes",
        "aliases",
        "replacement",
        "propagation_targets",
        "lifecycle_ref",
        "docs_gardener",
        "validation",
    }
)
VALIDATION_FIELDS = frozenset({"gate", "full_gate", "narrow", "notes"})

Runner = Callable[[list[str], Path | None, int], CommandResult]
Clock = Callable[[], datetime]


class RegistryValidationError(ValueError):
    """The rendered project registry is malformed or internally ambiguous."""


@dataclass(frozen=True)
class RegistryProject:
    project_id: str
    display_name: str
    kind: str
    status: str
    local_path: str | None
    github_repo: str | None
    archive_path: str | None


@dataclass(frozen=True)
class RegistrySource:
    path: str
    observed_at: str
    complete: bool
    schema_version: int | None
    sha256: str | None
    code_root: str | None
    projects: tuple[RegistryProject, ...]
    errors: tuple[str, ...] = ()

    @property
    def owners(self) -> tuple[str, ...]:
        owners: dict[str, str] = {}
        for project in self.projects:
            if not project.github_repo:
                continue
            owner = project.github_repo.split("/", 1)[0]
            owners.setdefault(owner.casefold(), owner)
        return tuple(sorted(owners.values(), key=str.casefold))


@dataclass(frozen=True)
class GithubRepository:
    full_name: str
    owner: str
    name: str
    archived: bool
    visibility: str
    default_branch: str | None


@dataclass(frozen=True)
class GithubOwnerCoverage:
    owner: str
    complete: bool
    observed_at: str
    repositories: tuple[GithubRepository, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class GithubInventory:
    authenticated: bool | None
    complete: bool
    observed_at: str
    owners: tuple[GithubOwnerCoverage, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalRepository:
    path: str
    name: str
    github_repo: str | None
    remote_url: str | None
    worktree_paths: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalInventory:
    complete: bool
    observed_at: str
    repositories: tuple[LocalRepository, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationIssue:
    severity: str
    category: str
    title: str
    detail: str
    repo_path: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class ReconciliationRow:
    key: str
    project_id: str | None
    registry_present: bool
    github_present: bool
    local_present: bool
    canonical_checkout_present: bool | None
    project: RegistryProject | None
    github_repository: GithubRepository | None
    local_paths: tuple[str, ...]
    worktree_paths: tuple[str, ...]
    issues: tuple[ReconciliationIssue, ...] = ()

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({issue.category for issue in self.issues}))


@dataclass(frozen=True)
class ReconciliationResult:
    complete: bool
    observed_at: str
    registry: RegistrySource
    github: GithubInventory
    local: LocalInventory
    rows: tuple[ReconciliationRow, ...]
    errors: tuple[str, ...] = ()


def load_registry(
    path: str | Path = DEFAULT_REGISTRY_PATH,
    *,
    clock: Clock | None = None,
) -> RegistrySource:
    """Load the installed schema-v1 registry while retaining failure provenance."""
    observed_at = _timestamp((clock or _utc_now)())
    registry_path = Path(path).expanduser().absolute()
    try:
        raw_bytes = registry_path.read_bytes()
    except OSError as exc:
        return RegistrySource(
            path=str(registry_path),
            observed_at=observed_at,
            complete=False,
            schema_version=None,
            sha256=None,
            code_root=None,
            projects=(),
            errors=(f"could not read project registry: {exc}",),
        )

    digest = hashlib.sha256(raw_bytes).hexdigest()
    schema_version: int | None = None
    try:
        raw = json.loads(raw_bytes)
        if isinstance(raw, dict) and type(raw.get("schema_version")) is int:
            schema_version = raw["schema_version"]
        code_root, projects = _parse_registry(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RegistryValidationError) as exc:
        return RegistrySource(
            path=str(registry_path),
            observed_at=observed_at,
            complete=False,
            schema_version=schema_version,
            sha256=digest,
            code_root=None,
            projects=(),
            errors=(f"invalid project registry: {exc}",),
        )

    return RegistrySource(
        path=str(registry_path),
        observed_at=observed_at,
        complete=True,
        schema_version=REGISTRY_SCHEMA_VERSION,
        sha256=digest,
        code_root=code_root,
        projects=projects,
    )


def collect_github_inventory(
    registry: RegistrySource,
    *,
    runner: Runner = run_command,
    clock: Clock | None = None,
    timeout: int = 60,
) -> GithubInventory:
    """Collect live, authenticated, archived-inclusive inventories for registry owners."""
    clock = clock or _utc_now
    observed_at = _timestamp(clock())
    if not registry.complete:
        return GithubInventory(
            authenticated=None,
            complete=False,
            observed_at=observed_at,
            owners=(),
            errors=("project registry is incomplete; GitHub owner scope is unavailable",),
        )

    owners = registry.owners
    auth = runner(["gh", "auth", "status"], None, min(timeout, 10))
    if auth.returncode != 0:
        detail = auth.stderr or auth.stdout or "gh auth status failed"
        coverages = tuple(
            GithubOwnerCoverage(
                owner=owner,
                complete=False,
                observed_at=observed_at,
                errors=(f"authentication unavailable: {detail}",),
            )
            for owner in owners
        )
        return GithubInventory(
            authenticated=False,
            complete=False,
            observed_at=observed_at,
            owners=coverages,
            errors=(f"GitHub authentication unavailable: {detail}",),
        )

    coverages: list[GithubOwnerCoverage] = []
    inventory_errors: list[str] = []
    seen_repositories: set[str] = set()
    for owner in owners:
        owner_observed_at = _timestamp(clock())
        try:
            metas = list_repo_meta(
                owner=owner,
                runner=runner,
                timeout=timeout,
                include_archived=True,
            )
            repositories: list[GithubRepository] = []
            for meta in metas:
                if meta.full_name is None or meta.archived is None:
                    raise RuntimeError("GitHub repository inventory omitted identity fields")
                identity = meta.full_name.casefold()
                if identity in seen_repositories:
                    raise RuntimeError(f"duplicate GitHub repository identity: {meta.full_name}")
                seen_repositories.add(identity)
                repo_owner, repo_name = meta.full_name.split("/", 1)
                repositories.append(
                    GithubRepository(
                        full_name=meta.full_name,
                        owner=repo_owner,
                        name=repo_name,
                        archived=meta.archived,
                        visibility=meta.visibility,
                        default_branch=meta.default_branch,
                    )
                )
            coverages.append(
                GithubOwnerCoverage(
                    owner=owner,
                    complete=True,
                    observed_at=owner_observed_at,
                    repositories=tuple(
                        sorted(repositories, key=lambda item: item.full_name.casefold())
                    ),
                )
            )
        except (RuntimeError, ValueError) as exc:
            message = f"{owner}: {exc}"
            inventory_errors.append(message)
            coverages.append(
                GithubOwnerCoverage(
                    owner=owner,
                    complete=False,
                    observed_at=owner_observed_at,
                    errors=(message,),
                )
            )

    coverages.sort(key=lambda item: item.owner.casefold())
    return GithubInventory(
        authenticated=True,
        complete=not inventory_errors,
        observed_at=observed_at,
        owners=tuple(coverages),
        errors=tuple(inventory_errors),
    )


def reconcile_inventory(
    registry: RegistrySource,
    github: GithubInventory,
    repos: list[RepoState],
    *,
    local_complete: bool = True,
    local_errors: tuple[str, ...] = (),
    clock: Clock | None = None,
) -> ReconciliationResult:
    """Reconcile intent and observations without authorizing or performing an action."""
    observed_at = _timestamp((clock or _utc_now)())
    local = _build_local_inventory(
        registry,
        repos,
        observed_at=observed_at,
        complete=local_complete,
        errors=local_errors,
    )
    errors = [
        *(f"registry: {error}" for error in registry.errors),
        *(f"github: {error}" for error in github.errors),
        *(f"local: {error}" for error in local.errors),
    ]
    if not registry.complete:
        return ReconciliationResult(
            complete=False,
            observed_at=observed_at,
            registry=registry,
            github=github,
            local=local,
            rows=(),
            errors=tuple(errors),
        )

    github_by_name: dict[str, GithubRepository] = {}
    owner_complete: dict[str, bool] = {}
    for coverage in github.owners:
        owner_complete[coverage.owner.casefold()] = coverage.complete
        for repository in coverage.repositories:
            github_by_name.setdefault(repository.full_name.casefold(), repository)

    local_repos = list(local.repositories)
    claimed_local: set[int] = set()
    claimed_github = {
        project.github_repo.casefold()
        for project in registry.projects
        if project.github_repo
    }
    rows: list[ReconciliationRow] = []
    code_root = Path(registry.code_root or "").expanduser()

    for project in registry.projects:
        canonical_path = (
            _normalized_path(code_root / project.local_path) if project.local_path else None
        )
        expected_github = project.github_repo.casefold() if project.github_repo else None
        canonical_indexes = {
            index
            for index, local_repo in enumerate(local_repos)
            if canonical_path
            and (
                _same_path(Path(canonical_path), Path(local_repo.path).expanduser())
                or any(
                    (Path(path).expanduser() / ".git").exists()
                    and _same_path(Path(canonical_path), Path(path).expanduser())
                    for path in local_repo.worktree_paths
                )
            )
        }
        remote_indexes = {
            index
            for index, local_repo in enumerate(local_repos)
            if expected_github
            and local_repo.github_repo
            and local_repo.github_repo.casefold() == expected_github
        }
        matched_indexes = canonical_indexes | remote_indexes
        claimed_local.update(matched_indexes)
        matched_locals = [local_repos[index] for index in sorted(matched_indexes)]
        github_repo = github_by_name.get(expected_github) if expected_github else None
        issues = _project_issues(
            project,
            canonical_path=canonical_path,
            canonical_indexes=canonical_indexes,
            remote_indexes=remote_indexes,
            local_repos=local_repos,
            github_repo=github_repo,
            owner_complete=owner_complete,
            local_complete=local.complete,
        )
        rows.append(
            ReconciliationRow(
                key=f"project:{project.project_id}",
                project_id=project.project_id,
                registry_present=True,
                github_present=github_repo is not None,
                local_present=bool(matched_locals),
                canonical_checkout_present=(
                    bool(canonical_indexes) if project.local_path else None
                ),
                project=project,
                github_repository=github_repo,
                local_paths=tuple(sorted({repo.path for repo in matched_locals}, key=str.casefold)),
                worktree_paths=tuple(
                    sorted(
                        {
                            path
                            for repo in matched_locals
                            for path in repo.worktree_paths
                        },
                        key=str.casefold,
                    )
                ),
                issues=tuple(sorted(issues, key=lambda item: (item.category, item.detail))),
            )
        )

    unregistered_github = {
        identity: repository
        for identity, repository in github_by_name.items()
        if identity not in claimed_github
    }
    consumed_unregistered_local: set[int] = set()
    for identity, repository in sorted(unregistered_github.items()):
        matching_indexes = {
            index
            for index, local_repo in enumerate(local_repos)
            if index not in claimed_local
            and local_repo.github_repo
            and local_repo.github_repo.casefold() == identity
        }
        consumed_unregistered_local.update(matching_indexes)
        matching_locals = [local_repos[index] for index in sorted(matching_indexes)]
        issues = [
            ReconciliationIssue(
                severity="medium",
                category="unregistered-github-repo",
                title=f"{repository.full_name}: GitHub repository is not registered",
                detail="The authenticated GitHub inventory contains no matching registry project.",
                url=f"https://github.com/{repository.full_name}",
            )
        ]
        if matching_locals:
            issues.append(
                ReconciliationIssue(
                    severity="medium",
                    category="unregistered-local-repo",
                    title=f"{repository.full_name}: local checkout is not registered",
                    detail="The local checkout remote has no matching registry project.",
                    repo_path=matching_locals[0].path,
                )
            )
        rows.append(
            ReconciliationRow(
                key=f"github:{identity}",
                project_id=None,
                registry_present=False,
                github_present=True,
                local_present=bool(matching_locals),
                canonical_checkout_present=None,
                project=None,
                github_repository=repository,
                local_paths=tuple(repo.path for repo in matching_locals),
                worktree_paths=tuple(
                    sorted(
                        {path for repo in matching_locals for path in repo.worktree_paths},
                        key=str.casefold,
                    )
                ),
                issues=tuple(issues),
            )
        )

    for index, local_repo in enumerate(local_repos):
        if index in claimed_local or index in consumed_unregistered_local:
            continue
        rows.append(
            ReconciliationRow(
                key=f"local:{_normalized_path(Path(local_repo.path))}",
                project_id=None,
                registry_present=False,
                github_present=False,
                local_present=True,
                canonical_checkout_present=None,
                project=None,
                github_repository=None,
                local_paths=(local_repo.path,),
                worktree_paths=local_repo.worktree_paths,
                issues=(
                    ReconciliationIssue(
                        severity="medium",
                        category="unregistered-local-repo",
                        title=f"{local_repo.name}: local repository is not registered",
                        detail=(
                            "Neither the checkout path nor its GitHub remote matches a registry project."
                        ),
                        repo_path=local_repo.path,
                    ),
                ),
            )
        )

    rows.sort(key=_row_sort_key)
    return ReconciliationResult(
        complete=registry.complete and github.complete and local.complete,
        observed_at=observed_at,
        registry=registry,
        github=github,
        local=local,
        rows=tuple(rows),
        errors=tuple(errors),
    )


def reconciliation_findings(result: ReconciliationResult) -> list[Finding]:
    """Project reconciliation issues into ordinary, non-executable scanner findings."""
    findings = [
        Finding(
            severity=issue.severity,
            category=issue.category,
            title=issue.title,
            detail=issue.detail,
            repo_path=issue.repo_path,
            url=issue.url,
            recommended_action="Reconcile the registry and observed inventory manually.",
        )
        for row in result.rows
        for issue in row.issues
    ]
    return sorted(findings, key=lambda item: (item.category, item.title, item.repo_path or ""))


def _parse_registry(raw: Any) -> tuple[str, tuple[RegistryProject, ...]]:
    root = _object(raw, "registry")
    _exact_fields(root, "registry", ROOT_FIELDS)
    if (
        type(root.get("schema_version")) is not int
        or root["schema_version"] != REGISTRY_SCHEMA_VERSION
    ):
        raise RegistryValidationError(
            f"schema_version must be {REGISTRY_SCHEMA_VERSION}"
        )
    code_root = _nonempty_string(root.get("code_root"), "code_root")
    _validation(root.get("default_validation"), "default_validation")
    projects_raw = _object(root.get("projects"), "projects")
    if not projects_raw:
        raise RegistryValidationError("projects must be a non-empty object")

    projects: list[RegistryProject] = []
    references: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    seen_names: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    seen_archives: dict[str, str] = {}
    seen_repos: dict[str, str] = {}
    for project_id in sorted(projects_raw):
        if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
            raise RegistryValidationError(f"invalid project id: {project_id!r}")
        project_raw = _object(projects_raw[project_id], f"projects.{project_id}")
        _exact_fields(project_raw, f"projects.{project_id}", PROJECT_FIELDS)
        kind = _choice(project_raw.get("kind"), VALID_KINDS, f"projects.{project_id}.kind")
        status = _choice(
            project_raw.get("status"), VALID_STATUSES, f"projects.{project_id}.status"
        )
        display_name = _nonempty_string(
            project_raw.get("display_name"), f"projects.{project_id}.display_name"
        )
        local_path = _optional_relative_path(
            project_raw.get("local_path"), f"projects.{project_id}.local_path"
        )
        github_repo = _optional_github_repo(
            project_raw.get("github_repo"), f"projects.{project_id}.github_repo"
        )
        archive_path = _optional_relative_path(
            project_raw.get("archive_path"), f"projects.{project_id}.archive_path"
        )
        lifecycle_ref = _optional_string(
            project_raw.get("lifecycle_ref"), f"projects.{project_id}.lifecycle_ref"
        )
        _optional_string(
            project_raw.get("status_since"), f"projects.{project_id}.status_since"
        )
        _nonempty_string(project_raw.get("layer"), f"projects.{project_id}.layer")
        _nonempty_string(
            project_raw.get("truth_store"), f"projects.{project_id}.truth_store"
        )
        _nonempty_string(project_raw.get("notes"), f"projects.{project_id}.notes")
        aliases = _string_list(project_raw.get("aliases"), f"projects.{project_id}.aliases")
        replacement = _string_list(
            project_raw.get("replacement"), f"projects.{project_id}.replacement"
        )
        propagation_targets = _string_list(
            project_raw.get("propagation_targets"),
            f"projects.{project_id}.propagation_targets",
        )
        docs_gardener = project_raw.get("docs_gardener")
        if not isinstance(docs_gardener, bool):
            raise RegistryValidationError(
                f"projects.{project_id}.docs_gardener must be a boolean"
            )
        validation_raw = project_raw.get("validation")
        if validation_raw is not None:
            _validation(validation_raw, f"projects.{project_id}.validation")
        if kind == "repository" and status != "archived":
            if not local_path or not github_repo or validation_raw is None:
                raise RegistryValidationError(
                    f"projects.{project_id}: non-archived repository requires "
                    "local_path, github_repo, and validation"
                )
        if kind != "repository" and validation_raw is not None:
            raise RegistryValidationError(
                f"projects.{project_id}: only repositories may define validation"
            )
        if status == "retiring" and (not archive_path or not lifecycle_ref):
            raise RegistryValidationError(
                f"projects.{project_id}: retiring project requires archive_path and lifecycle_ref"
            )
        if status == "archived" and not lifecycle_ref:
            raise RegistryValidationError(
                f"projects.{project_id}: archived project requires lifecycle_ref"
            )
        if docs_gardener and (kind != "repository" or status != "active"):
            raise RegistryValidationError(
                f"projects.{project_id}: docs_gardener requires an active repository"
            )

        _claim(seen_names, project_id.casefold(), project_id, "project name")
        _claim(seen_names, display_name.casefold(), project_id, "project name")
        for alias in aliases:
            _claim(seen_names, alias.casefold(), project_id, "project name")
        if local_path:
            _claim(seen_paths, local_path.casefold(), project_id, "local_path")
        if archive_path:
            _claim(seen_archives, archive_path.casefold(), project_id, "archive_path")
        if github_repo:
            _claim(seen_repos, github_repo.casefold(), project_id, "github_repo")
        references[project_id] = (replacement, propagation_targets)
        projects.append(
            RegistryProject(
                project_id=project_id,
                display_name=display_name,
                kind=kind,
                status=status,
                local_path=local_path,
                github_repo=github_repo,
                archive_path=archive_path,
            )
        )

    known_projects = {project.project_id for project in projects}
    for project_id, groups in references.items():
        for field, values in zip(("replacement", "propagation_targets"), groups, strict=True):
            for value in values:
                if value not in known_projects or value == project_id:
                    raise RegistryValidationError(
                        f"projects.{project_id}.{field} has invalid project reference {value!r}"
                    )
    return code_root, tuple(projects)


def _project_issues(
    project: RegistryProject,
    *,
    canonical_path: str | None,
    canonical_indexes: set[int],
    remote_indexes: set[int],
    local_repos: list[LocalRepository],
    github_repo: GithubRepository | None,
    owner_complete: dict[str, bool],
    local_complete: bool,
) -> list[ReconciliationIssue]:
    issues: list[ReconciliationIssue] = []
    expected_url = f"https://github.com/{project.github_repo}" if project.github_repo else None
    expected_path = str(Path(canonical_path)) if canonical_path else None

    if project.status != "archived" and project.local_path and not canonical_indexes:
        if local_complete:
            issues.append(
                ReconciliationIssue(
                    severity="high",
                    category="missing-canonical-checkout",
                    title=f"{project.display_name}: canonical checkout is missing",
                    detail=f"No local repository was observed at {expected_path}.",
                    repo_path=expected_path,
                )
            )

    if project.github_repo and github_repo is None:
        owner = project.github_repo.split("/", 1)[0].casefold()
        if owner_complete.get(owner, False):
            issues.append(
                ReconciliationIssue(
                    severity="high",
                    category="missing-github-repo",
                    title=f"{project.display_name}: registered GitHub repository is missing",
                    detail=(
                        f"Authenticated inventory for {project.github_repo} contained no repository."
                    ),
                    url=expected_url,
                )
            )

    if github_repo is not None:
        expected_archived = project.status == "archived"
        if github_repo.archived != expected_archived:
            issues.append(
                ReconciliationIssue(
                    severity="high",
                    category="lifecycle-mismatch",
                    title=f"{project.display_name}: registry and GitHub lifecycle disagree",
                    detail=(
                        f"registry_status={project.status}, github_archived={github_repo.archived}."
                    ),
                    url=f"https://github.com/{github_repo.full_name}",
                )
            )

    matched_indexes = canonical_indexes | remote_indexes
    if project.status == "archived" and matched_indexes:
        if not any(issue.category == "lifecycle-mismatch" for issue in issues):
            issues.append(
                ReconciliationIssue(
                    severity="high",
                    category="lifecycle-mismatch",
                    title=f"{project.display_name}: archived project still has a checkout",
                    detail="Local inventory contains a checkout matching this archived project.",
                    repo_path=local_repos[min(matched_indexes)].path,
                )
            )

    if project.github_repo:
        mismatches = [
            local_repos[index]
            for index in sorted(canonical_indexes)
            if not local_repos[index].github_repo
            or local_repos[index].github_repo.casefold() != project.github_repo.casefold()
        ]
        if mismatches:
            observed = mismatches[0].github_repo or "unresolved"
            issues.append(
                ReconciliationIssue(
                    severity="high",
                    category="canonical-remote-mismatch",
                    title=f"{project.display_name}: canonical checkout remote disagrees",
                    detail=f"expected={project.github_repo}, observed={observed}.",
                    repo_path=mismatches[0].path,
                )
            )

    noncanonical = remote_indexes - canonical_indexes
    if project.local_path and noncanonical:
        paths = sorted(local_repos[index].path for index in noncanonical)
        issues.append(
            ReconciliationIssue(
                severity="high",
                category="canonical-path-mismatch",
                title=f"{project.display_name}: checkout exists outside its canonical path",
                detail=f"expected={expected_path}, observed={', '.join(paths)}.",
                repo_path=paths[0],
            )
        )
    return issues


def _build_local_inventory(
    registry: RegistrySource,
    repos: list[RepoState],
    *,
    observed_at: str,
    complete: bool,
    errors: tuple[str, ...],
) -> LocalInventory:
    canonical_paths = {
        _normalized_path(Path(registry.code_root).expanduser() / project.local_path)
        for project in registry.projects
        if registry.code_root and project.local_path
    }
    nodes: dict[str, str] = {}
    adjacency: dict[str, set[str]] = {}
    repo_by_path: dict[str, list[RepoState]] = {}
    internal_errors = list(errors)

    for repo in repos:
        repo_path = _normalized_path(Path(repo.path))
        nodes.setdefault(repo_path, repo.path)
        adjacency.setdefault(repo_path, set())
        repo_by_path.setdefault(repo_path, []).append(repo)
        for worktree in repo.linked_worktrees:
            worktree_path = _normalized_path(Path(worktree.path))
            nodes.setdefault(worktree_path, worktree.path)
            adjacency.setdefault(worktree_path, set())
            adjacency[repo_path].add(worktree_path)
            adjacency[worktree_path].add(repo_path)

    components: list[set[str]] = []
    unseen = set(nodes)
    while unseen:
        seed = min(unseen)
        stack = [seed]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            unseen.discard(current)
            stack.extend(adjacency.get(current, ()))
        components.append(component)

    local_repositories: list[LocalRepository] = []
    for component in components:
        candidates = [
            (path, repo)
            for path in component
            for repo in repo_by_path.get(path, ())
        ]
        if not candidates:
            continue
        representative_path, representative = min(
            candidates,
            key=lambda item: (
                0 if item[0] in canonical_paths else 1,
                -len(item[1].linked_worktrees),
                item[0],
            ),
        )
        identities = {
            repo.github_repo.casefold(): repo.github_repo
            for _path, repo in candidates
            if repo.github_repo
        }
        if len(identities) > 1:
            internal_errors.append(
                f"linked checkout group at {nodes[representative_path]} has conflicting GitHub remotes"
            )
        github_repo = identities[min(identities)] if identities else None
        remote_urls = sorted(
            {repo.remote_url for _path, repo in candidates if repo.remote_url},
            key=str.casefold,
        )
        component_errors = sorted(
            {
                error
                for _path, repo in candidates
                for error in (
                    *repo.errors,
                    *(
                        (
                            "fetch --prune failed: "
                            f"{repo.fetch_prune_status}"
                        ,)
                        if repo.fetch_prune_status not in (None, "ok")
                        else ()
                    ),
                )
            }
        )
        internal_errors.extend(
            f"{nodes[representative_path]}: {error}" for error in component_errors
        )
        local_repositories.append(
            LocalRepository(
                path=nodes[representative_path],
                name=representative.name,
                github_repo=github_repo,
                remote_url=remote_urls[0] if remote_urls else None,
                worktree_paths=tuple(
                    sorted(
                        {nodes[path] for path in component if path != representative_path},
                        key=str.casefold,
                    )
                ),
                errors=tuple(component_errors),
            )
        )
    local_repositories.sort(key=lambda item: _normalized_path(Path(item.path)))
    deduped_errors = tuple(dict.fromkeys(internal_errors))
    return LocalInventory(
        complete=complete and not deduped_errors,
        observed_at=observed_at,
        repositories=tuple(local_repositories),
        errors=deduped_errors,
    )


def _validation(raw: Any, label: str) -> None:
    value = _object(raw, label)
    _exact_fields(value, label, VALIDATION_FIELDS)
    _nonempty_string(value.get("gate"), f"{label}.gate")
    for field in ("full_gate", "narrow", "notes"):
        _string(value.get(field), f"{label}.{field}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistryValidationError(f"{label} must be an object")
    return value


def _exact_fields(value: dict[str, Any], label: str, expected: frozenset[str]) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise RegistryValidationError(f"{label} is missing fields: {', '.join(missing)}")
    if unknown:
        raise RegistryValidationError(f"{label} has unknown fields: {', '.join(unknown)}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise RegistryValidationError(f"{label} must be a trimmed string")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    result = _string(value, label)
    if not result:
        raise RegistryValidationError(f"{label} must be a non-empty string")
    return result


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, label)


def _choice(value: Any, choices: frozenset[str], label: str) -> str:
    result = _nonempty_string(value, label)
    if result not in choices:
        raise RegistryValidationError(f"{label} must be one of: {', '.join(sorted(choices))}")
    return result


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RegistryValidationError(f"{label} must be an array")
    result = tuple(_nonempty_string(item, f"{label}[]") for item in value)
    if len({item.casefold() for item in result}) != len(result):
        raise RegistryValidationError(f"{label} contains duplicates")
    return result


def _optional_relative_path(value: Any, label: str) -> str | None:
    if value is None:
        return None
    result = _nonempty_string(value, label)
    path = PurePosixPath(result)
    if path.is_absolute() or ".." in path.parts or result.startswith("~"):
        raise RegistryValidationError(f"{label} must be relative to code_root")
    return result


def _optional_github_repo(value: Any, label: str) -> str | None:
    if value is None:
        return None
    result = _nonempty_string(value, label)
    if not GITHUB_REPO_RE.fullmatch(result):
        raise RegistryValidationError(f"{label} must be an owner/name GitHub repository")
    return result


def _claim(seen: dict[str, str], value: str, project_id: str, label: str) -> None:
    previous = seen.get(value)
    if previous is not None and previous != project_id:
        raise RegistryValidationError(
            f"duplicate {label} {value!r}: projects.{previous} and projects.{project_id}"
        )
    seen[value] = project_id


def _row_sort_key(row: ReconciliationRow) -> tuple[int, str]:
    if row.project_id is not None:
        return (0, row.project_id.casefold())
    if row.github_repository is not None:
        return (1, row.github_repository.full_name.casefold())
    return (2, row.key.casefold())


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path.expanduser()))


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
