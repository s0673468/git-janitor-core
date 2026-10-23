"""Preservation failures must leave synthetic saved work and unrelated files intact."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_janitor.git import run_command
from git_janitor.models import CommandResult
from git_janitor.safe_delete import preserve_branch


class PreservationFailureTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(prefix="janitor-preservation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Preservation Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", os.devnull)
        self.commit("base\n")
        self.output = self.root / "preserved"

    def git(self, *args):
        result = run_command(["git", *args], self.repo, 30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def commit(self, text):
        (self.repo / "work.txt").write_text(text, encoding="utf-8")
        self.git("add", "work.txt")
        self.git("commit", "-m", "synthetic work")

    def runner(self, args, cwd, timeout):
        self.assertEqual(args[0], "git")
        self.assertNotIn(args[1], {"fetch", "pull", "push"})
        return run_command(args, cwd, timeout)

    def preserve(self, runner=None):
        return preserve_branch(
            "main", repo_path=self.repo, reason="synthetic preservation",
            validation="offline regression", output_dir=self.output,
            runner=runner or self.runner,
        )

    def saved_copy(self):
        result = self.preserve()
        self.assertTrue(result["ok"], result)
        bundle, manifest = Path(result["bundle"]), Path(result["manifest"])
        return bundle, bundle.read_bytes(), manifest, manifest.read_bytes()

    def corrupt_candidate(self, args, cwd, timeout):
        result = self.runner(args, cwd, timeout)
        if args[:3] == ["git", "bundle", "create"] and result.returncode == 0:
            # Simulate damaged output between creation and Git's verification.
            Path(args[-2]).write_bytes(b"synthetic incomplete bundle\n")
        return result

    def test_failed_verification_keeps_previous_verified_bundle_and_manifest(self):
        bundle, bundle_bytes, manifest, manifest_bytes = self.saved_copy()
        self.commit("later work\n")

        result = self.preserve(self.corrupt_candidate)

        self.assertFalse(result["ok"], result)
        self.assertEqual(bundle.read_bytes(), bundle_bytes)
        self.assertEqual(manifest.read_bytes(), manifest_bytes)
        self.git("bundle", "verify", str(bundle))
        self.assertEqual(set(self.output.iterdir()), {bundle, manifest})

    def test_failed_first_verification_leaves_no_preservation_artifacts(self):
        result = self.preserve(self.corrupt_candidate)

        self.assertFalse(result["ok"], result)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_failed_bundle_create_keeps_previous_preservation(self):
        bundle, bundle_bytes, manifest, manifest_bytes = self.saved_copy()
        self.commit("later work\n")

        def failing_runner(args, cwd, timeout):
            if args[:3] == ["git", "bundle", "create"]:
                return CommandResult(args, 1, "", "synthetic creation failure")
            return self.runner(args, cwd, timeout)

        result = self.preserve(failing_runner)

        self.assertFalse(result["ok"], result)
        self.assertEqual(bundle.read_bytes(), bundle_bytes)
        self.assertEqual(manifest.read_bytes(), manifest_bytes)
        self.assertEqual(set(self.output.iterdir()), {bundle, manifest})

    def test_failed_manifest_write_keeps_previous_preservation(self):
        bundle, bundle_bytes, manifest, manifest_bytes = self.saved_copy()
        self.commit("later work\n")
        original_write = Path.write_text

        def failing_write(path, *args, **kwargs):
            if path.suffix == ".md":
                raise OSError("synthetic manifest write failure")
            return original_write(path, *args, **kwargs)

        with patch.object(Path, "write_text", failing_write):
            result = self.preserve()

        self.assertFalse(result["ok"], result)
        self.assertEqual(bundle.read_bytes(), bundle_bytes)
        self.assertEqual(manifest.read_bytes(), manifest_bytes)
        self.assertEqual(set(self.output.iterdir()), {bundle, manifest})

    def assert_manifest_alias_preserved(self, *, symlink):
        self.output.mkdir()
        unrelated = self.root / "unrelated.txt"
        unrelated.write_text("synthetic unrelated work\n", encoding="utf-8")
        manifest = self.output / "repo-main.md"
        if symlink:
            manifest.symlink_to(unrelated)
        else:
            os.link(unrelated, manifest)

        result = self.preserve()

        self.assertTrue(result["ok"], result)
        self.assertEqual(unrelated.read_text(), "synthetic unrelated work\n")
        self.assertFalse(manifest.is_symlink())
        self.assertFalse(manifest.samefile(unrelated))
        self.assertIn(f"- commit: {self.git('rev-parse', 'HEAD')}", manifest.read_text())
        bundle = Path(result["bundle"])
        self.git("bundle", "verify", str(bundle))
        self.assertEqual(set(self.output.iterdir()), {bundle, manifest})

    def test_manifest_symlink_cannot_overwrite_unrelated_file(self):
        self.assert_manifest_alias_preserved(symlink=True)

    def test_manifest_hard_link_cannot_overwrite_unrelated_file(self):
        self.assert_manifest_alias_preserved(symlink=False)

    def test_successful_fast_forward_replaces_bundle_and_manifest(self):
        bundle, bundle_bytes, manifest, _manifest_bytes = self.saved_copy()
        self.commit("later work\n")
        head = self.git("rev-parse", "HEAD")

        result = self.preserve()

        self.assertTrue(result["ok"], result)
        self.assertNotEqual(bundle.read_bytes(), bundle_bytes)
        self.assertEqual(self.git("bundle", "list-heads", str(bundle)), f"{head} refs/heads/main")
        self.assertIn(f"- commit: {head}", manifest.read_text())
        self.assertEqual(set(self.output.iterdir()), {bundle, manifest})
