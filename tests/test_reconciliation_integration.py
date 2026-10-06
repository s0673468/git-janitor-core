from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from git_janitor import cli
from git_janitor.models import BranchState, CommandResult, LinkedWorktreeState, RepoState
from git_janitor.reproduce import restore_report
from git_janitor.report import render_json, render_markdown


class ReconciliationCliIntegrationTests(unittest.TestCase):
    def test_complete_reconciliation_uses_injected_runner_and_renders_provenance(self) -> None:
        with _scenario() as scenario:
            fake = FakeRunner()
            fake.add(["gh", "auth", "status"])
            fake.add(
                _inventory_command(),
                stdout=json.dumps([_github_repo()]),
            )
            repo = _repo(
                scenario.canonical_path,
                linked_worktrees=[
                    LinkedWorktreeState(path="/tmp/alpha-one", branch="codex/one"),
                    LinkedWorktreeState(path="/tmp/alpha-two", branch="codex/two"),
                ],
            )

            status, markdown, payload = _run_cli(scenario, fake, repos=[repo])

        self.assertEqual(status, 0)
        self.assertEqual(fake.calls, [["gh", "auth", "status"], _inventory_command()])
        self.assertIn("## Inventory Reconciliation", markdown)
        self.assertIn("Evidence coverage: **complete**", markdown)
        self.assertIn("schema `1`; SHA-256", markdown)
        self.assertIn(f"Registry path: `{scenario.registry_path}`", markdown)
        self.assertIn("GitHub: complete; authenticated=yes; 1 repositories", markdown)
        self.assertIn("Local: complete; 1 repositories; 2 linked worktrees", markdown)
        self.assertIn("Inventory reconciliation completed with no mismatches.", markdown)

        reconciliation = payload["reconciliation"]
        self.assertTrue(reconciliation["complete"])
        self.assertEqual(reconciliation["registry"]["path"], str(scenario.registry_path))
        self.assertEqual(reconciliation["registry"]["schema_version"], 1)
        self.assertEqual(reconciliation["github"]["owners"][0]["owner"], "owner")
        self.assertEqual(reconciliation["local"]["repositories"][0]["worktree_paths"], [
            "/tmp/alpha-one",
            "/tmp/alpha-two",
        ])
        self.assertEqual(reconciliation["rows"][0]["project_id"], "alpha")
        self.assertTrue(reconciliation["rows"][0]["registry_present"])
        self.assertTrue(reconciliation["rows"][0]["github_present"])
        self.assertTrue(reconciliation["rows"][0]["local_present"])
        self.assertTrue(reconciliation["rows"][0]["canonical_checkout_present"])
        self.assertEqual(payload["automation_decisions"], [])
        self.assertEqual(payload["execution_results"], [])
        restored = restore_report(payload)
        self.assertEqual(json.loads(render_json(restored)), payload)
        self.assertEqual(render_markdown(restored), markdown)

    def test_authentication_failure_is_partial_and_never_claims_clean(self) -> None:
        with _scenario() as scenario:
            fake = FakeRunner()
            fake.add(
                ["gh", "auth", "status"],
                returncode=1,
                stderr="not authenticated",
            )

            status, markdown, payload = _run_cli(
                scenario,
                fake,
                repos=[_repo(scenario.canonical_path)],
            )

        self.assertEqual(status, 3)
        self.assertEqual(fake.calls, [["gh", "auth", "status"]])
        self.assertIn("Evidence coverage: **incomplete**", markdown)
        self.assertIn("GitHub: incomplete; authenticated=no; 0 repositories", markdown)
        self.assertIn("Inventory evidence is incomplete", markdown)
        self.assertNotIn("No forgotten local work or PR follow-ups found", markdown)
        self.assertNotIn("Inventory reconciliation completed with no mismatches", markdown)
        self.assertFalse(payload["reconciliation"]["complete"])
        self.assertFalse(payload["reconciliation"]["github"]["authenticated"])
        self.assertFalse(payload["reconciliation"]["github"]["owners"][0]["complete"])
        self.assertEqual(payload["reconciliation"]["rows"][0]["issues"], [])
        self.assertEqual(payload["automation_decisions"], [])
        self.assertEqual(payload["execution_results"], [])

    def test_invalid_registry_keeps_provenance_and_skips_auth_without_a_clean_claim(self) -> None:
        with _scenario() as scenario:
            scenario.registry_path.write_text(
                json.dumps({"schema_version": 2}),
                encoding="utf-8",
            )
            fake = FakeRunner()

            status, markdown, payload = _run_cli(scenario, fake, repos=[])

        self.assertEqual(status, 3)
        self.assertEqual(fake.calls, [])
        self.assertIn("Registry: incomplete; 0 projects", markdown)
        self.assertIn("project registry owner scope is incomplete", markdown)
        self.assertIn("Inventory evidence is incomplete", markdown)
        self.assertNotIn("No forgotten local work or PR follow-ups found", markdown)
        self.assertFalse(payload["reconciliation"]["registry"]["complete"])
        self.assertEqual(payload["reconciliation"]["rows"], [])

    def test_owner_inventory_failure_preserves_local_and_owner_source_evidence(self) -> None:
        with _scenario() as scenario:
            fake = FakeRunner()
            fake.add(["gh", "auth", "status"])
            fake.add(_inventory_command(), returncode=1, stderr="inventory denied")
            repo = _repo(
                scenario.canonical_path,
                linked_worktrees=[
                    LinkedWorktreeState(path="/tmp/alpha-worktree", branch="codex/work")
                ],
            )

            status, markdown, payload = _run_cli(scenario, fake, repos=[repo])

        self.assertEqual(status, 3)
        self.assertIn("GitHub owners: owner (incomplete)", markdown)
        self.assertIn("Local: complete; 1 repositories; 1 linked worktrees", markdown)
        self.assertIn("Inventory evidence is incomplete", markdown)
        reconciliation = payload["reconciliation"]
        self.assertEqual(len(reconciliation["local"]["repositories"]), 1)
        self.assertEqual(
            reconciliation["local"]["repositories"][0]["worktree_paths"],
            ["/tmp/alpha-worktree"],
        )
        self.assertEqual(reconciliation["github"]["owners"][0]["repositories"], [])
        self.assertTrue(reconciliation["github"]["owners"][0]["errors"])
        self.assertNotIn("No forgotten local work or PR follow-ups found", markdown)
        self.assertNotIn("Inventory reconciliation completed with no mismatches", markdown)

    def test_local_discovery_failure_preserves_observed_rows_and_returns_three(self) -> None:
        with _scenario() as scenario:
            fake = FakeRunner()
            fake.add(["gh", "auth", "status"])
            fake.add(_inventory_command(), stdout=json.dumps([_github_repo()]))
            repo = _repo(
                scenario.canonical_path,
                linked_worktrees=[
                    LinkedWorktreeState(path="/tmp/observed-worktree", branch="codex/work")
                ],
            )

            status, markdown, payload = _run_cli(
                scenario,
                fake,
                repos=[repo],
                discovery_error="could not inspect scan path /private/repos",
            )

        self.assertEqual(status, 3)
        self.assertIn("Local: incomplete; 1 repositories; 1 linked worktrees", markdown)
        self.assertIn("Inventory evidence is incomplete", markdown)
        self.assertNotIn("No forgotten local work or PR follow-ups found", markdown)
        self.assertNotIn("Inventory reconciliation completed with no mismatches", markdown)
        reconciliation = payload["reconciliation"]
        self.assertFalse(reconciliation["local"]["complete"])
        self.assertEqual(
            reconciliation["local"]["errors"],
            ["could not inspect scan path /private/repos"],
        )
        self.assertEqual(reconciliation["rows"][0]["local_paths"], [scenario.canonical_path])

    def test_fetch_failure_makes_reconciliation_incomplete_and_returns_three(self) -> None:
        with _scenario() as scenario:
            fake = FakeRunner()
            fake.add(["gh", "auth", "status"])
            fake.add(_inventory_command(), stdout=json.dumps([_github_repo()]))
            repo = _repo(scenario.canonical_path)
            repo.fetch_prune_status = "network unavailable"

            status, markdown, payload = _run_cli(scenario, fake, repos=[repo])

        self.assertEqual(status, 3)
        self.assertIn("Local: incomplete", markdown)
        self.assertFalse(payload["reconciliation"]["local"]["complete"])
        self.assertIn(
            "fetch --prune failed: network unavailable",
            payload["reconciliation"]["local"]["errors"][0],
        )

    def test_reconciliation_findings_never_become_decisions_or_execution_results(self) -> None:
        with _scenario() as scenario:
            fake = FakeRunner()
            fake.add(["gh", "auth", "status"])
            fake.add(_inventory_command(), stdout=json.dumps([_github_repo()]))

            status, markdown, payload = _run_cli(
                scenario,
                fake,
                repos=[
                    _repo(
                        scenario.canonical_path,
                        branches=[
                            BranchState(
                                name="merged-work",
                                merged_to_default=True,
                                unique_commit_count=0,
                            )
                        ],
                    )
                ],
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            [finding["category"] for finding in payload["findings"]],
            ["merged-local-branch"],
        )
        self.assertEqual(payload["automation_decisions"], [])
        self.assertEqual(payload["execution_results"], [])
        self.assertNotIn("## Automation Decisions", markdown)
        self.assertNotIn("## Execution Results", markdown)
        self.assertFalse(fake.mutating_calls())

    def test_reconciliation_explicitly_adds_canonical_repo_beyond_scan_depth(self) -> None:
        with _scenario() as scenario:
            (Path(scenario.canonical_path) / ".git").mkdir(parents=True)
            scenario.config_path.write_text(
                "\n".join(
                    [
                        "[scanner]",
                        f'scan_roots = ["{scenario.root}"]',
                        "max_depth = 0",
                        "fetch_prune = true",
                        "",
                        "[inventory]",
                        f'project_registry_path = "{scenario.registry_path}"',
                        "",
                        "[actions]",
                        "apply_categories = []",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            fake = FakeRunner()
            fake.add(["gh", "auth", "status"])
            fake.add(_inventory_command(), stdout=json.dumps([_github_repo()]))
            observed_configs = []

            status, _markdown, payload = _run_cli(
                scenario,
                fake,
                repos=[_repo(scenario.canonical_path)],
                observed_configs=observed_configs,
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            observed_configs[0].repos,
            [Path(scenario.canonical_path).resolve()],
        )
        self.assertTrue(payload["reconciliation"]["local"]["complete"])

    def test_missing_derived_canonical_repo_remains_an_actionable_finding(self) -> None:
        with _scenario() as scenario:
            scenario.config_path.write_text(
                "\n".join(
                    [
                        "[scanner]",
                        f'scan_roots = ["{scenario.root}"]',
                        "max_depth = 0",
                        "fetch_prune = true",
                        "",
                        "[inventory]",
                        f'project_registry_path = "{scenario.registry_path}"',
                        "",
                        "[actions]",
                        "apply_categories = []",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            fake = FakeRunner()
            fake.add(["gh", "auth", "status"])
            fake.add(_inventory_command(), stdout=json.dumps([_github_repo()]))
            observed_configs = []

            status, _markdown, payload = _run_cli(
                scenario,
                fake,
                repos=[],
                observed_configs=observed_configs,
            )

        self.assertEqual(status, 0)
        self.assertEqual(observed_configs[0].repos, [])
        self.assertTrue(payload["reconciliation"]["local"]["complete"])
        self.assertIn(
            "missing-canonical-checkout",
            {
                issue["category"]
                for issue in payload["reconciliation"]["rows"][0]["issues"]
            },
        )

    def test_reconciliation_rejects_action_modes_before_scanning(self) -> None:
        cases = (
            (
                ["--apply"],
                "inventory reconciliation is evidence-only and cannot execute actions",
            ),
            (
                ["--dry-run"],
                "inventory reconciliation is evidence-only and cannot execute actions",
            ),
            (
                ["--apply-categories", "merge-green-pr"],
                "inventory reconciliation cannot accept execution category filters",
            ),
            (
                ["--touched-maintainer-plan"],
                "inventory reconciliation cannot emit touched-maintainer action plans",
            ),
            (
                ["--no-fetch"],
                "inventory reconciliation requires live fetch freshness checks",
            ),
        )

        with _scenario() as scenario:
            for extra_args, expected_error in cases:
                with self.subTest(extra_args=extra_args):
                    fake = FakeRunner()
                    stderr = StringIO()
                    with mock.patch(
                        "git_janitor.cli.discover_repos",
                        side_effect=AssertionError("rejected mode must not scan"),
                    ) as discover, mock.patch("sys.stderr", stderr):
                        with self.assertRaises(SystemExit) as raised:
                            cli.main(
                                [
                                    "--config",
                                    str(scenario.config_path),
                                    "--reconcile-inventory",
                                    *extra_args,
                                ],
                                runner=fake,
                                clock=_fixed_clock,
                            )

                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn(expected_error, stderr.getvalue())
                    discover.assert_not_called()
                    self.assertEqual(fake.calls, [])

    def test_reconciliation_rejects_fetch_disabled_config_before_scanning(self) -> None:
        with _scenario() as scenario:
            contents = scenario.config_path.read_text(encoding="utf-8")
            scenario.config_path.write_text(
                contents.replace("fetch_prune = true", "fetch_prune = false"),
                encoding="utf-8",
            )
            stderr = StringIO()
            with mock.patch(
                "git_janitor.cli.discover_repos",
                side_effect=AssertionError("stale mode must not scan"),
            ) as discover, mock.patch("sys.stderr", stderr):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(
                        [
                            "--config",
                            str(scenario.config_path),
                            "--reconcile-inventory",
                        ],
                        clock=_fixed_clock,
                    )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(
            "inventory reconciliation requires live fetch freshness checks",
            stderr.getvalue(),
        )
        discover.assert_not_called()


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

    def mutating_calls(self) -> list[list[str]]:
        mutating_prefixes = (
            ["git", "fetch"],
            ["git", "branch", "-d"],
            ["git", "pull"],
            ["gh", "pr", "merge"],
            ["gh", "pr", "ready"],
        )
        return [
            call
            for call in self.calls
            if any(call[: len(prefix)] == prefix for prefix in mutating_prefixes)
        ]


@contextmanager
def _scenario():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        registry_path = root / "projects.json"
        code_root = root / "Code"
        canonical_path = str(code_root / "Alpha")
        registry_path.write_text(
            json.dumps(_registry_payload(str(code_root))),
            encoding="utf-8",
        )
        config_path = root / "config.toml"
        config_path.write_text(
            "\n".join(
                [
                    "[scanner]",
                    "scan_roots = []",
                    f'repos = ["{canonical_path}"]',
                    "fetch_prune = true",
                    "",
                    "[inventory]",
                    f'project_registry_path = "{registry_path}"',
                    "",
                    "[actions]",
                    "apply_categories = []",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        yield Scenario(
            root=root,
            config_path=config_path,
            registry_path=registry_path,
            canonical_path=canonical_path,
        )


class Scenario:
    def __init__(
        self,
        *,
        root: Path,
        config_path: Path,
        registry_path: Path,
        canonical_path: str,
    ) -> None:
        self.root = root
        self.config_path = config_path
        self.registry_path = registry_path
        self.canonical_path = canonical_path


def _run_cli(
    scenario: Scenario,
    runner: FakeRunner,
    *,
    repos: list[RepoState],
    discovery_error: str | None = None,
    extra_args: list[str] | None = None,
    observed_configs: list | None = None,
) -> tuple[int, str, dict]:
    output = scenario.root / "report.json"
    stdout = StringIO()

    def discover(_config, *, errors=None):
        if observed_configs is not None:
            observed_configs.append(_config)
        if discovery_error and errors is not None:
            errors.append(discovery_error)
        return [Path(repo.path) for repo in repos]

    with mock.patch("git_janitor.cli.discover_repos", side_effect=discover), mock.patch(
        "git_janitor.cli.scan_repo",
        side_effect=repos,
    ), mock.patch(
        "git_janitor.cli.list_pull_requests",
        return_value=([], []),
    ), mock.patch(
        "git_janitor.cli.gh_available",
        side_effect=AssertionError("reconciliation must use the injected inventory runner"),
    ), mock.patch("sys.stdout", stdout):
        status = cli.main(
            [
                "--config",
                str(scenario.config_path),
                "--reconcile-inventory",
                "--json-out",
                str(output),
                *(extra_args or []),
            ],
            runner=runner,
            clock=_fixed_clock,
        )

    return status, stdout.getvalue(), json.loads(output.read_text(encoding="utf-8"))


def _registry_payload(code_root: str) -> dict:
    return {
        "schema_version": 1,
        "code_root": code_root,
        "default_validation": {
            "gate": "make check",
            "full_gate": "",
            "narrow": "",
            "notes": "",
        },
        "projects": {
            "alpha": {
                "aliases": [],
                "archive_path": None,
                "display_name": "Alpha",
                "docs_gardener": False,
                "github_repo": "owner/alpha",
                "kind": "repository",
                "layer": "Tools",
                "lifecycle_ref": None,
                "local_path": "Alpha",
                "notes": "Alpha notes.",
                "propagation_targets": [],
                "replacement": [],
                "status": "active",
                "status_since": None,
                "truth_store": "repository",
                "validation": {
                    "gate": "make check",
                    "full_gate": "",
                    "narrow": "",
                    "notes": "",
                },
            }
        },
    }


def _repo(
    path: str,
    *,
    branches: list[BranchState] | None = None,
    linked_worktrees: list[LinkedWorktreeState] | None = None,
) -> RepoState:
    return RepoState(
        path=path,
        name="Alpha",
        current_branch="main",
        default_branch="main",
        default_ref="origin/main",
        remote_url="https://github.com/owner/alpha.git",
        github_repo="owner/alpha",
        fetch_prune_status="ok",
        branches=branches or [],
        linked_worktrees=linked_worktrees or [],
    )


def _inventory_command() -> list[str]:
    return [
        "gh",
        "repo",
        "list",
        "owner",
        "--limit",
        "201",
        "--json",
        "name,nameWithOwner,isArchived,visibility,defaultBranchRef",
    ]


def _github_repo() -> dict:
    return {
        "name": "alpha",
        "nameWithOwner": "owner/alpha",
        "isArchived": False,
        "visibility": "PRIVATE",
        "defaultBranchRef": {"name": "main"},
    }


def _fixed_clock() -> datetime:
    return datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main()
