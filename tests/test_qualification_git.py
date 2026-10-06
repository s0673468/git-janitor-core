"""Frozen observable scanner qualification using disposable Git repositories.

No test applies a scanner action or contacts a network service. Git writes are
fixture setup inside TemporaryDirectory; scan assertions verify preservation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_janitor.classify import classify_repo
from git_janitor.config import ScannerConfig
from git_janitor.git import (
    _same_path,
    _scan_linked_worktrees,
    discover_repos,
    run_command,
    scan_repo,
)
from git_janitor.models import CommandResult


NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)
MATRIX = Path(__file__).parent / "fixtures" / "qualification" / "matrix.json"


class QualificationGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="janitor-qualification-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.origin = self.root / "origin.git"
        self.repo = self.root / "repo"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.origin))
        self.git(self.root, "clone", str(self.origin), str(self.repo))
        self.configure(self.repo)
        self.commit(self.repo, "base.txt", "base\n")
        self.git(self.repo, "push", "-u", "origin", "main")
        self.git(self.repo, "remote", "set-head", "origin", "-a")
        self.config = ScannerConfig(fetch_prune=False)

    def git(self, path: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=path,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.rstrip()

    def configure(self, path: Path) -> None:
        self.git(path, "config", "user.name", "Qualification Fixture")
        self.git(path, "config", "user.email", "fixture@example.invalid")
        self.git(path, "config", "commit.gpgsign", "false")

    def commit(self, path: Path, filename: str, content: str) -> None:
        (path / filename).write_text(content, encoding="utf-8")
        self.git(path, "add", filename)
        self.git(path, "commit", "-m", filename)

    def categories(self, repo) -> list[str]:
        return [finding.category for finding in classify_repo(repo, self.config, NOW)]

    def test_GQ01_current_unpublished_branch_is_visible(self) -> None:
        self.git(self.repo, "checkout", "-b", "local-only")
        self.commit(self.repo, "local.txt", "unpublished\n")
        before = self.git(self.repo, "show-ref")

        repo = scan_repo(self.repo, self.config)

        self.assertIsNone(repo.upstream)
        self.assertEqual(repo.head_unique_commit_count, 1)
        self.assertIn("branch-without-upstream", self.categories(repo))
        self.assertEqual(self.git(self.repo, "show-ref"), before)

    def test_GQ02_detached_linked_work_preserves_dirty_and_untracked(self) -> None:
        detached = self.root / "detached"
        self.git(self.repo, "worktree", "add", "--detach", str(detached), "HEAD")
        (detached / "base.txt").write_text("edited\n", encoding="utf-8")
        (detached / "scratch.txt").write_text("untracked\n", encoding="utf-8")
        before = self.git(detached, "status", "--porcelain")

        repo = scan_repo(self.repo, self.config)
        linked = repo.linked_worktrees[0]

        self.assertIsNone(linked.branch)
        self.assertEqual(linked.dirty_files, ["base.txt"])
        self.assertEqual(linked.untracked_files, ["scratch.txt"])
        self.assertEqual(linked.unique_commit_count, 0)
        findings = classify_repo(repo, self.config, NOW)
        linked_categories = [f.category for f in findings if f.repo_path == str(detached)]
        self.assertIn("dirty-worktree", linked_categories)
        self.assertIn("detached-worktree", linked_categories)
        self.assertEqual(self.git(detached, "status", "--porcelain"), before)

    def test_GQ03_failed_fetch_without_stderr_is_not_clean(self) -> None:
        def fake(args, **kwargs):
            if args[:3] == ["git", "fetch", "--prune"]:
                return CommandResult(args, 1, "", "")
            return run_command(args, **kwargs)

        with patch("git_janitor.git.run_command", side_effect=fake):
            repo = scan_repo(self.repo, ScannerConfig(fetch_prune=True))

        self.assertTrue(repo.fetch_prune_status)
        self.assertNotEqual(repo.fetch_prune_status, "ok")
        self.assertIn("fetch-prune-failed", self.categories(repo))
        self.assertTrue(repo.errors)

    def test_GQ04_partial_branch_enumeration_is_incomplete(self) -> None:
        def fake(args, **kwargs):
            if args[:2] == ["git", "for-each-ref"]:
                return CommandResult(args, 1, "", "branch enumeration unavailable")
            return run_command(args, **kwargs)

        with patch("git_janitor.git.run_command", side_effect=fake):
            repo = scan_repo(self.repo, self.config)

        self.assertEqual(repo.branches, [])
        self.assertTrue(any("branch enumeration unavailable" in e for e in repo.errors))
        self.assertIn("scanner-error", self.categories(repo))

    def test_GQ05_divergence_reports_both_sides_and_preserves_refs(self) -> None:
        other = self.root / "other"
        self.git(self.root, "clone", str(self.origin), str(other))
        self.configure(other)
        self.commit(other, "remote.txt", "remote\n")
        self.git(other, "push")
        self.commit(self.repo, "local.txt", "local\n")
        self.git(self.repo, "fetch", "origin")
        before = self.git(self.repo, "show-ref")

        repo = scan_repo(self.repo, self.config)

        self.assertEqual((repo.ahead, repo.behind), (1, 1))
        self.assertIn("diverged-upstream", self.categories(repo))
        self.assertIn("unpushed-commits", self.categories(repo))
        self.assertEqual(self.git(self.repo, "show-ref"), before)

    def test_GQ06_local_merge_does_not_prove_remote_publication(self) -> None:
        self.git(self.repo, "checkout", "-b", "feature")
        self.commit(self.repo, "work.txt", "work\n")
        self.git(self.repo, "checkout", "main")
        self.git(self.repo, "merge", "--ff-only", "feature")

        repo = scan_repo(self.repo, self.config)
        branch = next(b for b in repo.branches if b.name == "feature")

        self.assertFalse(branch.merged_to_default)
        self.assertEqual(branch.unique_commit_count, 1)
        self.assertEqual(repo.ahead, 1)
        categories = self.categories(repo)
        self.assertIn("unpushed-commits", categories)
        self.assertIn("branch-without-upstream", categories)
        self.assertNotIn("merged-local-branch", categories)

    def test_GQ07_dirty_linked_live_upstream_is_visible(self) -> None:
        linked = self.root / "linked"
        self.git(self.repo, "branch", "feature")
        self.git(self.repo, "worktree", "add", str(linked), "feature")
        self.git(linked, "push", "-u", "origin", "feature")
        (linked / "base.txt").write_text("edited\n", encoding="utf-8")

        repo = scan_repo(self.repo, self.config)
        findings = classify_repo(repo, self.config, NOW)

        self.assertFalse(repo.linked_worktrees[0].upstream_gone)
        self.assertTrue(any(f.category == "dirty-worktree" and f.repo_path == str(linked)
                            for f in findings))

    def test_GQ08_missing_default_ref_preserves_unknown_comparison(self) -> None:
        self.git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        self.git(self.repo, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")

        repo = scan_repo(self.repo, self.config)

        self.assertIsNone(repo.default_ref)
        self.assertIsNone(repo.head_unique_commit_count)
        self.assertTrue(repo.errors)
        self.assertIn("scanner-error", self.categories(repo))
        self.assertNotIn("merged-local-branch", self.categories(repo))

    def test_GQ09_tracked_noncurrent_unpublished_commits_are_visible(self) -> None:
        self.git(self.repo, "checkout", "-b", "feature")
        self.git(self.repo, "push", "-u", "origin", "feature")
        self.commit(self.repo, "unpublished.txt", "unpublished\n")
        self.git(self.repo, "checkout", "main")

        repo = scan_repo(self.repo, self.config)
        branch = next(b for b in repo.branches if b.name == "feature")

        self.assertEqual(branch.ahead, 1)
        self.assertEqual(branch.behind, 0)
        self.assertTrue(any(f.category == "unpushed-commits" and "feature" in f.detail
                            for f in classify_repo(repo, self.config, NOW)))

    def test_gone_upstream_unique_work_is_visible(self) -> None:
        self.git(self.repo, "checkout", "-b", "feature")
        self.git(self.repo, "push", "-u", "origin", "feature")
        self.commit(self.repo, "unpublished.txt", "unpublished\n")
        self.git(self.repo, "push", "origin", "--delete", "feature")

        repo = scan_repo(self.repo, self.config)
        branch = next(b for b in repo.branches if b.current)

        self.assertTrue(branch.upstream_gone)
        self.assertEqual(repo.head_unique_commit_count, 1)
        findings = classify_repo(repo, self.config, NOW)
        self.assertTrue(any(f.category == "branch-without-upstream" and "gone" in f.detail
                            for f in findings))

    def test_noncurrent_default_unpublished_work_is_visible(self) -> None:
        self.commit(self.repo, "unpublished.txt", "unpublished\n")
        self.git(self.repo, "checkout", "-b", "other", "origin/main")

        repo = scan_repo(self.repo, self.config)

        self.assertTrue(any(f.category == "unpushed-commits" and "main" in f.detail
                            for f in classify_repo(repo, self.config, NOW)))

    def test_failed_branch_enumeration_retains_partial_observations(self) -> None:
        def fake(args, **kwargs):
            if args[:2] == ["git", "for-each-ref"]:
                return CommandResult(args, 1, "main|origin/main|2026-10-05|base", "partial refs")
            return run_command(args, **kwargs)

        with patch("git_janitor.git.run_command", side_effect=fake):
            repo = scan_repo(self.repo, self.config)

        self.assertEqual([branch.name for branch in repo.branches], ["main"])
        self.assertTrue(any("partial refs" in error for error in repo.errors))

    def test_nested_default_branch_name_and_oid_are_retained(self) -> None:
        self.git(self.repo, "branch", "-m", "release/main")
        self.git(self.repo, "push", "-u", "origin", "release/main")
        self.git(self.origin, "symbolic-ref", "HEAD", "refs/heads/release/main")
        self.git(self.repo, "remote", "set-head", "origin", "-a")

        repo = scan_repo(self.repo, self.config)

        self.assertEqual(repo.default_branch, "release/main")
        self.assertEqual(repo.default_oid, self.git(self.repo, "rev-parse", "origin/release/main"))
        self.assertNotIn("merged-local-branch", self.categories(repo))

    def test_detached_current_unique_commit_is_preserved(self) -> None:
        self.git(self.repo, "checkout", "--detach")
        self.commit(self.repo, "detached.txt", "detached\n")
        before = self.git(self.repo, "rev-parse", "HEAD")

        repo = scan_repo(self.repo, self.config)

        self.assertEqual(repo.head_oid, before)
        self.assertEqual(repo.head_unique_commit_count, 1)
        findings = classify_repo(repo, self.config, NOW)
        self.assertIn("detached-worktree", [f.category for f in findings])
        self.assertTrue(any("1" in f.detail for f in findings if f.category == "detached-worktree"))
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)

    def test_merged_branch_requires_successful_comparison(self) -> None:
        self.git(self.repo, "branch", "merged")

        def fake(args, **kwargs):
            if args[:2] == ["git", "cherry"]:
                return CommandResult(args, 128, "", "comparison unavailable")
            return run_command(args, **kwargs)

        with patch("git_janitor.git.run_command", side_effect=fake):
            repo = scan_repo(self.repo, self.config)

        self.assertTrue(repo.errors)
        self.assertNotIn("merged-local-branch", self.categories(repo))

    def mapped_directory_identity(self, alias: Path, target: Path, *, inaccessible=False):
        """Model casing aliases portably without requiring an APFS test host.

        The names remain lexically distinct through Path.resolve(). Native stat
        provides the target inode so samefile and discovery can observe physical
        identity. Only fixture paths are mapped; unrelated paths are untouched.
        """
        native_stat = os.stat

        def fixture_stat(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                candidate = Path(os.fsdecode(path))
                if candidate == alias or alias in candidate.parents:
                    if inaccessible and candidate == alias:
                        raise PermissionError("fixture checkout identity unavailable")
                    path = target / candidate.relative_to(alias)
            return native_stat(path, *args, **kwargs)

        return patch("os.stat", side_effect=fixture_stat)

    def test_GQ16_case_aliases_dedupe_physical_checkout(self) -> None:
        alias = self.repo.with_name("REPO")
        with self.mapped_directory_identity(alias, self.repo):
            self.assertNotEqual(self.repo.resolve(), alias.resolve())
            self.assertTrue(self.repo.samefile(alias))
            self.assertTrue(_same_path(self.repo, alias))
            errors = []
            discovered = discover_repos(
                ScannerConfig(repos=[self.repo, alias], scan_roots=[self.root], max_depth=1),
                errors=errors,
            )

        self.assertEqual(discovered, [self.repo])
        self.assertEqual(errors, [])

    def test_GQ16_current_alias_is_not_linked_but_distinct_worktree_is(self) -> None:
        alias = self.repo.with_name("REPO")
        linked = self.root / "linked"
        self.git(self.repo, "worktree", "add", "--detach", str(linked), "HEAD")
        raw = self.git(self.repo, "worktree", "list", "--porcelain")
        alias_raw = raw.replace(f"worktree {self.repo}\n", f"worktree {alias}\n", 1)

        def fake(args, **kwargs):
            if args[:3] == ["git", "worktree", "list"]:
                return CommandResult(args, 0, alias_raw, "")
            return run_command(args, **kwargs)

        with self.mapped_directory_identity(alias, self.repo), patch(
            "git_janitor.git.run_command", side_effect=fake,
        ):
            worktrees, errors = _scan_linked_worktrees(self.repo, "origin/main", self.config)
            discovered = discover_repos(ScannerConfig(repos=[self.repo, alias, linked]))

        self.assertEqual([tree.path for tree in worktrees], [str(linked)])
        self.assertEqual(errors, [])
        self.assertEqual(set(discovered), {self.repo, linked})
        self.assertEqual(
            self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            self.git(linked, "rev-parse", "--path-format=absolute", "--git-common-dir"),
        )

    def test_GQ16_case_sensitive_distinct_checkouts_stay_distinct(self) -> None:
        alias = self.repo.with_name("REPO")
        other = self.root / "other_repo"
        other.mkdir()
        self.git(other, "init", "--quiet")
        with self.mapped_directory_identity(alias, other):
            self.assertFalse(self.repo.samefile(alias))
            self.assertFalse(_same_path(self.repo, alias))
            discovered = discover_repos(ScannerConfig(repos=[self.repo, alias]))

        self.assertEqual(set(discovered), {self.repo, alias})

    def test_GQ16_inaccessible_identity_remains_unknown_and_preserved(self) -> None:
        alias = self.repo.with_name("REPO")
        errors = []
        with self.mapped_directory_identity(alias, self.repo, inaccessible=True):
            self.assertFalse(_same_path(self.repo, alias))
            discovered = discover_repos(ScannerConfig(repos=[self.repo, alias]), errors=errors)

        self.assertEqual(set(discovered), {self.repo, alias})
        self.assertTrue(any("identity" in error for error in errors))
        missing = self.root / "missing"
        missing_alias = self.root / "MISSING"
        self.assertFalse(_same_path(missing, missing_alias))
        self.assertTrue(_same_path(missing, missing))

    def test_frozen_matrix_has_unique_case_ids_and_preservation_contract(self) -> None:
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        self.assertEqual(matrix["schema_version"], 1)
        ids = [case["id"] for case in matrix["cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {f"GQ{number:02d}" for number in range(1, 20)})
        self.assertTrue(matrix["constraints"]["no_real_repository_mutations"])
        self.assertTrue(all(case["expected_disposition"] for case in matrix["cases"]))
