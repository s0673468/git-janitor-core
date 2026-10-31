"""Portable publication contract, independent of branch naming and CLI policy."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from git_janitor.preservation_publication import publish_pair, recover_pair


class PreservationPublicationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(prefix="preservation-publication-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Publication Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", os.devnull)
        (self.repo / "work.txt").write_text("synthetic saved work\n")
        self.git("add", "work.txt")
        self.git("commit", "-m", "synthetic work")
        self.head = self.git("rev-parse", "HEAD")
        self.output = self.root / "preserved"
        self.output.mkdir()
        # An earlier uniquely named snapshot must survive every new attempt.
        previous = self.output / "snapshot-previous.bundle"
        self.git("bundle", "create", str(previous), "refs/heads/main")
        previous.with_suffix(".md").write_text(f"- commit: {self.head}\n")
        self.previous = {p: p.read_bytes() for p in self.output.iterdir()}
        self.bundle = self.output / "snapshot-candidate.bundle"
        self.manifest = self.bundle.with_suffix(".md")
        # The journal identity need not depend on the freshly allocated filenames.
        self.journal = self.output / ".main.pending"
        self.staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.output))
        staged_bundle = self.staging / self.bundle.name
        self.git("bundle", "create", str(staged_bundle), "refs/heads/main")
        self.git("bundle", "verify", str(staged_bundle))
        (self.staging / self.manifest.name).write_text(f"- commit: {self.head}\n")

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.repo, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    @contextmanager
    def locked(self):
        descriptor = os.open(self.output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)

    def publish(self):
        with self.locked():
            publish_pair(self.staging, self.journal, self.bundle, self.manifest)

    def recover(self):
        with self.locked():
            recover_pair(self.journal, self.bundle, self.manifest)

    def assert_previous_unchanged(self):
        for path, original in self.previous.items():
            self.assertEqual(path.read_bytes(), original)

    def assert_restorable_pair(self):
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.manifest.read_text(), f"- commit: {self.head}\n")
        # Verify and restore without borrowing objects from the original repo.
        with tempfile.TemporaryDirectory(dir=self.root) as restore:
            destination = Path(restore)
            self.git("init", "--bare", restore)
            self.git("bundle", "verify", str(self.bundle), cwd=destination)
            self.git("bundle", "unbundle", str(self.bundle), cwd=destination)
            self.git("cat-file", "-e", f"{self.head}^{{commit}}", cwd=destination)
        self.assertEqual(
            set(self.output.iterdir()), set(self.previous) | {self.bundle, self.manifest},
        )
        self.assert_previous_unchanged()

    def interrupt(self, destination, *, recovering=False):
        # Real process death skips finally blocks and releases the directory lock.
        script = textwrap.dedent("""\
            import fcntl, os, shutil, sys
            from pathlib import Path
            from unittest.mock import patch
            from git_janitor.preservation_publication import publish_pair, recover_pair

            output, staging = map(Path, sys.argv[1:3])
            destination, recovering = sys.argv[3:5]
            bundle = output / 'snapshot-candidate.bundle'
            manifest = bundle.with_suffix('.md')
            journal = output / '.main.pending'
            original_replace, original_cleanup = Path.replace, shutil.rmtree

            def interrupted_replace(source, target):
                result = original_replace(source, target)
                if Path(target) == (bundle if destination == 'bundle' else manifest):
                    os._exit(79)
                return result

            def interrupted_cleanup(path, *args, **kwargs):
                if Path(path) == journal:
                    os._exit(79)
                return original_cleanup(path, *args, **kwargs)

            descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            hook = (patch.object(shutil, 'rmtree', interrupted_cleanup)
                    if destination == 'cleanup'
                    else patch.object(Path, 'replace', interrupted_replace))
            with hook:
                if recovering == 'True':
                    recover_pair(journal, bundle, manifest)
                else:
                    publish_pair(staging, journal, bundle, manifest)
            raise AssertionError('expected interruption')
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.output), str(self.staging),
             destination, str(recovering)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertTrue(self.journal.is_dir())

    def test_unique_pair_recovers_after_source_branch_deleted(self):
        self.interrupt("bundle")
        self.git("update-ref", "-d", "refs/heads/main")
        self.recover()
        self.assert_restorable_pair()

    def test_repeated_process_death_and_cleanup_death_recover(self):
        self.interrupt("bundle")
        self.interrupt("manifest", recovering=True)
        self.interrupt("cleanup", recovering=True)
        self.recover()
        self.assert_restorable_pair()

    def test_replacement_error_keeps_journal_until_retry(self):
        original_replace = Path.replace

        def fail_manifest(source, target):
            if Path(target) == self.manifest:
                raise OSError("synthetic publication failure")
            return original_replace(source, target)

        with patch.object(Path, "replace", fail_manifest), self.assertRaises(OSError):
            self.publish()
        self.assertTrue(self.journal.is_dir())
        self.assert_previous_unchanged()
        self.recover()
        self.assert_restorable_pair()

    def test_fsync_failure_does_not_publish_unrecoverable_outputs(self):
        with patch.object(os, "fsync", side_effect=OSError("synthetic sync failure")):
            with self.assertRaises(OSError):
                self.publish()
        self.assertFalse(self.journal.exists())
        self.assertFalse(self.bundle.exists())
        self.assertFalse(self.manifest.exists())
        self.assert_previous_unchanged()
        self.publish()
        self.assert_restorable_pair()

    def test_aliases_are_replaced_without_changing_unrelated_files(self):
        unrelated_bundle = self.root / "unrelated.bundle"
        unrelated_manifest = self.root / "unrelated.md"
        unrelated_bundle.write_bytes(b"unrelated saved work\n")
        unrelated_manifest.write_text("unrelated notes\n")
        os.link(unrelated_bundle, self.bundle)
        self.manifest.symlink_to(unrelated_manifest)
        self.publish()
        self.assertEqual(unrelated_bundle.read_bytes(), b"unrelated saved work\n")
        self.assertEqual(unrelated_manifest.read_text(), "unrelated notes\n")
        self.assertFalse(self.bundle.samefile(unrelated_bundle))
        self.assertFalse(self.manifest.is_symlink())
        self.assert_restorable_pair()
