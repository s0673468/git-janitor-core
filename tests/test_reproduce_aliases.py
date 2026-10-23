"""Offline replay must preserve snapshot bytes through filesystem aliases."""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_janitor.models import ScanReport
from git_janitor.report import render_json, render_markdown
from git_janitor.reproduce import main


class ReproduceAliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="janitor-replay-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.report = ScanReport("2026-10-23T12:00:00+00:00", [], [], [])
        self.snapshot = self.root / "snapshot.json"
        self.original = render_json(self.report).encode("utf-8")
        self.snapshot.write_bytes(self.original)
        self.markdown = self.root / "report.md"
        self.json_output = self.root / "report.json"

    def assert_refused(self, expected_error="paths must be distinct"):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            main([str(self.snapshot), "--out", str(self.markdown),
                  "--json-out", str(self.json_output)])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn(expected_error, stderr.getvalue())
        self.assertEqual(self.snapshot.read_bytes(), self.original)

    def test_markdown_hard_link_to_snapshot_is_refused_before_writes(self):
        os.link(self.snapshot, self.markdown)

        self.assert_refused()

        self.assertEqual(self.markdown.read_bytes(), self.original)
        self.assertFalse(self.json_output.exists())

    def test_json_hard_link_to_snapshot_is_refused_before_writes(self):
        os.link(self.snapshot, self.json_output)

        self.assert_refused()

        self.assertEqual(self.json_output.read_bytes(), self.original)
        self.assertFalse(self.markdown.exists())

    def test_hard_linked_outputs_are_refused_before_either_is_changed(self):
        self.markdown.write_bytes(b"synthetic earlier output\n")
        os.link(self.markdown, self.json_output)

        self.assert_refused()

        self.assertEqual(self.markdown.read_bytes(), b"synthetic earlier output\n")
        self.assertEqual(self.json_output.read_bytes(), b"synthetic earlier output\n")

    def test_symlink_to_snapshot_remains_refused(self):
        self.markdown.symlink_to(self.snapshot)

        self.assert_refused()

        self.assertFalse(self.json_output.exists())

    def test_distinct_outputs_are_rendered_offline_and_snapshot_is_unchanged(self):
        with patch("git_janitor.git.run_command", side_effect=AssertionError("replay must stay offline")):
            code = main([str(self.snapshot), "--out", str(self.markdown),
                         "--json-out", str(self.json_output)])

        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot.read_bytes(), self.original)
        self.assertEqual(self.markdown.read_text(), render_markdown(self.report))
        self.assertEqual(self.json_output.read_text(), render_json(self.report) + "\n")

    def test_distinct_existing_outputs_can_be_replaced(self):
        self.markdown.write_text("synthetic earlier markdown\n")
        self.json_output.write_text("synthetic earlier json\n")

        code = main([str(self.snapshot), "--out", str(self.markdown),
                     "--json-out", str(self.json_output)])

        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot.read_bytes(), self.original)
        self.assertEqual(self.markdown.read_text(), render_markdown(self.report))
        self.assertEqual(self.json_output.read_text(), render_json(self.report) + "\n")

    def test_unavailable_file_identity_refuses_before_writes(self):
        with patch.object(Path, "samefile", side_effect=PermissionError("synthetic identity failure")):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main([str(self.snapshot), "--out", str(self.markdown)])

        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.snapshot.read_bytes(), self.original)
        self.assertFalse(self.markdown.exists())

    def test_symlink_loop_refuses_before_writes(self):
        self.markdown.symlink_to(self.markdown)
        self.assert_refused("Cannot establish distinct snapshot and output paths")
        self.assertFalse(self.json_output.exists())
