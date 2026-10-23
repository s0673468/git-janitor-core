"""Synthetic ref races and process-death recovery for branch preservation."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from git_janitor.git import run_command
from git_janitor.safe_delete import preserve_branch


class PreservationRaceTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(prefix="janitor-preservation-races-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Preservation Race Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", os.devnull)
        self.base = self.commit("base\n")
        self.output = self.root / "preserved"
        self.bundle = self.output / "repo-main.bundle"
        self.manifest = self.output / "repo-main.md"
        self.journal = self.output / "repo-main.preserve-journal"

    def git(self, *args, cwd=None):
        result = run_command(["git", *args], cwd or self.repo, 30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def commit(self, text):
        (self.repo / "work.txt").write_text(text, encoding="utf-8")
        self.git("add", "work.txt")
        self.git("commit", "-m", "synthetic work")
        return self.git("rev-parse", "HEAD")

    def preserve(self, runner=run_command):
        return preserve_branch(
            "main", repo_path=self.repo, reason="synthetic race",
            validation="offline regression", output_dir=self.output, runner=runner,
        )

    def saved_copy(self):
        saved = self.commit("saved history\n")
        result = self.preserve()
        self.assertTrue(result["ok"], result)
        return saved, self.bundle.read_bytes(), self.manifest.read_bytes()

    def assert_pair(self, head, *retained):
        self.assertEqual(
            self.git("bundle", "list-heads", str(self.bundle)),
            f"{head} refs/heads/main",
        )
        self.assertIn(f"- commit: {head}\n", self.manifest.read_text())
        # Restore independently so unreachable source objects cannot hide an
        # incomplete backup. All repositories and identities are synthetic.
        with tempfile.TemporaryDirectory(dir=self.root) as restore:
            self.git("init", "--bare", restore)
            self.git("bundle", "verify", str(self.bundle), cwd=Path(restore))
            self.git("bundle", "unbundle", str(self.bundle), cwd=Path(restore))
            for commit in (head, *retained):
                self.git("cat-file", "-e", f"{commit}^{{commit}}", cwd=Path(restore))
        self.assertEqual(set(self.output.iterdir()), {self.bundle, self.manifest})

    def test_rewrite_after_ancestry_check_cannot_replace_saved_history(self):
        saved, bundle_bytes, manifest_bytes = self.saved_copy()
        self.commit("fast-forward candidate\n")

        def rewrite_before_create(args, cwd, timeout):
            if args[:3] == ["git", "bundle", "create"]:
                self.git("update-ref", "refs/heads/main", self.base)
            return run_command(args, cwd, timeout)

        result = self.preserve(rewrite_before_create)

        self.assertFalse(result["ok"], result)
        self.assertIn("branch moved", result["error"])
        self.assertEqual(self.bundle.read_bytes(), bundle_bytes)
        self.assertEqual(self.manifest.read_bytes(), manifest_bytes)
        self.assert_pair(saved)

    def test_rewrite_after_bundle_verification_fails_compare_and_swap(self):
        saved, bundle_bytes, manifest_bytes = self.saved_copy()
        self.commit("fast-forward candidate\n")
        comparisons = []

        def rewrite_before_cas(args, cwd, timeout):
            if args[:2] == ["git", "update-ref"]:
                self.git("update-ref", "refs/heads/main", self.base)
                result = run_command(args, cwd, timeout)
                comparisons.append(result.returncode)
                return result
            return run_command(args, cwd, timeout)

        result = self.preserve(rewrite_before_cas)

        self.assertFalse(result["ok"], result)
        self.assertEqual(len(comparisons), 1)
        self.assertNotEqual(comparisons[0], 0)
        self.assertIn("branch moved", result["error"])
        self.assertEqual(self.bundle.read_bytes(), bundle_bytes)
        self.assertEqual(self.manifest.read_bytes(), manifest_bytes)
        self.assert_pair(saved)

    def test_rewrite_after_compare_and_swap_keeps_the_verified_commit(self):
        saved, _bundle_bytes, _manifest_bytes = self.saved_copy()
        head = self.commit("fast-forward candidate\n")

        def rewrite_after_cas(args, cwd, timeout):
            result = run_command(args, cwd, timeout)
            if args[:2] == ["git", "update-ref"]:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.git("update-ref", "refs/heads/main", self.base)
            return result

        result = self.preserve(rewrite_after_cas)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["commit"], head)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assert_pair(head, saved)

    def test_same_named_tag_cannot_select_different_history(self):
        head = self.commit("branch work\n")
        self.git("tag", "main", self.base)

        result = self.preserve()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["commit"], head)
        self.assert_pair(head)

    def interrupt_publication(self, *, destination="bundle", during_cleanup=False):
        # os._exit skips finally blocks, exercising kernel lock release and the
        # durable journal rather than exception-based rollback.
        script = textwrap.dedent("""\
            import os
            from pathlib import Path
            import sys
            from unittest.mock import patch
            from git_janitor.safe_delete import preserve_branch

            repo, output = map(Path, sys.argv[1:3])
            destination, during_cleanup = sys.argv[3:5]
            original_replace = Path.replace

            def interrupted_replace(source, target):
                result = original_replace(source, target)
                suffix = '.bundle' if destination == 'bundle' else '.md'
                if Path(target) == output / ('repo-main' + suffix):
                    os._exit(79)
                return result

            def interrupted_cleanup(path, *args, **kwargs):
                if Path(path) == output / 'repo-main.preserve-journal':
                    os._exit(79)
                return original_cleanup(path, *args, **kwargs)

            import shutil
            original_cleanup = shutil.rmtree
            if during_cleanup == 'True':
                hook = patch.object(shutil, 'rmtree', interrupted_cleanup)
            else:
                hook = patch.object(Path, 'replace', interrupted_replace)
            with hook:
                result = preserve_branch(
                    'main', repo_path=repo, output_dir=output,
                    reason='synthetic interruption', validation='offline',
                )
            raise AssertionError(result)
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.repo), str(self.output),
             destination, str(during_cleanup)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertTrue(self.journal.is_dir())

    def test_process_death_between_renames_recovers_before_rewrite_refusal(self):
        saved, _bundle_bytes, manifest_bytes = self.saved_copy()
        head = self.commit("fast-forward candidate\n")

        self.interrupt_publication()

        self.assertEqual(self.manifest.read_bytes(), manifest_bytes)
        self.assertEqual(
            self.git("bundle", "list-heads", str(self.bundle)), f"{head} refs/heads/main",
        )
        for name in (self.bundle.name, self.manifest.name):
            self.assertTrue((self.journal / name).is_file())
        self.git("update-ref", "refs/heads/main", self.base)
        result = self.preserve()

        self.assertFalse(result["ok"], result)
        self.assertIn("refusing to overwrite", result["error"])
        self.assert_pair(head, saved)

    def test_first_publication_recovers_even_if_branch_was_deleted(self):
        head = self.commit("first preservation\n")
        self.interrupt_publication()
        self.assertFalse(self.manifest.exists())
        self.git("update-ref", "-d", "refs/heads/main")

        result = self.preserve()

        self.assertFalse(result["ok"], result)
        self.assert_pair(head)

    def test_repeated_interruptions_are_recoverable(self):
        saved, _bundle_bytes, _manifest_bytes = self.saved_copy()
        head = self.commit("fast-forward candidate\n")
        self.interrupt_publication()
        self.interrupt_publication(destination="manifest")
        self.git("update-ref", "refs/heads/main", self.base)

        result = self.preserve()

        self.assertFalse(result["ok"], result)
        self.assert_pair(head, saved)

    def test_process_death_during_journal_cleanup_is_recoverable(self):
        saved, _bundle_bytes, _manifest_bytes = self.saved_copy()
        head = self.commit("fast-forward candidate\n")
        self.interrupt_publication(during_cleanup=True)
        self.assertFalse((self.journal / self.manifest.name).exists())
        self.git("update-ref", "refs/heads/main", self.base)

        result = self.preserve()

        self.assertFalse(result["ok"], result)
        self.assert_pair(head, saved)

    def test_rename_error_retains_journal_for_next_run(self):
        saved, _bundle_bytes, _manifest_bytes = self.saved_copy()
        head = self.commit("fast-forward candidate\n")
        original_replace = Path.replace

        def fail_manifest(source, target):
            if Path(target) == self.manifest:
                raise OSError("synthetic publication failure")
            return original_replace(source, target)

        with patch.object(Path, "replace", fail_manifest):
            result = self.preserve()

        self.assertFalse(result["ok"], result)
        self.assertIn("retry to recover", result["error"])
        self.assertTrue(self.journal.is_dir())
        result = self.preserve()
        self.assertTrue(result["ok"], result)
        self.assert_pair(head, saved)

    def test_legacy_lock_file_still_blocks_preservation(self):
        self.output.mkdir()
        lock = self.output / "repo-main.preserve-lock"
        lock.touch()

        result = self.preserve()

        self.assertFalse(result["ok"], result)
        self.assertIn("in progress", result["error"])
        self.assertEqual(list(self.output.iterdir()), [lock])
