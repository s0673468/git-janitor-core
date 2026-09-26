from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from git_janitor.models import CommandResult, LinkedWorktreeState, RepoState
from git_janitor.reconciliation import (
    GithubInventory,
    GithubOwnerCoverage,
    GithubRepository,
    collect_github_inventory,
    load_registry,
    reconcile_inventory,
    reconciliation_findings,
)


FIXED_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


class RegistryLoaderTests(unittest.TestCase):
    def test_loads_strict_registry_with_provenance_and_distinct_owners(self) -> None:
        payload = _registry_payload()
        raw = json.dumps(payload, sort_keys=True).encode()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            path.write_bytes(raw)
            source = load_registry(path, clock=_fixed_clock)

        self.assertTrue(source.complete)
        self.assertEqual(source.schema_version, 1)
        self.assertEqual(source.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(source.observed_at, "2026-08-30T12:00:00+00:00")
        self.assertEqual([project.project_id for project in source.projects], ["alpha", "old"])
        self.assertEqual(source.owners, ("owner", "personal"))

    def test_duplicate_case_insensitive_identity_fails_closed_but_keeps_provenance(self) -> None:
        payload = _registry_payload()
        duplicate = dict(payload["projects"]["alpha"])
        duplicate["display_name"] = "Duplicate"
        duplicate["local_path"] = "duplicate"
        duplicate["github_repo"] = "OWNER/ALPHA"
        payload["projects"]["duplicate"] = duplicate

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            source = load_registry(path, clock=_fixed_clock)

        self.assertFalse(source.complete)
        self.assertEqual(source.schema_version, 1)
        self.assertIsNotNone(source.sha256)
        self.assertEqual(source.projects, ())
        self.assertRegex(source.errors[0], "duplicate github_repo")

    def test_unknown_schema_and_unknown_fields_fail_closed(self) -> None:
        for mutation in ("schema", "boolean schema", "field"):
            with self.subTest(mutation=mutation):
                payload = _registry_payload()
                if mutation == "schema":
                    payload["schema_version"] = 2
                elif mutation == "boolean schema":
                    payload["schema_version"] = True
                else:
                    payload["projects"]["alpha"]["unexpected"] = True
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "projects.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    source = load_registry(path, clock=_fixed_clock)

                self.assertFalse(source.complete)
                self.assertEqual(source.projects, ())


class GithubInventoryTests(unittest.TestCase):
    def test_collects_each_distinct_owner_live_and_keeps_archived_repositories(self) -> None:
        source = _loaded_registry()
        fake = FakeRunner()
        fake.add(["gh", "auth", "status"])
        fake.add(
            _inventory_command("owner"),
            stdout=json.dumps(
                [
                    _github_payload("owner", "alpha", archived=False),
                    _github_payload("owner", "unused", archived=True),
                ]
            ),
        )
        fake.add(
            _inventory_command("personal"),
            stdout=json.dumps(
                [
                    _github_payload("personal", "old", archived=True),
                    {
                        **_github_payload("personal", "empty", archived=False),
                        "defaultBranchRef": None,
                    },
                ]
            ),
        )

        inventory = collect_github_inventory(source, runner=fake, clock=_fixed_clock)

        self.assertTrue(inventory.authenticated)
        self.assertTrue(inventory.complete)
        self.assertEqual([coverage.owner for coverage in inventory.owners], ["owner", "personal"])
        self.assertEqual(
            [repo.full_name for coverage in inventory.owners for repo in coverage.repositories],
            ["owner/alpha", "owner/unused", "personal/empty", "personal/old"],
        )
        self.assertTrue(inventory.owners[0].repositories[1].archived)
        self.assertIsNone(inventory.owners[1].repositories[0].default_branch)
        self.assertEqual(fake.calls.count(_inventory_command("owner")), 1)

    def test_auth_failure_preserves_incomplete_owner_coverage_without_queries(self) -> None:
        source = _loaded_registry()
        fake = FakeRunner()
        fake.add(["gh", "auth", "status"], returncode=1, stderr="not authenticated")

        inventory = collect_github_inventory(source, runner=fake, clock=_fixed_clock)

        self.assertFalse(inventory.authenticated)
        self.assertFalse(inventory.complete)
        self.assertEqual(fake.calls, [["gh", "auth", "status"]])
        self.assertTrue(all(not owner.complete for owner in inventory.owners))
        self.assertTrue(all(owner.repositories == () for owner in inventory.owners))

    def test_malformed_or_saturated_owner_inventory_is_partial(self) -> None:
        for payload, expected in (
            ({"not": "a list"}, "non-list"),
            (
                [_github_payload("owner", f"repo-{index}") for index in range(201)],
                "200-repository safety cap",
            ),
        ):
            with self.subTest(expected=expected):
                source = _loaded_registry()
                fake = FakeRunner()
                fake.add(["gh", "auth", "status"])
                fake.add(_inventory_command("owner"), stdout=json.dumps(payload))
                fake.add(
                    _inventory_command("personal"),
                    stdout=json.dumps([_github_payload("personal", "old", archived=True)]),
                )

                inventory = collect_github_inventory(source, runner=fake, clock=_fixed_clock)

                owner = next(item for item in inventory.owners if item.owner == "owner")
                personal = next(item for item in inventory.owners if item.owner == "personal")
                self.assertFalse(owner.complete)
                self.assertRegex(owner.errors[0], expected)
                self.assertTrue(personal.complete)
                self.assertFalse(inventory.complete)


class ReconciliationTests(unittest.TestCase):
    def test_reconciles_lifecycle_paths_remotes_and_unregistered_inventory(self) -> None:
        payload = _registry_payload()
        payload["projects"]["missing"] = _project(
            "Missing", status="active", local_path="Missing", github_repo="owner/missing"
        )
        registry = _loaded_registry(payload)
        github = _github_inventory(
            {
                "owner": (
                    GithubRepository(
                        full_name="OWNER/ALPHA",
                        owner="OWNER",
                        name="ALPHA",
                        archived=True,
                        visibility="PRIVATE",
                        default_branch="main",
                    ),
                    GithubRepository(
                        full_name="owner/unregistered",
                        owner="owner",
                        name="unregistered",
                        archived=False,
                        visibility="PRIVATE",
                        default_branch="main",
                    ),
                ),
                "personal": (
                    GithubRepository(
                        full_name="personal/old",
                        owner="personal",
                        name="old",
                        archived=False,
                        visibility="PRIVATE",
                        default_branch="main",
                    ),
                ),
            }
        )
        alpha_path = str(Path(registry.code_root).expanduser() / "Alpha")
        repos = [
            RepoState(
                path=alpha_path,
                name="Alpha",
                remote_url="https://github.com/owner/wrong.git",
                github_repo="owner/wrong",
                linked_worktrees=[
                    LinkedWorktreeState(path="/tmp/alpha-worktree", branch="codex/feature"),
                    LinkedWorktreeState(path="/tmp/alpha-worktree", branch="codex/feature"),
                ],
            ),
            RepoState(
                path="/tmp/alpha-copy",
                name="alpha-copy",
                remote_url="https://github.com/owner/alpha.git",
                github_repo="owner/alpha",
            ),
            RepoState(
                path="/tmp/local-only",
                name="local-only",
                remote_url="https://github.com/owner/local-only.git",
                github_repo="owner/local-only",
            ),
        ]

        result = reconcile_inventory(registry, github, repos, clock=_fixed_clock)

        self.assertTrue(result.complete)
        by_id = {row.project_id: row for row in result.rows if row.project_id}
        self.assertEqual(
            set(by_id["alpha"].categories),
            {"canonical-path-mismatch", "canonical-remote-mismatch", "lifecycle-mismatch"},
        )
        self.assertTrue(by_id["alpha"].registry_present)
        self.assertTrue(by_id["alpha"].github_present)
        self.assertTrue(by_id["alpha"].local_present)
        self.assertTrue(by_id["alpha"].canonical_checkout_present)
        self.assertEqual(by_id["alpha"].worktree_paths, ("/tmp/alpha-worktree",))
        self.assertEqual(
            set(by_id["missing"].categories),
            {"missing-canonical-checkout", "missing-github-repo"},
        )
        self.assertTrue(by_id["missing"].registry_present)
        self.assertFalse(by_id["missing"].github_present)
        self.assertFalse(by_id["missing"].local_present)
        self.assertFalse(by_id["missing"].canonical_checkout_present)
        self.assertEqual(by_id["old"].categories, ("lifecycle-mismatch",))
        categories = [category for row in result.rows for category in row.categories]
        self.assertIn("unregistered-local-repo", categories)
        self.assertIn("unregistered-github-repo", categories)
        findings = reconciliation_findings(result)
        self.assertEqual([item.category for item in findings], sorted(item.category for item in findings))
        self.assertTrue(all(not item.category.startswith("auto-") for item in findings))

    def test_incomplete_owner_suppresses_only_that_owners_missing_claims(self) -> None:
        registry = _loaded_registry()
        github = _github_inventory(
            {"personal": ()},
            incomplete_owners={"owner"},
        )

        result = reconcile_inventory(
            registry,
            github,
            [],
            local_complete=False,
            local_errors=("root unavailable",),
            clock=_fixed_clock,
        )

        by_id = {row.project_id: row for row in result.rows if row.project_id}
        self.assertNotIn("missing-github-repo", by_id["alpha"].categories)
        self.assertNotIn("missing-canonical-checkout", by_id["alpha"].categories)
        self.assertNotIn("missing-canonical-checkout", by_id["old"].categories)
        self.assertTrue(result.github.owners[0].errors)
        self.assertFalse(result.complete)

    def test_case_insensitive_identity_is_aligned_and_duplicate_local_paths_dedupe(self) -> None:
        registry = _loaded_registry()
        github = _github_inventory(
            {
                "owner": (
                    GithubRepository(
                        full_name="OWNER/ALPHA",
                        owner="OWNER",
                        name="ALPHA",
                        archived=False,
                        visibility="PRIVATE",
                        default_branch="main",
                    ),
                ),
                "personal": (
                    GithubRepository(
                        full_name="PERSONAL/OLD",
                        owner="PERSONAL",
                        name="OLD",
                        archived=True,
                        visibility="PRIVATE",
                        default_branch="main",
                    ),
                ),
            }
        )
        path = str(Path(registry.code_root).expanduser() / "Alpha")
        repo = RepoState(
            path=path,
            name="Alpha",
            github_repo="OWNER/ALPHA",
            remote_url="https://github.com/OWNER/ALPHA.git",
        )

        result = reconcile_inventory(registry, github, [repo, repo], clock=_fixed_clock)

        alpha = next(row for row in result.rows if row.project_id == "alpha")
        self.assertEqual(alpha.categories, ())
        self.assertEqual(alpha.local_paths, (path,))

    def test_failed_fetch_makes_local_inventory_incomplete(self) -> None:
        registry = _loaded_registry()
        github = _github_inventory({"owner": (), "personal": ()})
        path = str(Path(registry.code_root).expanduser() / "Alpha")
        repo = RepoState(
            path=path,
            name="Alpha",
            github_repo="owner/alpha",
            fetch_prune_status="network unavailable",
        )

        result = reconcile_inventory(registry, github, [repo], clock=_fixed_clock)

        self.assertFalse(result.complete)
        self.assertFalse(result.local.complete)
        self.assertIn("fetch --prune failed: network unavailable", result.local.errors[0])

    def test_canonical_linked_worktree_is_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _registry_payload()
            payload["code_root"] = tmp
            registry = _loaded_registry(payload)
            github = _github_inventory({"owner": (), "personal": ()})
            canonical = Path(tmp) / "Alpha"
            (canonical / ".git").mkdir(parents=True)
            repo = RepoState(
                path=str(Path(tmp) / "alpha-primary"),
                name="Alpha",
                github_repo="owner/alpha",
                fetch_prune_status="ok",
                linked_worktrees=[LinkedWorktreeState(path=str(canonical), branch="main")],
            )

            result = reconcile_inventory(registry, github, [repo], clock=_fixed_clock)

        alpha = next(row for row in result.rows if row.project_id == "alpha")
        self.assertTrue(alpha.canonical_checkout_present)
        self.assertNotIn("missing-canonical-checkout", alpha.categories)
        self.assertNotIn("canonical-path-mismatch", alpha.categories)

    def test_prunable_linked_worktree_is_not_canonical_checkout_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _registry_payload()
            payload["code_root"] = tmp
            registry = _loaded_registry(payload)
            github = _github_inventory({"owner": (), "personal": ()})
            canonical = Path(tmp) / "Alpha"
            repo = RepoState(
                path=str(Path(tmp) / "alpha-primary"),
                name="Alpha",
                github_repo="owner/alpha",
                fetch_prune_status="ok",
                linked_worktrees=[LinkedWorktreeState(path=str(canonical))],
            )

            result = reconcile_inventory(registry, github, [repo], clock=_fixed_clock)

        alpha = next(row for row in result.rows if row.project_id == "alpha")
        self.assertFalse(alpha.canonical_checkout_present)
        self.assertIn("missing-canonical-checkout", alpha.categories)


class FakeRunner:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, ...], list[CommandResult]] = defaultdict(list)
        self.calls: list[list[str]] = []

    def add(
        self,
        args: Sequence[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        command = list(args)
        self.responses[tuple(command)].append(
            CommandResult(command, returncode, stdout, stderr)
        )

    def __call__(self, args: list[str], cwd: Path | None = None, timeout: int = 45) -> CommandResult:
        del cwd, timeout
        self.calls.append(list(args))
        responses = self.responses[tuple(args)]
        if not responses:
            raise AssertionError(f"unexpected command: {args!r}")
        return responses.pop(0)


def _registry_payload() -> dict:
    return {
        "schema_version": 1,
        "code_root": "~/Code",
        "default_validation": {
            "gate": "make check",
            "full_gate": "",
            "narrow": "",
            "notes": "",
        },
        "projects": {
            "alpha": _project(
                "Alpha",
                status="active",
                local_path="Alpha",
                github_repo="owner/alpha",
            ),
            "old": _project(
                "Old",
                status="archived",
                local_path=None,
                github_repo="personal/old",
                archive_path="_archive/Old",
                lifecycle_ref="workspace:_archive/Old",
                validation=None,
            ),
        },
    }


def _project(
    display_name: str,
    *,
    status: str,
    local_path: str | None,
    github_repo: str | None,
    archive_path: str | None = None,
    lifecycle_ref: str | None = None,
    validation: dict | None | object = ...,
) -> dict:
    if validation is ...:
        validation = {"gate": "make check", "full_gate": "", "narrow": "", "notes": ""}
    return {
        "aliases": [],
        "archive_path": archive_path,
        "display_name": display_name,
        "docs_gardener": False,
        "github_repo": github_repo,
        "kind": "repository",
        "layer": "Tools",
        "lifecycle_ref": lifecycle_ref,
        "local_path": local_path,
        "notes": f"{display_name} notes.",
        "propagation_targets": [],
        "replacement": [],
        "status": status,
        "status_since": None,
        "truth_store": "repository",
        "validation": validation,
    }


def _loaded_registry(payload: dict | None = None):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "projects.json"
        path.write_text(json.dumps(payload or _registry_payload()), encoding="utf-8")
        source = load_registry(path, clock=_fixed_clock)
    if not source.complete:
        raise AssertionError(source.errors)
    return source


def _github_payload(owner: str, name: str, *, archived: bool = False) -> dict:
    return {
        "name": name,
        "nameWithOwner": f"{owner}/{name}",
        "isArchived": archived,
        "visibility": "PRIVATE",
        "defaultBranchRef": {"name": "main"},
    }


def _inventory_command(owner: str) -> list[str]:
    return [
        "gh",
        "repo",
        "list",
        owner,
        "--limit",
        "201",
        "--json",
        "name,nameWithOwner,isArchived,visibility,defaultBranchRef",
    ]


def _github_inventory(
    repositories: dict[str, tuple[GithubRepository, ...]],
    *,
    incomplete_owners: set[str] | None = None,
) -> GithubInventory:
    incomplete_owners = incomplete_owners or set()
    owner_names = sorted({"owner", "personal", *repositories}, key=str.casefold)
    owners = tuple(
        GithubOwnerCoverage(
            owner=owner,
            complete=owner not in incomplete_owners,
            observed_at="2026-08-30T12:00:00+00:00",
            repositories=tuple(sorted(repositories.get(owner, ()), key=lambda item: item.full_name.casefold())),
            errors=("inventory failed",) if owner in incomplete_owners else (),
        )
        for owner in owner_names
    )
    return GithubInventory(
        authenticated=True,
        complete=all(owner.complete for owner in owners),
        observed_at="2026-08-30T12:00:00+00:00",
        owners=owners,
        errors=tuple(error for owner in owners for error in owner.errors),
    )


def _fixed_clock() -> datetime:
    return FIXED_NOW


if __name__ == "__main__":
    unittest.main()
