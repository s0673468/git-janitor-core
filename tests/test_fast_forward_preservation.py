"""Fast-forwards preserve synthetic ignored files without scanning build trees."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_janitor.autonomy import AutomationPolicy, decide_touched_repo_candidate
from git_janitor.config import ScannerConfig
from git_janitor.execute import ActionExecutor
from git_janitor.fleet import TouchedRepoMaintainerConfig, build_touched_repo_candidates
from git_janitor.git import run_command, scan_repo
from git_janitor.ledger import AuditLedger
from git_janitor.models import AutomationDecision, CommandResult


class FastForwardPreservationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(prefix="janitor-ff-preservation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.git("init", "-b", "main", str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Preservation Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", "/dev/null")
        (self.repo / ".gitignore").write_text("ignored*\n.venv/\nnode_modules/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "synthetic base")
        self.base = self.git("rev-parse", "HEAD")
        # Remote metadata only: no transport is permitted by the runner below.
        self.git("remote", "add", "origin", "https://github.com/fixture-owner/fixture.git")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git("branch", "--set-upstream-to=origin/main", "main")
        self.config = ScannerConfig(fetch_prune=False)

    def git(self, *args, cwd=None):
        result = run_command(["git", *args], cwd or self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def incoming(self, filename="ignored.txt"):
        self.git("switch", "-c", "incoming")
        target = self.repo / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic upstream\n")
        self.git("add", "-f", filename)
        self.git("commit", "-m", "synthetic incoming change")
        self.target = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.target)
        self.git("switch", "main")

    def runner(self, args, cwd, timeout):
        self.assertEqual(args[0], "git")
        self.assertNotIn(args[1], {"fetch", "pull", "push"})
        return run_command(args, cwd, timeout)

    def execute(self, *, mode="apply", runner=None):
        executor = ActionExecutor(
            policy=AutomationPolicy(auto_fast_forward_default_branch=True,
                                    apply_categories=frozenset({"fast-forward-default-branch"})),
            ledger=AuditLedger(self.root / "audit.jsonl"), mode=mode,
            runner=runner or self.runner, repo_paths={"repo": self.repo}, config=self.config,
        )
        return executor.execute(AutomationDecision(
            disposition="auto-act", category="fast-forward-default-branch",
            title="repo: default branch can be fast-forwarded", reason="synthetic proof",
            recommended_action="synthetic proof",
            evidence=("branch=main", "upstream=origin/main", "behind=1"),
        ))

    def test_scan_blocks_colliding_ignored_file_without_calling_it_dirty(self):
        self.incoming()
        local = self.repo / "ignored.txt"
        local.write_text("synthetic local\n")
        index = (self.repo / ".git/index").read_bytes()
        state = scan_repo(self.repo, self.config)
        self.assertEqual((state.dirty_files, state.untracked_files), ([], []))
        self.assertTrue(any("overwrite" in error for error in state.errors), state.errors)
        candidates = build_touched_repo_candidates(
            [state], [], TouchedRepoMaintainerConfig(active_window_minutes=0),
        )
        self.assertEqual(len(candidates), 1)
        self.assertNotEqual(decide_touched_repo_candidate(candidates[0]).category,
                            "fast-forward-default-branch")
        self.assertEqual(local.read_text(), "synthetic local\n")
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def assert_collision_preserved(self, kind):
        filename = "ignored-parent/child.txt" if kind.startswith("parent-") else (
            "ignored\nfile.txt" if kind == "newline" else "ignored.txt"
        )
        self.incoming(filename)
        local = self.repo / filename
        if kind == "directory":
            local.mkdir()
            local = local / "valuable.txt"
        elif kind.startswith("parent-"):
            if kind == "parent-symlink":
                destination = self.root / "destination"
                destination.mkdir()
                local.parent.symlink_to(destination, target_is_directory=True)
                local = destination / "child.txt"
            else:
                local = local.parent
        contents = "synthetic upstream\n" if kind == "identical" else "synthetic local\n"
        local.write_text(contents)
        result = self.execute()
        self.assertEqual(result.status, "drifted", result)
        self.assertIn("overwrite", result.detail)
        self.assertEqual(local.read_text(), contents)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_executor_refuses_ignored_file(self):
        self.assert_collision_preserved("file")

    def test_executor_refuses_identical_ignored_file(self):
        self.assert_collision_preserved("identical")

    def test_executor_refuses_ignored_directory_replaced_by_file(self):
        self.assert_collision_preserved("directory")

    def test_executor_refuses_ignored_parent_file_replaced_by_directory(self):
        self.assert_collision_preserved("parent-file")

    def test_executor_refuses_ignored_parent_symlink(self):
        self.assert_collision_preserved("parent-symlink")

    def test_executor_refuses_ignored_filename_with_newline(self):
        self.assert_collision_preserved("newline")

    def test_unrelated_large_build_trees_allow_actual_fast_forward(self):
        self.incoming("tracked.txt")
        for directory in (".venv", "node_modules"):
            output = self.repo / directory
            output.mkdir()
            for index in range(300):
                (output / f"file-{index}").write_text("synthetic output\n")
        state = scan_repo(self.repo, self.config)
        self.assertEqual(state.errors, [])
        result = self.execute()
        self.assertEqual(result.status, "applied", result)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)
        self.assertEqual((self.repo / ".venv/file-299").read_text(), "synthetic output\n")
        self.assertEqual((self.repo / "node_modules/file-299").read_text(), "synthetic output\n")

    def test_native_merge_guard_preserves_ignored_file_created_after_preflight(self):
        self.incoming()
        local = self.repo / "ignored.txt"

        def race_runner(args, cwd, timeout):
            if args[:2] == ["git", "merge"]:
                local.write_text("synthetic late local\n")
            return self.runner(args, cwd, timeout)

        result = self.execute(runner=race_runner)
        self.assertEqual(result.status, "failed", result)
        self.assertEqual(local.read_text(), "synthetic late local\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_executor_pins_preflight_target_if_upstream_moves(self):
        self.incoming("tracked.txt")
        expected = self.target

        def race_runner(args, cwd, timeout):
            if args[:2] == ["git", "merge"]:
                self.git("update-ref", "refs/remotes/origin/main", self.base)
            return self.runner(args, cwd, timeout)

        result = self.execute(runner=race_runner)
        self.assertEqual(result.status, "applied", result)
        self.assertEqual(self.git("rev-parse", "HEAD"), expected)

    def test_scan_and_executor_fail_closed_when_collision_inspection_fails(self):
        self.incoming("tracked.txt")

        def failed_runner(args, cwd=None, timeout=45):
            if args[:2] == ["git", "diff"]:
                return CommandResult(args, 124, "", "synthetic timeout")
            return self.runner(args, cwd, timeout)

        with patch("git_janitor.git.run_command", side_effect=failed_runner):
            state = scan_repo(self.repo, self.config)
        self.assertTrue(state.errors)
        result = self.execute(runner=failed_runner)
        self.assertEqual(result.status, "drifted", result)
        self.assertIn("unknown", result.detail)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
