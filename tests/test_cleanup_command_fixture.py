"""Keep the shared cleanup command fixture explicit and fail closed."""
from __future__ import annotations

import unittest

from git_janitor.models import CommandResult
from git_janitor.safe_delete import evaluate_delete
from test_cli import FakeRunner as CliRunner, REPO_PATH as CLI_REPO_PATH, _add_branch_preflight
from test_execute import (
    FakeRunner as ExecutorRunner, REPO_PATH as EXECUTOR_REPO_PATH, _add_delete_branch_verify,
)
from test_repo_policy_tools import REPO_PATH, TEST_HEAD_SHA, _runner_for_cleanup_scenario


class CleanupCommandFixtureTests(unittest.TestCase):
    def test_cli_and_executor_fixtures_return_hashes_for_exact_local_refs(self):
        cli = CliRunner()
        _add_branch_preflight(cli)
        executor = ExecutorRunner()
        _add_delete_branch_verify(executor, current_branch="main")
        for fake, repo_path in ((cli, CLI_REPO_PATH), (executor, EXECUTOR_REPO_PATH)):
            with self.subTest(fixture=type(fake).__module__):
                result = fake(
                    ["git", "show-ref", "--verify", "--hash", "refs/heads/old-docs"],
                    cwd=repo_path,
                )
                self.assertEqual(result.returncode, 0)
                self.assertRegex(result.stdout, r"\A[0-9a-f]{40}\Z")
                self.assertEqual(result.stderr, "")
                with self.assertRaisesRegex(AssertionError, "unexpected command"):
                    fake(["git", "show-ref", "--verify", "--hash", "refs/tags/old-docs"],
                         cwd=repo_path)

    def test_show_ref_returns_the_pinned_head_only_for_the_exact_local_ref(self):
        fake = _runner_for_cleanup_scenario("squash_tree_equivalent", "codex/squashed")
        command = ["git", "show-ref", "--verify", "--hash", "refs/heads/codex/squashed"]
        result = fake(command, REPO_PATH, 30)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "branch-sha")
        self.assertEqual(result.stderr, "")
        for ref in ("codex/squashed", "refs/tags/codex/squashed", "refs/heads/missing"):
            with self.subTest(ref=ref), self.assertRaisesRegex(AssertionError, "unexpected command"):
                fake([*command[:-1], ref], REPO_PATH, 30)

    def test_missing_local_ref_does_not_pass_the_branch_exists_proof(self):
        fake = _runner_for_cleanup_scenario("ancestor_pass", "old-docs")
        command = ["git", "show-ref", "--verify", "--hash", "refs/heads/old-docs"]
        fake.responses[tuple(command)] = [CommandResult(command, 1, "", "missing local ref")]

        report = evaluate_delete("old-docs", repo_path=REPO_PATH, runner=fake)

        self.assertFalse(report.passed)
        self.assertEqual([p.name for p in report.proofs if not p.passed], ["branch_exists"])

    def test_saved_work_and_submodules_still_block_cleanup(self):
        for command, output in (
            (["git", "stash", "list", "--format=%H"], TEST_HEAD_SHA),
            (["git", "ls-files", "--stage", "-z"], f"160000 {TEST_HEAD_SHA} 0\tmodule\0"),
        ):
            with self.subTest(command=command):
                fake = _runner_for_cleanup_scenario("ancestor_pass", "old-docs")
                fake.responses[tuple(command)] = [CommandResult(command, 0, output, "")]

                report = evaluate_delete("old-docs", repo_path=REPO_PATH, runner=fake)

                self.assertFalse(report.passed)
                self.assertEqual([p.name for p in report.proofs if not p.passed],
                                 ["saved_and_nested_work_preserved"])

    def test_failed_preservation_queries_still_block_cleanup(self):
        for command in (
            ["git", "stash", "list", "--format=%H"],
            ["git", "ls-files", "--stage", "-z"],
        ):
            with self.subTest(command=command):
                fake = _runner_for_cleanup_scenario("ancestor_pass", "old-docs")
                fake.responses[tuple(command)] = [CommandResult(command, 128, "", "query failed")]

                report = evaluate_delete("old-docs", repo_path=REPO_PATH, runner=fake)

                self.assertFalse(report.passed)
                self.assertEqual([p.name for p in report.proofs if not p.passed],
                                 ["saved_and_nested_work_preserved"])

    def test_squash_equivalence_still_requires_merge_reachable_from_default(self):
        fake = _runner_for_cleanup_scenario("squash_tree_equivalent", "codex/squashed")
        command = ["git", "merge-base", "--is-ancestor", "merge-sha", "refs/remotes/origin/main"]
        fake.responses[tuple(command)] = [CommandResult(command, 1, "", "")]

        report = evaluate_delete("codex/squashed", repo_path=REPO_PATH, runner=fake)

        self.assertFalse(report.passed)
        self.assertEqual([p.name for p in report.proofs if not p.passed],
                         ["merge_reachable_from_default"])
