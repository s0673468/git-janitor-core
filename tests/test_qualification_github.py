from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from git_janitor.classify import classify_pr
from git_janitor.config import ScannerConfig
from git_janitor.github import list_open_pull_requests, list_pull_requests
from git_janitor.models import CommandResult


def raw_pr():
    return {
        "number": 12, "title": "Small docs change",
        "url": "https://github.com/owner/repo/pull/12",
        "headRefName": "codex/docs", "headRefOid": "a" * 40,
        "baseRefName": "main", "isDraft": False,
        "mergeStateStatus": "CLEAN", "reviewDecision": None,
        "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        "author": {"login": "owner"},
    }


class GithubQualificationTests(unittest.TestCase):
    def collect(self, payload, *, view=None, error=False, all_authors=False):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["gh", "pr", "list"]:
                return CommandResult(args, int(error), json.dumps(payload), "denied" if error else "")
            if args[:3] == ["gh", "pr", "view"]:
                return CommandResult(args, 0, json.dumps(view or {
                    "files": [], "changedFiles": 0, "headRefOid": "a" * 40,
                }), "")
            self.fail(f"Unexpected API command {args}")

        with patch("git_janitor.github.run_command", side_effect=run):
            collect = list_open_pull_requests if all_authors else list_pull_requests
            result = collect("owner/repo", ScannerConfig(github_author=""))
        return result, calls

    def test_gq10_head_movement_during_enrichment_cannot_look_green(self):
        (prs, errors), _ = self.collect([raw_pr()], view={
            "files": [], "changedFiles": 0, "headRefOid": "b" * 40,
        })
        self.assertEqual(errors, [])
        self.assertEqual(prs[0].head_oid, "a" * 40)
        self.assertTrue(prs[0].errors)
        categories = {f.category for f in classify_pr(prs[0], ScannerConfig())}
        self.assertIn("pr-inspection-warning", categories)
        self.assertNotIn("green-mergeable-pr", categories)

    def test_gq10_snapshot_retains_observed_head_and_author(self):
        (prs, errors), calls = self.collect([raw_pr()])
        self.assertEqual(errors, [])
        self.assertEqual(prs[0].head_oid, "a" * 40)
        self.assertEqual(prs[0].author, "owner")
        self.assertIn("headRefOid", calls[0][calls[0].index("--json") + 1])
        self.assertIn("--limit", calls[0])

    def test_gq13_malformed_response_is_incomplete_not_empty_or_crash(self):
        for payload in ({}, [None], [{}], [{"number": "not-an-integer"}]):
            with self.subTest(payload=payload):
                (prs, errors), _ = self.collect(payload)
                self.assertEqual(prs, [])
                self.assertTrue(errors)

    def test_gq13_partial_response_retains_valid_pr_and_gap(self):
        (prs, errors), _ = self.collect([raw_pr(), None])
        self.assertEqual([pr.number for pr in prs], [12])
        self.assertTrue(errors)

    def test_gq12_auth_failure_has_visible_collection_error(self):
        (prs, errors), _ = self.collect([], error=True)
        self.assertEqual(prs, [])
        self.assertIn("denied", errors[0])

    def test_gq17_dependency_actor_alias_stays_visible_but_not_actionable(self):
        human = {**raw_pr(), "headRefName": "dependabot/manual-human-work"}
        for login in ("dependabot[bot]", "app/dependabot"):
            bot = {**raw_pr(), "number": 13, "author": {"login": login}}
            with self.subTest(login=login):
                (authored, errors), calls = self.collect([human, bot])
                self.assertEqual(errors, [])
                self.assertEqual([pr.number for pr in authored], [12])
                self.assertEqual(len(calls), 2)  # Only the human PR is enriched.
                (visible, errors), _ = self.collect([human, bot], all_authors=True)
                self.assertEqual(errors, [])
                self.assertEqual([pr.number for pr in visible], [12, 13])
                self.assertEqual(visible[1].author, login)


if __name__ == "__main__":
    unittest.main()
