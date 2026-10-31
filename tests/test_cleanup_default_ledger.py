"""CLI audit output stays outside an otherwise clean target checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src"
CLI = """
from unittest.mock import patch
import sys
from git_janitor import safe_delete
report = safe_delete.DeleteReport(
    branch="old-docs", repo="example/project", default_ref="origin/main",
    merge_commit="a" * 40, command=["git", "branch", "-d", "old-docs"],
    proofs=(safe_delete.Proof("fixture", True, "synthetic merge proof"),))
with patch.object(safe_delete, "evaluate_delete", return_value=report):
    raise SystemExit(safe_delete.git_safe_delete_main(sys.argv[1:]))
"""


class DefaultLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.home, self.repo = base / "home", base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        self.default = self.home / ".local/state/git-janitor/branch-cleanup-audit.jsonl"

    def run_cli(self, *extra):
        environment = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(SOURCE),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            [sys.executable, "-B", "-c", CLI, "old-docs", "--repo", str(self.repo), *extra],
            cwd=self.repo, env=environment, text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def assert_checkout_clean(self):
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo,
                                check=True, text=True, capture_output=True).stdout
        self.assertEqual(status, "")

    def test_default_dry_run_writes_audit_without_dirtying_target_checkout(self):
        self.run_cli()
        self.assert_checkout_clean()
        record = json.loads(self.default.read_text())
        self.assertEqual(record["status"], "dry-run")
        self.assertEqual(record["branch"], "old-docs")

    def test_explicit_ledger_path_still_wins(self):
        custom = self.home / "chosen-audit.jsonl"
        self.run_cli("--ledger", str(custom))
        self.assert_checkout_clean()
        self.assertEqual(json.loads(custom.read_text())["status"], "dry-run")
        self.assertFalse(self.default.exists())


if __name__ == "__main__":
    unittest.main()
