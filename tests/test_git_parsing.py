from __future__ import annotations

import unittest
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from git_janitor.config import ScannerConfig
from git_janitor.git import (
    discover_repos,
    parse_github_remote,
    parse_status_porcelain,
    parse_worktree_porcelain,
    run_command,
    status_upstream_gone,
)


class GitParsingTests(unittest.TestCase):
    def test_parse_status_header_and_files(self) -> None:
        output = "\n".join(
            [
                "## feature...origin/feature [ahead 2, behind 1]",
                " M app.py",
                "A  new.py",
                "?? scratch.txt",
            ]
        )

        dirty, untracked, ahead, behind, upstream = parse_status_porcelain(output)

        self.assertEqual(dirty, ["app.py", "new.py"])
        self.assertEqual(untracked, ["scratch.txt"])
        self.assertEqual(ahead, 2)
        self.assertEqual(behind, 1)
        self.assertEqual(upstream, "origin/feature")

    def test_run_command_keeps_leading_status_space(self) -> None:
        # `git status --porcelain=v1` encodes the staged/worktree state in the
        # first two columns, so the first entry of an unstaged change starts
        # with a space. Stripping stdout shifts that line left and truncates
        # the first reported path (" M alpha.py" -> "lpha.py").
        result = run_command(["printf", "%s", " M alpha.py\n?? beta.py\n"])

        self.assertEqual(result.returncode, 0)
        dirty, untracked, _ahead, _behind, _upstream = parse_status_porcelain(result.stdout)
        self.assertEqual(dirty, ["alpha.py"])
        self.assertEqual(untracked, ["beta.py"])

    def test_run_command_preserves_exact_git_diff_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            source = root / "example.txt"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "example.txt"], cwd=root, check=True)
            source.write_text("after  \n", encoding="utf-8")
            args = ["git", "diff", "--no-ext-diff", "--unified=4"]

            raw = subprocess.run(
                args,
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            wrapped = run_command(args, root, 20)

        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(wrapped.stdout, raw)

    def test_parse_github_remotes(self) -> None:
        self.assertEqual(
            parse_github_remote("git@github.com:owner/repo.git"),
            "owner/repo",
        )
        self.assertEqual(
            parse_github_remote("https://github.com/owner/repo.git"),
            "owner/repo",
        )
        self.assertEqual(
            parse_github_remote("ssh://git@github.com/owner/repo.with.dots.git"),
            "owner/repo.with.dots",
        )
        self.assertEqual(
            parse_github_remote("https://github.com/owner/repo.with.dots"),
            "owner/repo.with.dots",
        )

    def test_parse_worktree_porcelain(self) -> None:
        output = "\n".join(
            [
                "worktree /repo",
                "HEAD abc123",
                "branch refs/heads/main",
                "",
                "worktree /tmp/repo-feature",
                "HEAD def456",
                "branch refs/heads/codex/old-feature",
            ]
        )

        entries = parse_worktree_porcelain(output)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].path, "/repo")
        self.assertEqual(entries[0].branch, "main")
        self.assertEqual(entries[1].path, "/tmp/repo-feature")
        self.assertEqual(entries[1].branch, "codex/old-feature")

    def test_status_upstream_gone(self) -> None:
        self.assertTrue(
            status_upstream_gone(
                "## codex/old...origin/codex/old [gone]\n",
            )
        )
        self.assertFalse(
            status_upstream_gone(
                "## codex/old...origin/codex/old [ahead 1]\n",
            )
        )

    def test_discovery_can_report_missing_and_unreadable_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_root = root / "missing-root"
            missing_repo = root / "missing-repo"
            errors: list[str] = []

            repos = discover_repos(
                ScannerConfig(scan_roots=[missing_root], repos=[missing_repo]),
                errors=errors,
            )

            self.assertEqual(repos, [])
            self.assertEqual(len(errors), 2)
            self.assertIn("configured repository is missing", errors[0])
            self.assertIn("configured scan root is missing", errors[1])

            errors = []
            with mock.patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
                repos = discover_repos(
                    ScannerConfig(scan_roots=[root], max_depth=1),
                    errors=errors,
                )

            self.assertEqual(repos, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("permission denied", errors[0])


if __name__ == "__main__":
    unittest.main()
