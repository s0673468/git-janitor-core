from __future__ import annotations

import json
import unittest
from pathlib import Path
import tempfile
from io import StringIO
from unittest.mock import patch

from git_janitor.delivery_queue import build_delivery_queue
from git_janitor.models import Finding, PullRequestState, RepoState, ScanReport
from git_janitor.report import render_json, render_markdown
from git_janitor.reproduce import main as reproduce_main, restore_report


class DeliveryQueueTests(unittest.TestCase):
    def report(self):
        return ScanReport(
            generated_at="2026-10-05T23:00:00+00:00",
            repos=[RepoState(path="/private/repo", name="repo", head_oid="a" * 40, fetch_prune_status="ok", default_ref="origin/main")],
            pull_requests=[PullRequestState(repo="owner/repo", number=1, title="Docs", url="https://github.com/owner/repo/pull/1", head_ref="docs", base_ref="main", is_draft=False, merge_state="CLEAN", review_decision=None, check_status="success", head_oid="b" * 40, author="owner")],
            findings=[
                Finding("low", "merged-local-branch", "Candidate", "ancestor only", repo_path="/private/repo"),
                Finding("high", "green-mergeable-pr", "Green", "reported checks succeeded", url="https://github.com/owner/repo/pull/1"),
                Finding("high", "dirty-worktree", "Dirty", "1 modified", repo_path="/private/repo"),
                Finding("high", "scanner-error", "Partial", "branch inspection denied", repo_path="/private/repo"),
            ],
        )

    def test_gq15_queue_ranks_evidence_preservation_delivery_then_cleanup(self):
        queue = build_delivery_queue(self.report())
        self.assertEqual([item.category for item in queue], ["scanner-error", "dirty-worktree", "green-mergeable-pr", "merged-local-branch"])
        self.assertEqual([item.rank for item in queue], [1, 2, 3, 4])
        self.assertTrue(all("Inspection only" in item.authorization for item in queue))
        self.assertTrue(all(item.unknowns and item.preservation for item in queue))
        self.assertIn("required exact-head gate and receipt", queue[2].unknowns)
        self.assertIn("b" * 40, queue[2].evidence[1])
        self.assertIn("a" * 40, queue[1].evidence[1])

    def test_gq15_reports_reproduce_without_mutating_input(self):
        report = self.report()
        before = report.to_dict()
        first_json, first_markdown = render_json(report), render_markdown(report)
        self.assertEqual(render_json(report), first_json)
        self.assertEqual(render_markdown(report), first_markdown)
        self.assertEqual(report.to_dict(), before)
        payload = json.loads(first_json)
        self.assertEqual(len(payload["delivery_queue"]), 4)
        self.assertIn("/findings/2", payload["delivery_queue"][1]["evidence"][0])
        self.assertIn("## Ranked Delivery and Hygiene Queue", first_markdown)
        self.assertIn("unknown ownership and publication are preserved", first_markdown)
        with patch("git_janitor.git.run_command", side_effect=AssertionError("Snapshot replay must stay offline")):
            restored = restore_report(payload)
            self.assertEqual(render_json(restored), first_json)
            self.assertEqual(render_markdown(restored), first_markdown)

    def test_gq15_empty_inventory_with_auth_error_has_evidence_queue(self):
        report = ScanReport(generated_at="fixed", repos=[], pull_requests=[], findings=[], errors=["gh auth unavailable"])
        item = build_delivery_queue(report)[0]
        self.assertEqual(item.status, "coverage-gap")
        self.assertEqual(item.evidence, ("/errors/0: gh auth unavailable",))
        self.assertNotIn("No forgotten", render_markdown(report))

    def test_gq15_replay_preserves_original_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "original.json"
            content = render_json(self.report())
            snapshot.write_text(content)
            with patch("sys.stderr", StringIO()), self.assertRaises(SystemExit):
                reproduce_main([str(snapshot), "--out", str(snapshot)])
            self.assertEqual(snapshot.read_text(), content)


if __name__ == "__main__":
    unittest.main()
