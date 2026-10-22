"""Saved work blocks cleanup while unrelated updates remain safe and offline."""
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import unittest

from git_janitor.autonomy import (
    decide_branch_cleanup, decide_linked_worktree_cleanup, decide_touched_repo_candidate,
)
from git_janitor.classify import classify_repo
from git_janitor.fleet import TouchedRepoMaintainerConfig, build_touched_repo_candidates
from git_janitor.git import fast_forward_blockers, run_command, scan_repo
from git_janitor.models import CommandResult
from git_janitor.safe_delete import evaluate_delete
import test_fast_forward_preservation as fixtures


class CleanupUpdateBlockerTests(unittest.TestCase):
    setUp = fixtures.FastForwardPreservationTests.setUp
    git = fixtures.FastForwardPreservationTests.git
    incoming = fixtures.FastForwardPreservationTests.incoming
    runner = fixtures.FastForwardPreservationTests.runner
    execute = fixtures.FastForwardPreservationTests.execute

    def assert_update_allowed(self, state):
        self.assertEqual(state.errors, [])
        self.assertEqual((state.dirty_files, state.untracked_files), ([], []))
        for worktree in state.linked_worktrees:
            self.assertEqual((worktree.dirty_files, worktree.untracked_files), ([], []))
        findings = classify_repo(state, self.config, datetime.now(timezone.utc))
        self.assertFalse(any(f.category == "scanner-error" for f in findings), findings)
        candidates = build_touched_repo_candidates(
            [state], [], TouchedRepoMaintainerConfig(active_window_minutes=0),
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(decide_touched_repo_candidate(candidates[0]).category,
                         "fast-forward-default-branch")
        result = self.execute()
        self.assertEqual(result.status, "applied", result)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)

    def assert_cleanup_blocked(self, state):
        topic = next(branch for branch in state.branches if branch.name == "cleanup-topic")
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")
        def runner(args, cwd, timeout):
            if args[:3] == ["gh", "pr", "list"]:
                return CommandResult(args, 0, "[]", "")
            return self.runner(args, cwd, timeout)

        report = evaluate_delete("cleanup-topic", repo_path=self.repo, runner=runner)
        self.assertFalse(report.passed)
        self.assertFalse(next(p.passed for p in report.proofs
                              if p.name == "saved_and_nested_work_preserved"))
        self.assertEqual(self.git("rev-parse", "cleanup-topic"), self.base)

    def stash(self, cwd=None):
        path = cwd or self.repo
        (path / ".gitignore").write_text("synthetic unfinished change\n")
        self.git("stash", "push", cwd=path)
        return self.git("rev-parse", "refs/stash")

    def linked(self, name, *, gone=False):
        linked = self.root / name
        self.git("worktree", "add", "-b", name, str(linked))
        self.git("config", f"branch.{name}.remote", "origin")
        self.git("config", f"branch.{name}.merge", f"refs/heads/{name if gone else 'main'}")
        return linked

    def add_submodule_entry(self):
        # An uninitialized gitlink needs no clone, service or nested personal data.
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.base},module")
        (self.repo / "module").mkdir()
        self.git("commit", "-m", "synthetic uninitialized submodule")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)

    def test_stash_blocks_cleanup_but_allows_actual_fast_forward(self):
        stash = self.stash()
        self.git("branch", "cleanup-topic")
        self.incoming("tracked.txt")
        (self.repo / "ignored-output").write_text("synthetic ignored output\n")
        state = scan_repo(self.repo, self.config)
        self.assertEqual(state.cleanup_blockers.count(
            "stashed work requires preservation before cleanup"), 1)
        self.assertTrue(any("ignored-output" in item for item in state.cleanup_blockers))
        self.assert_cleanup_blocked(state)
        self.assert_update_allowed(state)
        self.assertEqual(self.git("rev-parse", "refs/stash"), stash)
        self.assertEqual((self.repo / "ignored-output").read_text(), "synthetic ignored output\n")

    def test_shared_stash_is_inspected_once_and_blocks_each_linked_cleanup(self):
        first = self.linked("first")
        self.linked("second")
        stash = self.stash(cwd=first)
        self.incoming("tracked.txt")
        commands = []

        def runner(args, cwd=None, timeout=45):
            commands.append((args, cwd))
            return run_command(args, cwd, timeout)

        with patch("git_janitor.git.run_command", side_effect=runner):
            state = scan_repo(self.repo, self.config)
        self.assertEqual([cwd for args, cwd in commands if args[:2] == ["git", "stash"]],
                         [self.repo])
        self.assertEqual(state.cleanup_blockers,
                         ["stashed work requires preservation before cleanup"])
        for worktree in state.linked_worktrees:
            self.assertEqual(worktree.errors, [])
            self.assertTrue(worktree.cleanup_blockers)
            self.assertFalse(any("stashed work requires" in item
                                 for item in worktree.cleanup_blockers))
            self.assertNotEqual(decide_linked_worktree_cleanup(state, worktree).category,
                                "stale-linked-worktree-cleanup")
        findings = classify_repo(state, self.config, datetime.now(timezone.utc))
        self.assertFalse(any(f.category == "stale-linked-worktree" and f.severity == "low"
                             for f in findings))
        self.assert_update_allowed(state)
        self.assertEqual(self.git("rev-parse", "refs/stash"), stash)

    def test_submodules_block_cleanup_but_allow_actual_fast_forward(self):
        self.add_submodule_entry()
        self.git("branch", "cleanup-topic")
        self.linked("linked")
        self.incoming("tracked.txt")
        state = scan_repo(self.repo, self.config)
        message = "submodules require separate preservation proof before cleanup"
        self.assertIn(message, state.cleanup_blockers)
        worktree = state.linked_worktrees[0]
        self.assertEqual(worktree.errors, [])
        self.assertIn(message, worktree.cleanup_blockers)
        self.assertNotEqual(decide_linked_worktree_cleanup(state, worktree).category,
                            "stale-linked-worktree-cleanup")
        self.assert_cleanup_blocked(state)
        self.assert_update_allowed(state)

    def test_failed_cleanup_inspections_do_not_poison_update_safety(self):
        self.linked("linked")
        self.incoming("tracked.txt")
        for command in (["git", "stash", "list", "--format=%H"],
                        ["git", "ls-files", "--stage", "-z"]):
            with self.subTest(command=command):
                def runner(args, cwd=None, timeout=45):
                    if args == command:
                        return CommandResult(args, 124, "", "synthetic timeout")
                    return run_command(args, cwd, timeout)

                with patch("git_janitor.git.run_command", side_effect=runner):
                    state = scan_repo(self.repo, self.config)
                self.assertEqual(state.errors, [])
                self.assertTrue(any("unknown" in item for item in state.cleanup_blockers))
                worktree = state.linked_worktrees[0]
                self.assertEqual(worktree.errors, [])
                self.assertTrue(worktree.cleanup_blockers)
                self.assertNotEqual(decide_linked_worktree_cleanup(state, worktree).category,
                                    "stale-linked-worktree-cleanup")
                candidates = build_touched_repo_candidates(
                    [state], [], TouchedRepoMaintainerConfig(active_window_minutes=0),
                )
                self.assertEqual(decide_touched_repo_candidate(candidates[0]).category,
                                 "fast-forward-default-branch")

    def test_stale_linked_ignored_evidence_keeps_shared_and_submodule_blockers(self):
        self.add_submodule_entry()
        linked = self.linked("stale", gone=True)
        baseline = scan_repo(self.repo, self.config)
        # Missing-upstream diagnostics are independent of saved-work blockers.
        eligible = replace(baseline.linked_worktrees[0], errors=[], cleanup_blockers=[])
        self.assertEqual(decide_linked_worktree_cleanup(baseline, eligible).category,
                         "stale-linked-worktree-cleanup")
        self.stash(cwd=linked)
        (linked / "ignored-output").write_text("synthetic ignored output\n")
        state = scan_repo(self.repo, self.config)
        worktree = state.linked_worktrees[0]
        self.assertEqual(state.errors, baseline.errors)
        self.assertEqual(worktree.errors, baseline.linked_worktrees[0].errors)
        self.assertTrue(any("shared saved-work" in item for item in worktree.cleanup_blockers))
        self.assertTrue(any("submodules require" in item for item in worktree.cleanup_blockers))
        self.assertTrue(any("ignored-output" in item for item in worktree.cleanup_blockers))
        blocked = replace(worktree, errors=[])
        self.assertNotEqual(decide_linked_worktree_cleanup(state, blocked).category,
                            "stale-linked-worktree-cleanup")

    def tracked_base(self, name):
        (self.repo / name).write_text("synthetic tracked file\n")
        self.git("add", name)
        self.git("commit", "-m", "synthetic tracked base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)

    def test_tracked_file_replaced_by_directory_allows_actual_fast_forward(self):
        self.tracked_base("tracked")
        self.git("switch", "-c", "incoming")
        self.git("rm", "tracked")
        (self.repo / "tracked").mkdir()
        (self.repo / "tracked/child").write_text("synthetic incoming child\n")
        self.git("add", "tracked/child")
        self.git("commit", "-m", "synthetic file to directory transition")
        self.target = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.target)
        self.git("switch", "main")
        self.assert_update_allowed(scan_repo(self.repo, self.config))
        self.assertEqual((self.repo / "tracked/child").read_text(), "synthetic incoming child\n")

    def case_rename(self, *, keep_old=False):
        self.tracked_base("Readme")
        self.git("switch", "-c", "incoming")
        if keep_old:
            (self.repo / "README").write_text("synthetic new file\n")
            self.git("add", "README")
        else:
            self.git("mv", "Readme", "README")
        self.git("commit", "-m", "synthetic case change")
        self.target = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.target)
        self.git("switch", "main")

    def case_insensitive_lstat(self):
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            if path == self.repo / "README":
                path = self.repo / "Readme"
            return original(path, *args, **kwargs)

        return patch.object(Path, "lstat", lstat)

    def test_case_only_rename_allows_head_alias_on_insensitive_filesystem(self):
        self.case_rename()
        # Model the filesystem alias, keeping real HEAD and target tree evidence.
        with self.case_insensitive_lstat():
            self.assertEqual(fast_forward_blockers(self.repo, self.base, self.target), [])
            self.assertEqual(self.execute(mode="dry-run").status, "would-apply")
        self.assert_update_allowed(scan_repo(self.repo, self.config))
        self.assertEqual((self.repo / "README").read_text(), "synthetic tracked file\n")

    def test_case_alias_is_not_safe_when_target_retains_original_head_path(self):
        self.case_rename(keep_old=True)
        with self.case_insensitive_lstat():
            self.assertTrue(fast_forward_blockers(self.repo, self.base, self.target))

    def test_distinct_untracked_case_variant_still_blocks_even_when_hard_linked(self):
        self.case_rename()
        (self.repo / "README").hardlink_to(self.repo / "Readme")
        blockers = fast_forward_blockers(self.repo, self.base, self.target)
        self.assertTrue(any("overwrite" in blocker for blocker in blockers), blockers)
        result = self.execute()
        self.assertEqual(result.status, "drifted", result)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual((self.repo / "README").read_text(), "synthetic tracked file\n")

    def test_failed_head_or_target_tree_inspection_fails_closed(self):
        self.incoming()
        (self.repo / "ignored.txt").write_text("synthetic local\n")
        for revision in (self.base, self.target):
            with self.subTest(revision=revision):
                def runner(args, cwd=None, timeout=45):
                    if args[:2] == ["git", "ls-tree"] and revision in args:
                        return CommandResult(args, 124, "", "synthetic timeout")
                    return run_command(args, cwd, timeout)

                blockers = fast_forward_blockers(self.repo, self.base, self.target, runner)
                self.assertTrue(any("unknown" in blocker for blocker in blockers), blockers)
                result = self.execute(runner=runner)
                self.assertEqual(result.status, "drifted", result)
                self.assertEqual((self.repo / "ignored.txt").read_text(), "synthetic local\n")
