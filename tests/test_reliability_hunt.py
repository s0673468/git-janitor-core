"""Second-pass regressions using invented identities and disposable repositories."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from git_janitor.config import ScannerConfig
from git_janitor.git import parse_github_remote, scan_repo


class ReliabilityHuntTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(prefix="janitor-hunt2-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.git(self.root, "init", "--initial-branch=main", str(self.repo))
        self.git(self.repo, "config", "user.name", "Hunt Fixture")
        self.git(self.repo, "config", "user.email", "fixture@example.invalid")
        self.git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.git(self.repo, "add", "base.txt")
        self.git(self.repo, "commit", "-m", "base")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.config = ScannerConfig(fetch_prune=False)

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd, check=True, capture_output=True, text=True,
        ).stdout.rstrip("\n")

    def test_proxy_remote_does_not_claim_embedded_github_identity(self) -> None:
        for remote in (
            "https://mirror.example.invalid/https://github.com/fixture-owner/project.git",
            "ssh://git@mirror.example.invalid/https://github.com/fixture-owner/project.git",
            "../mirrors/git@github.com:fixture-owner/project.git",
        ):
            with self.subTest(remote=remote):
                self.git(self.repo, "config", "remote.origin.url", remote)
                before = self.git(self.repo, "show-ref")

                state = scan_repo(self.repo, self.config)

                self.assertIsNone(state.github_repo)
                self.assertEqual(state.remote_url, remote)
                self.assertEqual(state.errors, [])
                self.assertEqual(self.git(self.repo, "show-ref"), before)

    def test_direct_github_remote_forms_remain_supported(self) -> None:
        for remote in (
            "git@github.com:fixture-owner/project.with-dots.git",
            "ssh://git@github.com/fixture-owner/project.with-dots.git",
            "https://github.com/fixture-owner/project.with-dots.git",
            "https://github.com/fixture-owner/project.with-dots/",
            " https://github.com/fixture-owner/project.with-dots.git \n",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(parse_github_remote(remote), "fixture-owner/project.with-dots")

    def test_unicode_separators_in_worktree_paths_cannot_hide_dirty_work(self) -> None:
        paths = []
        for index, separator in enumerate(("\u0085", "\u2028", "\u2029")):
            # splitlines() mistakes these legal filename characters for record
            # boundaries and truncates each path to the primary checkout's path.
            linked = Path(str(self.repo) + separator + "side")
            self.git(self.repo, "worktree", "add", "-b", f"side-{index}", str(linked))
            (linked / "base.txt").write_text("uncommitted\n", encoding="utf-8")
            paths.append(str(linked))

        state = scan_repo(self.repo, self.config)

        self.assertEqual(state.errors, [])
        self.assertCountEqual([worktree.path for worktree in state.linked_worktrees], paths)
        for worktree in state.linked_worktrees:
            self.assertEqual(worktree.dirty_files, ["base.txt"])

    def test_case_distinct_worktree_reports_are_stable_across_hash_seeds(self) -> None:
        upper = self.root / "Task"
        lower = self.root / "task"
        self.git(self.repo, "worktree", "add", "-b", "upper", str(upper))
        if lower.exists():
            self.skipTest("case-distinct directories require a case-sensitive filesystem")
        self.git(self.repo, "worktree", "add", "-b", "lower", str(lower))
        self.git(self.repo, "config", "remote.origin.url", "https://github.com/fixture-owner/project.git")
        other = self.root / "Repo"
        self.git(self.root, "clone", "--no-hardlinks", str(self.repo), str(other))
        self.git(other, "config", "remote.origin.url", "https://github.com/fixture-owner/project.git")
        script = """
from datetime import datetime, timezone
from pathlib import Path
import sys
from git_janitor.config import ScannerConfig
from git_janitor.git import scan_repo
from git_janitor.models import ScanReport
from git_janitor.reconciliation import (
    GithubInventory, GithubOwnerCoverage, GithubRepository, RegistryProject,
    RegistrySource, reconcile_inventory,
)
from git_janitor.report import render_json

repo = scan_repo(Path(sys.argv[1]), ScannerConfig(fetch_prune=False))
other = scan_repo(Path(sys.argv[3]), ScannerConfig(fetch_prune=False))
observed = "2026-10-22T12:00:00+00:00"
mode = sys.argv[2]
projects = (RegistryProject("fixture", "Fixture", "code", "active", "repo",
                            "fixture-owner/project", None),) if mode == "registered" else ()
registry = RegistrySource("fixture.json", observed, True, 1, None,
                          str(Path(repo.path).parent), projects)
repository = GithubRepository("fixture-owner/project", "fixture-owner", "project",
                              False, "PUBLIC", "main")
owners = (GithubOwnerCoverage("fixture-owner", True, observed, (repository,)),)
github = GithubInventory(True, True, observed, owners if mode != "local" else ())
result = reconcile_inventory(registry, github, [repo, other],
                             clock=lambda: datetime(2026, 10, 22, 12, tzinfo=timezone.utc))
print(render_json(ScanReport(observed, [repo, other], [], [], reconciliation=result)))
"""
        for mode in ("registered", "unregistered", "local"):
            outputs = []
            for seed in range(12):
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(self.repo), mode, str(other)],
                    env={**os.environ, "PYTHONHASHSEED": str(seed)},
                    check=True, capture_output=True, text=True,
                )
                outputs.append(completed.stdout)
            with self.subTest(mode=mode):
                self.assertEqual(len(set(outputs)), 1, "frozen report depends on hash seed")
                result = json.loads(outputs[0])["reconciliation"]
                local = next(item for item in result["local"]["repositories"]
                             if item["path"] == str(self.repo))
                self.assertEqual(local["worktree_paths"], [str(upper), str(lower)])
                row = next(item for item in result["rows"] if item["worktree_paths"])
                self.assertEqual(row["worktree_paths"], [str(upper), str(lower)])
                if mode != "local":
                    self.assertEqual(row["local_paths"], [str(other), str(self.repo)])


if __name__ == "__main__":
    unittest.main()
