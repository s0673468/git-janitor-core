from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from git_janitor.config import ScannerConfig
from git_janitor.github import classify_check_rollup, list_open_pull_requests, list_pull_requests
from git_janitor.models import CommandResult


class GithubPullRequestListTests(unittest.TestCase):
    def test_normal_pr_list_filters_by_config_author_and_enriches_files(self) -> None:
        calls: list[list[str]] = []

        def fake_run_command(args: list[str], **kwargs) -> CommandResult:
            del kwargs
            calls.append(args)
            if args[:3] == ["gh", "pr", "list"]:
                return _command_result(args, _pr_list_json())
            if args[:3] == ["gh", "pr", "view"]:
                return _command_result(args, json.dumps({"files": [{"path": "README.md"}]}))
            raise AssertionError(f"unexpected command: {args}")

        with patch("git_janitor.github.run_command", side_effect=fake_run_command):
            prs, errors = list_pull_requests(
                "owner/repo",
                ScannerConfig(github_author="@me"),
                cwd=Path("/tmp/repo"),
            )

        self.assertEqual(errors, [])
        self.assertIn("--author", calls[0])
        self.assertIn("@me", calls[0])
        self.assertEqual(prs[0].changed_files, ["README.md"])
        self.assertEqual(len(calls), 2)

    def test_hard_stop_pr_list_is_unfiltered_and_does_not_enrich_files(self) -> None:
        calls: list[list[str]] = []

        def fake_run_command(args: list[str], **kwargs) -> CommandResult:
            del kwargs
            calls.append(args)
            if args[:3] == ["gh", "pr", "list"]:
                return _command_result(args, _pr_list_json())
            raise AssertionError(f"unexpected command: {args}")

        with patch("git_janitor.github.run_command", side_effect=fake_run_command):
            prs, errors = list_open_pull_requests(
                "owner/repo",
                ScannerConfig(github_author="@me"),
                cwd=Path("/tmp/repo"),
            )

        self.assertEqual(errors, [])
        self.assertNotIn("--author", calls[0])
        self.assertEqual(prs[0].head_ref, "codex/demo")
        self.assertEqual(prs[0].changed_files, [])
        self.assertEqual(len(calls), 1)

    def test_unfiltered_hard_stop_keeps_dependency_bot_prs_visible(self) -> None:
        raw_prs = json.loads(_pr_list_json())
        raw_prs.extend(
            [
                {
                    **raw_prs[0],
                    "number": 13,
                    "title": "Bump ruff from 0.16.3 to 0.16.4",
                    "headRefName": "dependabot/pip/ruff-0.16.4",
                    "author": {"login": "dependabot[bot]"},
                },
                {
                    **raw_prs[0],
                    "number": 14,
                    "title": "Update dependency pytest to v9",
                    "headRefName": "renovate/pytest-9.x",
                    "author": {"login": "renovate[bot]"},
                },
            ]
        )

        def fake_run_command(args: list[str], **kwargs) -> CommandResult:
            del kwargs
            if args[:3] == ["gh", "pr", "list"]:
                return _command_result(args, json.dumps(raw_prs))
            raise AssertionError(f"unexpected command: {args}")

        with patch("git_janitor.github.run_command", side_effect=fake_run_command):
            unfiltered, unfiltered_errors = list_open_pull_requests(
                "owner/repo",
                ScannerConfig(github_author="@me"),
                cwd=Path("/tmp/repo"),
            )

        self.assertEqual(unfiltered_errors, [])
        self.assertEqual([pr.number for pr in unfiltered], [12, 13, 14])

    def test_authored_scan_excludes_dependency_bot_owned_prs(self) -> None:
        raw_prs = json.loads(_pr_list_json())
        raw_prs.extend(
            [
                {
                    **raw_prs[0],
                    "number": 13,
                    "title": "Bump ruff from 0.16.3 to 0.16.4",
                    "headRefName": "dependabot/pip/ruff-0.16.4",
                    "author": {"login": "dependabot[bot]"},
                },
                {
                    **raw_prs[0],
                    "number": 14,
                    "title": "User-owned branch with a bot-like name",
                    "headRefName": "renovate/manual-cleanup",
                    "author": {"login": "example-author"},
                },
            ]
        )

        def fake_run_command(args: list[str], **kwargs) -> CommandResult:
            del kwargs
            if args[:3] == ["gh", "pr", "list"]:
                return _command_result(args, json.dumps(raw_prs))
            if args[:3] == ["gh", "pr", "view"]:
                return _command_result(args, json.dumps({"files": [], "changedFiles": 0}))
            raise AssertionError(f"unexpected command: {args}")

        with patch("git_janitor.github.run_command", side_effect=fake_run_command):
            authored, authored_errors = list_pull_requests(
                "owner/repo",
                ScannerConfig(github_author=""),
                cwd=Path("/tmp/repo"),
            )

        self.assertEqual(authored_errors, [])
        self.assertEqual([pr.number for pr in authored], [12, 14])

    def test_truncated_pr_file_list_is_recorded_as_an_inspection_error(self) -> None:
        # `gh pr view --json files` caps at 100 entries. Without comparing
        # changedFiles, a truncated list is indistinguishable from a complete
        # one and high-risk classification silently runs on a partial set.
        def fake_run_command(args: list[str], **kwargs) -> CommandResult:
            del kwargs
            if args[:3] == ["gh", "pr", "list"]:
                return _command_result(args, _pr_list_json())
            if args[:3] == ["gh", "pr", "view"]:
                return _command_result(
                    args,
                    json.dumps(
                        {
                            "files": [{"path": f"file{index}.py"} for index in range(100)],
                            "changedFiles": 137,
                        }
                    ),
                )
            raise AssertionError(f"unexpected command: {args}")

        with patch("git_janitor.github.run_command", side_effect=fake_run_command):
            prs, _errors = list_pull_requests(
                "owner/repo",
                ScannerConfig(github_author="@me"),
                cwd=Path("/tmp/repo"),
            )

        self.assertEqual(len(prs[0].changed_files), 100)
        self.assertTrue(
            any("137" in error for error in prs[0].errors),
            f"expected a truncation error, got {prs[0].errors}",
        )

    def test_complete_pr_file_list_records_no_error(self) -> None:
        def fake_run_command(args: list[str], **kwargs) -> CommandResult:
            del kwargs
            if args[:3] == ["gh", "pr", "list"]:
                return _command_result(args, _pr_list_json())
            if args[:3] == ["gh", "pr", "view"]:
                return _command_result(
                    args,
                    json.dumps({"files": [{"path": "README.md"}], "changedFiles": 1}),
                )
            raise AssertionError(f"unexpected command: {args}")

        with patch("git_janitor.github.run_command", side_effect=fake_run_command):
            prs, _errors = list_pull_requests(
                "owner/repo",
                ScannerConfig(github_author="@me"),
                cwd=Path("/tmp/repo"),
            )

        self.assertEqual(prs[0].errors, [])

    def test_stale_check_rollup_is_explicit(self) -> None:
        rollup = [{"conclusion": "STALE", "status": "COMPLETED"}]

        self.assertEqual(classify_check_rollup(rollup), "stale")

    def test_failing_legacy_status_is_not_masked_by_a_passing_check_run(self) -> None:
        # A StatusContext reports `state`, not `conclusion`. Matching states
        # only against PENDING_STATES let a hard FAILURE fall through to the
        # success branch as long as every CheckRun passed.
        rollup = [
            {"__typename": "CheckRun", "conclusion": "SUCCESS", "status": "COMPLETED"},
            {"__typename": "StatusContext", "state": "FAILURE", "context": "ci/external"},
        ]

        self.assertEqual(classify_check_rollup(rollup), "failure")

    def test_errored_legacy_status_is_a_failure(self) -> None:
        rollup = [{"__typename": "StatusContext", "state": "ERROR", "context": "ci/external"}]

        self.assertEqual(classify_check_rollup(rollup), "failure")

    def test_check_run_statuses_are_not_read_as_failures(self) -> None:
        rollup = [
            {"__typename": "CheckRun", "conclusion": "SUCCESS", "status": "COMPLETED"},
            {"__typename": "CheckRun", "conclusion": "SKIPPED", "status": "COMPLETED"},
        ]

        self.assertEqual(classify_check_rollup(rollup), "success")


def _pr_list_json() -> str:
    return json.dumps(
        [
            {
                "number": 12,
                "title": "Demo",
                "url": "https://github.com/owner/repo/pull/12",
                "headRefName": "codex/demo",
                "baseRefName": "main",
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "reviewDecision": None,
                "statusCheckRollup": [],
                "updatedAt": "2026-06-26T10:00:00Z",
                "author": {"login": "example-author"},
            }
        ]
    )


def _command_result(args: list[str], stdout: str) -> CommandResult:
    return CommandResult(args=args, returncode=0, stdout=stdout, stderr="")


if __name__ == "__main__":
    unittest.main()
