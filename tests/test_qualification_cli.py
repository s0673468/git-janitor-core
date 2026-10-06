from __future__ import annotations

from io import StringIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_janitor.cli import main


class CoverageExitTests(unittest.TestCase):
    def test_gq12_normal_scan_auth_failure_exits_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text("[scanner]\nscan_roots=[]\nfetch_prune=false\n")
            output = StringIO()
            with patch("git_janitor.cli.gh_available", return_value=False), patch("sys.stdout", output):
                self.assertEqual(main(["--config", str(config)]), 3)
            self.assertIn("coverage-gap", output.getvalue())
            self.assertNotIn("No forgotten", output.getvalue())

    def test_gq12_normal_scan_missing_root_cannot_look_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            missing = Path(directory) / "absent"
            config.write_text(f'[scanner]\nscan_roots=["{missing}"]\nfetch_prune=false\n')
            output = StringIO()
            with patch("git_janitor.cli.gh_available", return_value=True), patch("sys.stdout", output):
                self.assertEqual(main(["--config", str(config)]), 3)
            self.assertIn("configured scan root is missing", output.getvalue())
            self.assertNotIn("No forgotten", output.getvalue())


if __name__ == "__main__":
    unittest.main()
