"""Cleanup gathers bounded ignored-file evidence independently of dirty status."""
from datetime import datetime, timezone
from unittest.mock import patch
import unittest

from git_janitor.autonomy import decide_branch_cleanup, decide_linked_worktree_cleanup
from git_janitor.classify import classify_repo
from git_janitor.git import run_command, scan_repo
from git_janitor.models import BranchState, CommandResult, LinkedWorktreeState, RepoState
import test_ignored_status as fixtures


class CleanupIgnoredEvidenceTests(unittest.TestCase):
    setUp = fixtures.IgnoredStatusTests.setUp
    git = fixtures.IgnoredStatusTests.git
    build_output = fixtures.IgnoredStatusTests.build_output

    def findings(self, state):
        return classify_repo(state, self.config, datetime.now(timezone.utc))

    def test_branch_cleanup_collects_collapsed_ignored_directories_without_dirtying_scan(self):
        self.git("branch", "topic")
        self.build_output(self.repo, count=300)
        index = (self.repo / ".git/index").read_bytes()
        commands = []

        def runner(args, *, cwd=None, timeout=45):
            commands.append(args)
            return run_command(args, cwd, timeout)

        with patch("git_janitor.git.run_command", side_effect=runner):
            state = scan_repo(self.repo, self.config)
        self.assertEqual(state.errors, [])
        self.assertEqual((state.dirty_files, state.untracked_files, state.ignored_files),
                         ([], [], []))
        self.assertEqual(len(state.cleanup_blockers), 2)
        self.assertTrue(any(".venv/" in item for item in state.cleanup_blockers))
        self.assertTrue(any("node_modules/" in item for item in state.cleanup_blockers))
        ignored_queries = [args for args in commands if "--ignored" in args]
        self.assertEqual(len(ignored_queries), 1)
        self.assertIn("--directory", ignored_queries[0])
        self.assertNotIn("--untracked-files=all", ignored_queries[0])
        topic = next(branch for branch in state.branches if branch.name == "topic")
        decision = decide_branch_cleanup(state, topic)
        self.assertNotEqual(decision.category, "delete-merged-branch")
        self.assertTrue(set(state.cleanup_blockers).issubset(decision.evidence))
        self.assertFalse(any(f.category in {"dirty-worktree", "merged-local-branch"}
                             for f in self.findings(state)))
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertTrue((self.repo / ".venv/nested/file-299.txt").exists())

    def test_failed_cleanup_inspection_blocks_deletion_without_blocking_general_scan(self):
        self.git("branch", "topic")

        def runner(args, *, cwd=None, timeout=45):
            if "--ignored" in args:
                return CommandResult(args, 124, "", "synthetic timeout")
            return run_command(args, cwd, timeout)

        with patch("git_janitor.git.run_command", side_effect=runner):
            state = scan_repo(self.repo, self.config)
        topic = next(branch for branch in state.branches if branch.name == "topic")
        self.assertEqual(state.errors, [])
        self.assertTrue(any("unknown" in item for item in state.cleanup_blockers))
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")
        self.assertFalse(any(f.category == "merged-local-branch" for f in self.findings(state)))

    def test_scan_without_cleanup_targets_does_not_query_ignored_trees(self):
        self.build_output(self.repo, count=300)
        commands = []

        def runner(args, *, cwd=None, timeout=45):
            commands.append(args)
            return run_command(args, cwd, timeout)

        with patch("git_janitor.git.run_command", side_effect=runner):
            state = scan_repo(self.repo, self.config)
        self.assertEqual(state.cleanup_blockers, [])
        self.assertFalse(any("--ignored" in args for args in commands))

    def stale_linked(self):
        linked = self.root / "linked"
        self.git("worktree", "add", "-b", "topic", str(linked))
        self.git("config", "branch.topic.remote", "origin")
        self.git("config", "branch.topic.merge", "refs/heads/topic")
        return linked

    def test_stale_linked_cleanup_checks_its_own_ignored_files(self):
        linked = self.stale_linked()
        self.build_output(linked, count=300)
        state = scan_repo(self.repo, self.config)
        worktree = state.linked_worktrees[0]
        self.assertEqual(state.cleanup_blockers, [])
        self.assertEqual(worktree.ignored_files, [])
        self.assertEqual(len(worktree.cleanup_blockers), 2)
        self.assertNotEqual(decide_linked_worktree_cleanup(state, worktree).category,
                            "stale-linked-worktree-cleanup")
        self.assertFalse(any(f.category == "stale-linked-worktree" and f.severity == "low"
                             for f in self.findings(state)))
        self.assertTrue((linked / ".venv/nested/file-299.txt").exists())

    def test_linked_ignored_inspection_is_scoped_to_linked_checkout(self):
        self.stale_linked()
        baseline = scan_repo(self.repo, self.config).linked_worktrees[0]
        self.build_output(self.repo)
        state = scan_repo(self.repo, self.config)
        worktree = state.linked_worktrees[0]
        self.assertEqual(worktree.cleanup_blockers, [])
        self.assertEqual(worktree.errors, baseline.errors)
        self.assertEqual(decide_linked_worktree_cleanup(state, worktree),
                         decide_linked_worktree_cleanup(state, baseline))

    def test_failed_linked_ignored_inspection_blocks_cleanup_recommendations(self):
        linked = self.stale_linked()

        def runner(args, *, cwd=None, timeout=45):
            if "--ignored" in args and cwd == linked:
                return CommandResult(args, 124, "", "synthetic timeout")
            return run_command(args, cwd, timeout)

        with patch("git_janitor.git.run_command", side_effect=runner):
            state = scan_repo(self.repo, self.config)
        worktree = state.linked_worktrees[0]
        self.assertTrue(worktree.cleanup_blockers)
        self.assertNotEqual(decide_linked_worktree_cleanup(state, worktree).category,
                            "stale-linked-worktree-cleanup")
        self.assertFalse(any(f.category == "stale-linked-worktree" and f.severity == "low"
                             for f in self.findings(state)))

    def test_explicit_ignored_evidence_also_blocks_cleanup(self):
        for field in ("ignored_files", "cleanup_blockers"):
            with self.subTest(evidence=field):
                branch = BranchState(name="topic", merged_to_default=True, unique_commit_count=0)
                state = RepoState(path="synthetic", name="repo", branches=[branch],
                                  **{field: ["synthetic-output/"]})
                self.assertNotEqual(decide_branch_cleanup(state, branch).category,
                                    "delete-merged-branch")
                self.assertFalse(any(f.category == "merged-local-branch"
                                     for f in self.findings(state)))
                worktree = LinkedWorktreeState(
                    path="synthetic-linked", branch="topic", upstream_gone=True,
                    tree_matches_default=True, unique_commit_count=0,
                    **{field: ["synthetic-output/"]},
                )
                state.linked_worktrees = [worktree]
                self.assertNotEqual(decide_linked_worktree_cleanup(state, worktree).category,
                                    "stale-linked-worktree-cleanup")
                self.assertFalse(any(f.category == "stale-linked-worktree" and f.severity == "low"
                                     for f in self.findings(state)))
