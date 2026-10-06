from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from git_janitor import cli
from git_janitor.models import BranchState, PullRequestState, RepoState


FIXED_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


class SavedScanPlanCliTests(unittest.TestCase):
    def test_plan_narrows_projects_filters_findings_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            code_root = root / "Code"
            alpha_path = code_root / "alpha"
            beta_path = code_root / "beta"
            registry_path = _write_registry(root, code_root, ("alpha", "beta"))
            json_path = root / "report.json"
            config_path = _write_config(
                root,
                registry_path=registry_path,
                projects=("alpha",),
                categories=("merged-local-branch",),
            )
            alpha = _repo(
                alpha_path,
                errors=["status unavailable"],
                fetch_prune_status="network unavailable",
                dirty_files=["scratch.txt"],
                branches=[
                    BranchState(
                        name="merged-docs",
                        upstream="origin/merged-docs",
                        merged_to_default=True,
                        unique_commit_count=0,
                    )
                ],
            )
            stale_pr = PullRequestState(
                repo="owner/alpha",
                number=7,
                title="Stale PR",
                url="https://github.com/owner/alpha/pull/7",
                head_ref="stale",
                base_ref="main",
                is_draft=False,
                merge_state="CLEAN",
                review_decision=None,
                check_status="stale",
                errors=["file metadata unavailable"],
            )
            stdout = StringIO()

            with (
                mock.patch(
                    "git_janitor.cli.discover_repos",
                    return_value=[alpha_path, beta_path],
                ) as discover,
                mock.patch("git_janitor.cli.scan_repo", return_value=alpha) as scan,
                mock.patch("git_janitor.cli.gh_available", return_value=True) as auth,
                mock.patch(
                    "git_janitor.cli.list_pull_requests",
                    return_value=([stale_pr], []),
                ) as pull_requests,
                mock.patch("sys.stdout", stdout),
            ):
                result = cli.main(
                    [
                        "--config",
                        str(config_path),
                        "--plan",
                        "review",
                        "--json-out",
                        str(json_path),
                    ],
                    clock=_fixed_clock,
                )

            self.assertEqual(result, 3)
            discover.assert_called_once()
            scan.assert_called_once()
            self.assertEqual(scan.call_args.args[0], alpha_path.resolve())
            auth.assert_called_once_with(timeout=10)
            pull_requests.assert_called_once()
            self.assertEqual(pull_requests.call_args.args[0], "owner/alpha")

            markdown = stdout.getvalue()
            tick = chr(96)
            self.assertIn("## Saved Scan Plan", markdown)
            self.assertIn(f"- Name: {tick}review{tick}", markdown)
            self.assertIn(f"- Projects: {tick}alpha{tick}", markdown)
            self.assertIn(
                f"- Finding categories: {tick}merged-local-branch{tick}",
                markdown,
            )
            self.assertNotIn(f"Category: {tick}merged-local-branch{tick}", markdown)
            self.assertIn(f"Category: {tick}scanner-error{tick}", markdown)
            self.assertIn(f"Category: {tick}fetch-prune-failed{tick}", markdown)
            self.assertIn(f"Category: {tick}pr-inspection-warning{tick}", markdown)
            self.assertNotIn(f"Category: {tick}pr-stale-ci{tick}", markdown)
            self.assertNotIn(f"Category: {tick}dirty-worktree{tick}", markdown)

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["scan_plan"],
                {
                    "description": "Bounded review.",
                    "finding_categories": ["merged-local-branch"],
                    "name": "review",
                    "project_ids": ["alpha"],
                },
            )
            self.assertEqual([repo["path"] for repo in payload["repos"]], [str(alpha_path)])
            self.assertEqual(
                {finding["category"] for finding in payload["findings"]},
                {
                    "fetch-prune-failed",
                    "pr-inspection-warning",
                    "scanner-error",
                },
            )
            self.assertEqual(payload["automation_decisions"], [])
            self.assertEqual(payload["execution_results"], [])

    def test_unknown_missing_and_outside_scope_plans_fail_before_repo_scan(self) -> None:
        cases = (
            ("unknown plan", ("alpha",), ("alpha",), "missing", False),
            ("missing project", ("missing",), ("alpha",), "review", True),
            ("outside scope", ("alpha",), ("beta",), "review", True),
        )
        for name, plan_projects, discovered_projects, requested_plan, expects_discovery in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                code_root = root / "Code"
                registry_path = _write_registry(root, code_root, ("alpha", "beta"))
                config_path = _write_config(
                    root,
                    registry_path=registry_path,
                    projects=plan_projects,
                )
                discovered = [code_root / project for project in discovered_projects]

                with (
                    mock.patch(
                        "git_janitor.cli.discover_repos",
                        return_value=discovered,
                    ) as discover,
                    mock.patch("git_janitor.cli.scan_repo") as scan,
                    mock.patch("git_janitor.cli.execute_decisions") as execute,
                    mock.patch("sys.stderr", StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    cli.main(
                        ["--config", str(config_path), "--plan", requested_plan],
                        clock=_fixed_clock,
                    )

                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(discover.called, expects_discovery)
                scan.assert_not_called()
                execute.assert_not_called()

    def test_plan_rejects_unsafe_modes_and_stale_config_before_execution(self) -> None:
        cases = (
            ("apply", True, ["--apply"]),
            ("dry run", True, ["--dry-run"]),
            ("no fetch", True, ["--no-fetch"]),
            ("reconciliation", True, ["--reconcile-inventory"]),
            ("execution category", True, ["--apply-categories", "merge-green-pr"]),
            ("touched maintainer", True, ["--touched-maintainer-plan"]),
            ("config disables fetch", False, []),
        )
        for name, fetch_prune, extra_args in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                code_root = root / "Code"
                registry_path = _write_registry(root, code_root, ("alpha",))
                ledger_path = root / "execution-audit.jsonl"
                config_path = _write_config(
                    root,
                    registry_path=registry_path,
                    projects=("alpha",),
                    fetch_prune=fetch_prune,
                    ledger_path=ledger_path,
                )

                with (
                    mock.patch("git_janitor.cli.load_registry") as load_registry,
                    mock.patch("git_janitor.cli.discover_repos") as discover,
                    mock.patch("git_janitor.cli.scan_repo") as scan,
                    mock.patch("git_janitor.cli.execute_decisions") as execute,
                    mock.patch("sys.stderr", StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    cli.main(
                        ["--config", str(config_path), "--plan", "review", *extra_args],
                        clock=_fixed_clock,
                    )

                self.assertEqual(raised.exception.code, 2)
                load_registry.assert_not_called()
                discover.assert_not_called()
                scan.assert_not_called()
                execute.assert_not_called()
                self.assertFalse(ledger_path.exists())

    def test_plan_still_checks_live_github_auth_and_never_reports_false_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            code_root = root / "Code"
            alpha_path = code_root / "alpha"
            registry_path = _write_registry(root, code_root, ("alpha",))
            config_path = _write_config(
                root,
                registry_path=registry_path,
                projects=("alpha",),
            )
            stdout = StringIO()

            with (
                mock.patch(
                    "git_janitor.cli.discover_repos",
                    return_value=[alpha_path],
                ),
                mock.patch("git_janitor.cli.scan_repo", return_value=_repo(alpha_path)),
                mock.patch("git_janitor.cli.gh_available", return_value=False) as auth,
                mock.patch("git_janitor.cli.list_pull_requests") as pull_requests,
                mock.patch("sys.stdout", stdout),
            ):
                result = cli.main(
                    ["--config", str(config_path), "--plan", "review"],
                    clock=_fixed_clock,
                )

            self.assertEqual(result, 3)
            auth.assert_called_once_with(timeout=10)
            pull_requests.assert_not_called()
            markdown = stdout.getvalue()
            self.assertIn("## Scanner Errors", markdown)
            self.assertIn("gh is unavailable or not authenticated", markdown)
            self.assertIn(
                "Scanner evidence is incomplete. No clean audit conclusion is available.",
                markdown,
            )
            self.assertNotIn("No forgotten local work or PR follow-ups found.", markdown)
            self.assertNotIn("No findings matched saved scan plan", markdown)

    def test_list_plans_prints_without_scanning_or_loading_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry_path = root / "unused-projects.json"
            config_path = _write_config(
                root,
                registry_path=registry_path,
                projects=("alpha",),
                additional_plan=True,
            )
            stdout = StringIO()

            with (
                mock.patch("git_janitor.cli.load_registry") as load_registry,
                mock.patch("git_janitor.cli.discover_repos") as discover,
                mock.patch("git_janitor.cli.scan_repo") as scan,
                mock.patch("git_janitor.cli.gh_available") as auth,
                mock.patch("sys.stdout", stdout),
            ):
                result = cli.main(["--config", str(config_path), "--list-plans"])

            self.assertEqual(result, 0)
            load_registry.assert_not_called()
            discover.assert_not_called()
            scan.assert_not_called()
            auth.assert_not_called()
            self.assertEqual(
                stdout.getvalue().splitlines(),
                [
                    "another",
                    "  projects: alpha",
                    "  findings: scanner-error",
                    "review — Bounded review.",
                    "  projects: alpha",
                    "  findings: merged-local-branch",
                ],
            )


def _write_config(
    root: Path,
    *,
    registry_path: Path,
    projects: tuple[str, ...],
    categories: tuple[str, ...] = ("merged-local-branch",),
    fetch_prune: bool = True,
    ledger_path: Path | None = None,
    additional_plan: bool = False,
) -> Path:
    config_path = root / "config.toml"
    ledger_path = ledger_path or root / "execution-audit.jsonl"
    project_values = ", ".join(json.dumps(value) for value in projects)
    category_values = ", ".join(json.dumps(value) for value in categories)
    additional = (
        """
        [scan_plans.another]
        project_ids = ["alpha"]
        finding_categories = ["scanner-error"]
        """
        if additional_plan
        else ""
    )
    config_path.write_text(
        f"""
        [scanner]
        scan_roots = []
        repos = []
        fetch_prune = {str(fetch_prune).lower()}
        high_risk_patterns = ["workflow"]

        [inventory]
        project_registry_path = {json.dumps(str(registry_path))}

        [actions]
        auto_merge_green_prs = true
        auto_delete_merged_branches = true
        auto_mark_drafts_ready = true
        auto_fast_forward_default_branch = true
        apply_categories = [
          "merge-green-pr",
          "delete-merged-branch",
          "mark-draft-ready",
          "fast-forward-default-branch",
        ]
        ledger_path = {json.dumps(str(ledger_path))}

        [scan_plans.review]
        description = "Bounded review."
        project_ids = [{project_values}]
        finding_categories = [{category_values}]
        {additional}
        """,
        encoding="utf-8",
    )
    return config_path


def _write_registry(
    root: Path,
    code_root: Path,
    project_ids: tuple[str, ...],
) -> Path:
    registry_path = root / "projects.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "code_root": str(code_root),
                "default_validation": _validation(),
                "projects": {
                    project_id: _registry_project(project_id)
                    for project_id in project_ids
                },
            }
        ),
        encoding="utf-8",
    )
    return registry_path


def _registry_project(project_id: str) -> dict:
    return {
        "aliases": [],
        "archive_path": None,
        "display_name": project_id.title(),
        "docs_gardener": False,
        "github_repo": f"owner/{project_id}",
        "kind": "repository",
        "layer": "Tools",
        "lifecycle_ref": None,
        "local_path": project_id,
        "notes": f"{project_id} fixture.",
        "propagation_targets": [],
        "replacement": [],
        "status": "active",
        "status_since": None,
        "truth_store": "repository",
        "validation": _validation(),
    }


def _validation() -> dict[str, str]:
    return {
        "gate": "make check",
        "full_gate": "",
        "narrow": "",
        "notes": "",
    }


def _repo(
    path: Path,
    *,
    errors: list[str] | None = None,
    fetch_prune_status: str | None = "ok",
    dirty_files: list[str] | None = None,
    branches: list[BranchState] | None = None,
) -> RepoState:
    return RepoState(
        path=str(path),
        name=path.name,
        current_branch="main",
        default_branch="main",
        default_ref="origin/main",
        github_repo=f"owner/{path.name}",
        remote_url=f"https://github.com/owner/{path.name}.git",
        errors=list(errors or []),
        fetch_prune_status=fetch_prune_status,
        dirty_files=list(dirty_files or []),
        branches=list(branches or []),
    )


def _fixed_clock() -> datetime:
    return FIXED_NOW


if __name__ == "__main__":
    unittest.main()
