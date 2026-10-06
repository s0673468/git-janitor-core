"""GQ20: configured tracking is independent of collected upstream evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_janitor.autonomy import decide_branch_cleanup, decide_linked_worktree_cleanup
from git_janitor.classify import classify_repo
from git_janitor.config import ScannerConfig
from git_janitor.delivery_queue import build_delivery_queue
from git_janitor.git import run_command, scan_repo
from git_janitor.models import CommandResult, ScanReport
from git_janitor.report import render_json
from git_janitor.reproduce import restore_report


NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


class PartialTrackingTests(unittest.TestCase):
    def setUp(self):
        isolated = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        isolated.start()
        self.addCleanup(isolated.stop)
        temporary = tempfile.TemporaryDirectory(prefix="janitor-tracking-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.origin = self.root / "origin.git"
        seed = self.root / "seed"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.origin))
        self.git(self.root, "clone", str(self.origin), str(seed))
        self.git(seed, "config", "user.name", "Synthetic Fixture")
        self.git(seed, "config", "user.email", "fixture@example.invalid")
        self.git(seed, "config", "commit.gpgsign", "false")
        (seed / "base.txt").write_text("base\n")
        self.git(seed, "add", "base.txt")
        self.git(seed, "commit", "-m", "base")
        self.git(seed, "push", "origin", "main")
        self.git(seed, "checkout", "-b", "feature")
        (seed / "feature.txt").write_text("feature\n")
        self.git(seed, "add", "feature.txt")
        self.git(seed, "commit", "-m", "feature")
        self.git(seed, "push", "origin", "feature")
        self.repo = self.root / "partial"
        self.git(self.root, "clone", "--single-branch", "--branch", "main",
                 str(self.origin), str(self.repo))
        self.git(self.repo, "fetch", "origin", "refs/heads/feature")
        self.git(self.repo, "checkout", "-b", "feature", "FETCH_HEAD")
        self.git(self.repo, "config", "branch.feature.remote", "origin")
        self.git(self.repo, "config", "branch.feature.merge", "refs/heads/feature")
        self.config = ScannerConfig(fetch_prune=False)

    def git(self, path, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=path,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            check=True, capture_output=True, text=True,
        ).stdout.rstrip()

    def assert_partial(self, repo, state, *, path):
        self.assertTrue(any("tracking" in error and "unknown" in error for error in repo.errors))
        self.assertTrue(state.tracking_configured)
        self.assertEqual(state.tracking_remote, "origin")
        self.assertEqual(state.tracking_merge, "refs/heads/feature")
        self.assertIsNone(state.upstream)  # Do not manufacture origin/feature.
        self.assertTrue(any("tracking" in error and "unknown" in error for error in repo.errors))
        findings = classify_repo(repo, self.config, NOW)
        self.assertNotIn("branch-without-upstream", [f.category for f in findings if f.repo_path == path])
        self.assertIn("scanner-error", [f.category for f in findings])
        self.assertEqual(self.git(self.origin, "rev-parse", "refs/heads/feature"),
                         self.git(self.repo, "rev-parse", "feature"))
        return findings

    def test_GQ20_configured_unmapped_tracking_is_comparison_unknown(self):
        before = self.git(self.repo, "show-ref")
        (self.repo / "scratch.txt").write_text("preserve\n")
        status = self.git(self.repo, "status", "--porcelain")
        repo = scan_repo(self.repo, self.config)
        findings = self.assert_partial(repo, repo, path=str(self.repo))
        branch = next(b for b in repo.branches if b.current)
        self.assertTrue(branch.tracking_configured)
        self.assertEqual(repo.head_unique_commit_count, 1)
        self.assertEqual(branch.unique_commit_count, 1)
        self.assertEqual(repo.untracked_files, ["scratch.txt"])
        self.assertEqual(self.git(self.repo, "show-ref"), before)
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), status)
        report = ScanReport("fixed", [repo], [], findings)
        queue = build_delivery_queue(report)
        self.assertEqual(queue[0].status, "coverage-gap")
        self.assertTrue(all("Inspection only" in item.authorization for item in queue))
        payload = render_json(report)
        self.assertEqual(render_json(restore_report(json.loads(payload))), payload)

    def test_GQ20_noncurrent_configured_unmapped_is_not_unconfigured(self):
        self.git(self.repo, "checkout", "main")
        repo = scan_repo(self.repo, self.config)
        branch = next(b for b in repo.branches if b.name == "feature")
        self.assert_partial(repo, branch, path=str(self.repo))
        self.assertEqual(branch.unique_commit_count, 1)
        self.assertEqual(decide_branch_cleanup(repo, branch).disposition, "report-blocked")

    def test_GQ20_linked_configured_unmapped_preserves_work(self):
        self.git(self.repo, "checkout", "main")
        linked = self.root / "linked"
        self.git(self.repo, "worktree", "add", str(linked), "feature")
        (linked / "base.txt").write_text("dirty\n")
        (linked / "scratch.txt").write_text("preserve\n")
        before = self.git(linked, "status", "--porcelain")
        repo = scan_repo(self.repo, self.config)
        state = repo.linked_worktrees[0]
        findings = self.assert_partial(repo, state, path=str(linked))
        self.assertTrue(state.errors)
        self.assertEqual(state.unique_commit_count, 1)
        self.assertEqual(state.dirty_files, ["base.txt"])
        self.assertEqual(state.untracked_files, ["scratch.txt"])
        self.assertIn("dirty-worktree", [f.category for f in findings if f.repo_path == str(linked)])
        self.assertEqual(self.git(linked, "status", "--porcelain"), before)

    def test_GQ20_unconfigured_and_valid_mapping_controls(self):
        self.git(self.repo, "config", "--unset", "branch.feature.remote")
        self.git(self.repo, "config", "--unset", "branch.feature.merge")
        unconfigured = scan_repo(self.repo, self.config)
        self.assertFalse(unconfigured.tracking_configured)
        self.assertFalse(unconfigured.errors)
        self.assertIn("branch-without-upstream", [f.category for f in classify_repo(unconfigured, self.config, NOW)])
        self.git(self.repo, "config", "branch.feature.remote", "origin")
        self.git(self.repo, "config", "branch.feature.merge", "refs/heads/feature")
        self.git(self.repo, "config", "--add", "remote.origin.fetch",
                 "+refs/heads/feature:refs/remotes/origin/feature")
        self.git(self.repo, "fetch", "origin", "feature")
        mapped = scan_repo(self.repo, self.config)
        self.assertTrue(mapped.tracking_configured)
        self.assertEqual(mapped.upstream, "origin/feature")
        self.assertFalse(mapped.errors)
        self.assertNotIn("branch-without-upstream", [f.category for f in classify_repo(mapped, self.config, NOW)])
        self.git(self.repo, "config", "branch.feature.remote", ".")
        self.git(self.repo, "config", "branch.feature.merge", "refs/heads/main")
        local = scan_repo(self.repo, self.config)
        self.assertEqual(local.tracking_remote, ".")
        self.assertEqual(local.upstream, "main")
        self.assertEqual(local.ahead, 1)
        self.assertFalse(local.errors)

    def test_GQ20_missing_local_mapped_ref_is_not_remote_deletion(self):
        self.git(self.repo, "config", "--add", "remote.origin.fetch",
                 "+refs/heads/feature:refs/remotes/origin/feature")
        current = scan_repo(self.repo, self.config)
        self.assertTrue(current.tracking_configured)
        self.assertEqual(current.upstream, "origin/feature")
        self.assertTrue(any("remote existence" in error and "unknown" in error for error in current.errors))
        self.git(self.repo, "checkout", "main")
        linked = self.root / "linked"
        self.git(self.repo, "worktree", "add", str(linked), "feature")
        repo = scan_repo(self.repo, self.config)
        state = repo.linked_worktrees[0]
        self.assertEqual(state.upstream, "origin/feature")
        self.assertTrue(state.upstream_gone)  # Git's local [gone] observation only.
        self.assertTrue(state.tracking_configured)
        self.assertTrue(any("remote existence" in error and "unknown" in error for error in state.errors))
        self.assertEqual(decide_linked_worktree_cleanup(repo, state).disposition, "report-blocked")
        self.assertEqual(self.git(self.origin, "rev-parse", "refs/heads/feature"), state.head)
        detail = " ".join(f.detail for f in classify_repo(repo, self.config, NOW))
        self.assertNotIn("remote branch deleted", detail)

    def test_GQ20_config_inspection_failure_is_unknown(self):
        def failed_config(args, **kwargs):
            if args[1:3] == ["config", "--get"] and args[-1] == "branch.feature.remote":
                return CommandResult(args, 128, "", "private credential-like stderr")
            return run_command(args, **kwargs)
        with patch("git_janitor.git.run_command", side_effect=failed_config):
            repo = scan_repo(self.repo, self.config)
        self.assertIsNone(repo.tracking_configured)
        self.assertTrue(any("tracking configuration" in error for error in repo.errors))
        self.assertEqual(repo.head_unique_commit_count, 1)
        self.assertNotIn("branch-without-upstream", [f.category for f in classify_repo(repo, self.config, NOW)])
        self.assertNotIn("private credential-like stderr", render_json(ScanReport("fixed", [repo], [], [])))

    def test_GQ20_remote_location_credentials_are_not_serialized(self):
        def remote_location(args, **kwargs):
            if args[1:3] == ["config", "--get"] and args[-1] == "branch.feature.remote":
                return CommandResult(args, 0, "https://synthetic:fixture-secret@example.invalid/r?token=fixture-token", "")
            return run_command(args, **kwargs)
        with patch("git_janitor.git.run_command", side_effect=remote_location):
            repo = scan_repo(self.repo, self.config)
        self.assertTrue(repo.tracking_configured)
        content = render_json(ScanReport("fixed", [repo], [], classify_repo(repo, self.config, NOW)))
        self.assertNotIn("fixture-secret", content)
        self.assertNotIn("fixture-token", content)
        self.assertEqual(repo.tracking_remote, "[remote location redacted]")

    def test_GQ20_incomplete_tracking_and_invalid_merge_are_preserved(self):
        self.git(self.repo, "config", "--unset", "branch.feature.merge")
        incomplete = scan_repo(self.repo, self.config)
        self.assertTrue(incomplete.tracking_configured)
        self.assertIsNone(incomplete.tracking_merge)
        self.assertIsNone(incomplete.upstream)
        self.assertTrue(incomplete.errors)
        self.assertEqual(incomplete.head_unique_commit_count, 1)
        self.assertNotIn("branch-without-upstream", [f.category for f in classify_repo(incomplete, self.config, NOW)])

        def invalid_merge(args, **kwargs):
            if args[1:3] == ["config", "--get"] and args[-1] == "branch.feature.merge":
                return CommandResult(args, 0, "refs/heads/https://synthetic:merge-secret@example.invalid/r?token=merge-token", "")
            return run_command(args, **kwargs)
        with patch("git_janitor.git.run_command", side_effect=invalid_merge):
            invalid = scan_repo(self.repo, self.config)
        self.assertTrue(invalid.tracking_configured)
        self.assertEqual(invalid.tracking_merge, "[merge target redacted]")
        content = render_json(ScanReport("fixed", [invalid], [], classify_repo(invalid, self.config, NOW)))
        self.assertNotIn("merge-secret", content)
        self.assertNotIn("merge-token", content)
