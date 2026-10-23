"""Offline delivery preservation regressions using disposable synthetic repositories."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from git_janitor.models import CommandResult
import test_delivery as delivery_fixtures


class DeliveryPreservationHuntTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        # Reuse only setup/helpers, without inheriting and rerunning existing tests.
        self.fixture = delivery_fixtures.DeliveryTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.git("config", "core.hooksPath", os.devnull)
        self.fixture.git("config", "commit.gpgsign", "false")

    def assert_task_preserved(self):
        fixture = self.fixture
        self.assertTrue(fixture.worktree.is_dir())
        self.assertEqual(fixture.git("rev-parse", "refs/heads/codex/task"), fixture.head)
        self.assertFalse(any(args[:3] == ["git", "worktree", "remove"]
                             for args in fixture.calls))

    def test_assume_unchanged_local_edit_survives_finish(self):
        self.check_hidden_local_edit("--assume-unchanged")

    def test_skip_worktree_local_edit_survives_finish(self):
        self.check_hidden_local_edit("--skip-worktree")

    def test_combined_index_flags_preserve_local_edit(self):
        self.check_hidden_local_edit("--skip-worktree", "--assume-unchanged")

    def check_hidden_local_edit(self, *flags):
        fixture = self.fixture
        for flag in flags:
            fixture.git("update-index", flag, "file.txt", cwd=fixture.worktree)
        local = fixture.worktree / "file.txt"
        local.write_text("synthetic unfinished local work\n")
        self.assertEqual(fixture.git("status", "--porcelain", cwd=fixture.worktree), "")

        self.assertEqual(fixture.finish(), 3)

        self.assert_task_preserved()
        self.assertEqual(local.read_text(), "synthetic unfinished local work\n")

    def test_failed_index_inspection_preserves_task(self):
        fixture = self.fixture
        original = fixture.runner

        def runner(args, cwd, timeout):
            if args == ["git", "ls-files", "-v", "-z"]:
                return CommandResult(args, 124, "", "synthetic index inspection timeout")
            return original(args, cwd, timeout)

        fixture.runner = runner
        self.assertEqual(fixture.finish(), 3)
        self.assert_task_preserved()

    def test_index_flags_are_rechecked_before_worktree_removal(self):
        fixture = self.fixture
        original = fixture.runner
        inspected = 0

        def runner(args, cwd, timeout):
            nonlocal inspected
            if args == ["git", "ls-files", "-v", "-z"]:
                inspected += 1
                if inspected == 2:
                    fixture.git("update-index", "--skip-worktree", "file.txt",
                                cwd=fixture.worktree)
                    (fixture.worktree / "file.txt").write_text("synthetic late edit\n")
            return original(args, cwd, timeout)

        fixture.runner = runner
        self.assertEqual(fixture.finish(), 3)
        self.assertEqual(inspected, 2)
        self.assert_task_preserved()
        self.assertEqual((fixture.worktree / "file.txt").read_text(), "synthetic late edit\n")

    def incoming_addition(self):
        fixture = self.fixture
        fixture.git("switch", "-c", "incoming", fixture.merge)
        (fixture.root / "notes.txt").write_text("synthetic published content\n")
        fixture.git("add", "notes.txt")
        fixture.git("commit", "-m", "synthetic incoming addition")
        target = fixture.git("rev-parse", "HEAD")
        fixture.git("update-ref", "refs/remotes/origin/main", target)
        fixture.git("switch", "main")
        with (fixture.root / ".git/info/exclude").open("a") as stream:
            stream.write("notes.txt\n")
        return target

    def test_default_sync_preserves_ignored_file_in_incoming_tree(self):
        fixture = self.fixture
        self.incoming_addition()
        notes = fixture.root / "notes.txt"
        notes.write_text("synthetic unfinished local notes\n")

        self.assertEqual(fixture.finish(), 3)

        self.assertEqual(notes.read_text(), "synthetic unfinished local notes\n")
        self.assertEqual(fixture.git("rev-parse", "HEAD"), fixture.base)
        self.assert_task_preserved()

    def test_default_sync_preserves_ignored_file_created_after_status(self):
        fixture = self.fixture
        self.incoming_addition()
        original = fixture.runner

        def runner(args, cwd, timeout):
            if args[:3] == ["git", "merge", "--ff-only"]:
                (fixture.root / "notes.txt").write_text("synthetic late local notes\n")
            return original(args, cwd, timeout)

        fixture.runner = runner
        self.assertEqual(fixture.finish(), 3)
        self.assertEqual((fixture.root / "notes.txt").read_text(), "synthetic late local notes\n")
        self.assertEqual(fixture.git("rev-parse", "HEAD"), fixture.base)
        self.assert_task_preserved()

    def test_safe_default_fast_forward_and_task_cleanup_still_succeed(self):
        fixture = self.fixture
        target = self.incoming_addition()

        self.assertEqual(fixture.finish(), 0)

        self.assertEqual(fixture.git("rev-parse", "HEAD"), target)
        self.assertEqual((fixture.root / "notes.txt").read_text(), "synthetic published content\n")
        self.assertFalse(fixture.worktree.exists())
        self.assertTrue(any(args[:4] == ["git", "merge", "--ff-only", "--no-overwrite-ignore"]
                            for args in fixture.calls))

    def test_unrelated_ignored_default_file_survives_sync(self):
        fixture = self.fixture
        target = self.incoming_addition()
        with (fixture.root / ".git/info/exclude").open("a") as stream:
            stream.write("scratch.txt\n")
        local = fixture.root / "scratch.txt"
        local.write_text("synthetic unrelated work\n")

        # Existing cleanup policy blocks ignored primary state, after the safe sync.
        self.assertEqual(fixture.finish(), 3)
        self.assertEqual(fixture.git("rev-parse", "HEAD"), target)
        self.assertEqual(local.read_text(), "synthetic unrelated work\n")
        self.assert_task_preserved()
