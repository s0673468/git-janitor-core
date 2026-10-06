from __future__ import annotations

from datetime import datetime, timezone
import unittest

from git_janitor.classify import (
    classify_pr,
    classify_repo,
    classify_report,
    pr_risk_reasons,
)
from git_janitor.config import ScannerConfig
from git_janitor.models import (
    BranchState,
    LinkedWorktreeState,
    PullRequestState,
    RepoState,
)


class ClassifyPrTests(unittest.TestCase):
    def _pr(self, **overrides: object) -> PullRequestState:
        values = {
            "repo": "owner/repo",
            "number": 10,
            "title": "Feature",
            "url": "https://github.com/owner/repo/pull/10",
            "head_ref": "feature",
            "base_ref": "main",
            "is_draft": False,
            "merge_state": "CLEAN",
            "review_decision": None,
            "check_status": "success",
        }
        values.update(overrides)
        return PullRequestState(**values)

    def test_green_clean_pr_is_mergeable(self) -> None:
        pr = PullRequestState(
            repo="owner/repo",
            number=1,
            title="Small docs fix",
            url="https://github.com/owner/repo/pull/1",
            head_ref="docs-fix",
            base_ref="main",
            is_draft=False,
            merge_state="CLEAN",
            review_decision=None,
            check_status="success",
        )

        findings = classify_pr(pr, ScannerConfig())

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "green-mergeable-pr")

    def test_high_risk_green_pr_is_not_plain_mergeable(self) -> None:
        pr = PullRequestState(
            repo="owner/repo",
            number=2,
            title="Change supabase sync behavior",
            url=None,
            head_ref="sync-change",
            base_ref="main",
            is_draft=False,
            merge_state="CLEAN",
            review_decision=None,
            check_status="success",
            changed_files=["scripts/sync_to_cloud.py"],
        )
        config = ScannerConfig(high_risk_patterns=["supabase", "sync"])

        findings = classify_pr(pr, config)

        self.assertEqual(findings[0].category, "green-high-risk-pr")
        self.assertIn("sync", pr.risk_reasons)

    def test_built_in_deploy_risk_does_not_need_config_pattern(self) -> None:
        pr = PullRequestState(
            repo="owner/repo",
            number=4,
            title="Update deploy script",
            url=None,
            head_ref="deploy-update",
            base_ref="main",
            is_draft=False,
            merge_state="CLEAN",
            review_decision=None,
            check_status="success",
            changed_files=["scripts/deploy.py"],
        )

        findings = classify_pr(pr, ScannerConfig())

        self.assertEqual(findings[0].category, "green-high-risk-pr")
        self.assertIn("deploy protected change", pr.risk_reasons)

    def test_green_draft_is_ready_attention(self) -> None:
        pr = PullRequestState(
            repo="owner/repo",
            number=3,
            title="Feature",
            url=None,
            head_ref="feature",
            base_ref="main",
            is_draft=True,
            merge_state="DRAFT",
            review_decision=None,
            check_status="success",
        )

        findings = classify_pr(pr, ScannerConfig())

        self.assertEqual(findings[0].category, "green-draft-pr")

    def test_pr_errors_block_green_classification(self) -> None:
        pr = self._pr(errors=["files unavailable", "checks partial"])

        findings = classify_pr(pr, ScannerConfig())

        self.assertEqual(
            [finding.category for finding in findings],
            ["pr-inspection-warning"],
        )
        self.assertEqual(findings[0].severity, "medium")
        self.assertEqual(findings[0].detail, "files unavailable; checks partial")
        self.assertIn("owner/repo#10: Feature", findings[0].title)
        self.assertEqual(findings[0].url, "https://github.com/owner/repo/pull/10")

    def test_stale_failure_and_conflict_prs_short_circuit(self) -> None:
        cases = [
            (
                self._pr(check_status="stale", merge_state="UNKNOWN"),
                "pr-stale-ci",
                "medium",
                "Refresh or rerun CI before making a merge decision.",
            ),
            (
                self._pr(check_status="failure", merge_state="CLEAN"),
                "pr-failing",
                "high",
                "Fix CI or inspect the failing check logs.",
            ),
            (
                self._pr(check_status="success", merge_state="DIRTY"),
                "pr-conflicted",
                "high",
                "Rebase or merge the base branch and resolve conflicts.",
            ),
        ]

        for pr, category, severity, action in cases:
            with self.subTest(category=category):
                findings = classify_pr(pr, ScannerConfig())

                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].category, category)
                self.assertEqual(findings[0].severity, severity)
                self.assertEqual(findings[0].recommended_action, action)
                self.assertIn("owner/repo#10: Feature", findings[0].title)

    def test_pending_and_blocked_prs_are_classified(self) -> None:
        pending = self._pr(check_status="pending", merge_state="CLEAN")
        blocked = self._pr(
            check_status="skipped",
            merge_state="BLOCKED",
            review_decision="CHANGES_REQUESTED",
        )

        pending_findings = classify_pr(pending, ScannerConfig())
        blocked_findings = classify_pr(blocked, ScannerConfig())

        self.assertEqual(pending_findings[0].category, "pr-pending")
        self.assertEqual(pending_findings[0].severity, "low")
        self.assertEqual(
            pending_findings[0].recommended_action,
            "No action unless it remains pending for a long time.",
        )
        self.assertEqual(blocked_findings[0].category, "pr-blocked-or-needs-review")
        self.assertEqual(blocked_findings[0].severity, "medium")
        self.assertEqual(
            blocked_findings[0].detail,
            "checks=skipped, mergeState=BLOCKED, review=CHANGES_REQUESTED.",
        )

    def test_green_high_risk_detail_includes_size_and_sorted_risks(self) -> None:
        pr = self._pr(
            title="Update CI",
            head_ref="sync-token",
            changed_files=[
                ".github/workflows/test.yml",
                "scripts/sync/token_manager.py",
            ],
            additions=12,
            deletions=3,
        )

        findings = classify_pr(pr, ScannerConfig())

        self.assertEqual(findings[0].category, "green-high-risk-pr")
        self.assertEqual(findings[0].severity, "medium")
        self.assertEqual(
            pr.risk_reasons,
            [
                ".github workflow/config change",
                "ci protected change",
                "sync protected change",
                "token protected change",
            ],
        )
        self.assertEqual(
            findings[0].detail,
            "checks=success, mergeState=CLEAN, review=unknown, +12/-3, "
            "risk_candidates=.github workflow/config change, ci protected change, "
            "sync protected change, token protected change.",
        )

    def test_risk_reasons_use_boundaries_and_config_patterns(self) -> None:
        pr = self._pr(
            title="official docs",
            head_ref="feature/access-token",
            check_status="pending",
            changed_files=["docs/topic.md"],
        )

        reasons = pr_risk_reasons(pr, ScannerConfig(high_risk_patterns=["docs"]))

        self.assertEqual(
            reasons,
            ["access protected change", "docs", "token protected change"],
        )
        self.assertNotIn("ci protected change", reasons)


class ClassifyRepoTests(unittest.TestCase):
    def test_repo_errors_are_high_scanner_errors(self) -> None:
        repo = RepoState(
            path="/repo",
            name="repo",
            errors=["fetch failed", "parse failed"],
        )

        findings = classify_repo(repo, ScannerConfig(), datetime.now(timezone.utc))

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "scanner-error")
        self.assertEqual(findings[0].severity, "high")
        self.assertEqual(
            findings[0].title,
            "repo: scanner could not inspect repository cleanly",
        )
        self.assertEqual(findings[0].detail, "fetch failed; parse failed")
        self.assertEqual(findings[0].repo_path, "/repo")
        self.assertEqual(findings[0].recommended_action, "Inspect this repo manually.")

    def test_dirty_and_ahead_repo_generates_high_findings(self) -> None:
        repo = RepoState(
            path="/repo",
            name="repo",
            current_branch="codex/work",
            dirty_files=["src/app.py", "tests/test_app.py"],
            untracked_files=["scratch.txt"],
            ahead=3,
        )

        findings = classify_repo(repo, ScannerConfig(), datetime.now(timezone.utc))

        self.assertEqual(
            [finding.category for finding in findings],
            ["dirty-worktree", "unpushed-commits"],
        )
        self.assertEqual([finding.severity for finding in findings], ["high", "high"])
        self.assertEqual(
            findings[0].detail,
            "2 modified/staged files, 1 untracked files.",
        )
        self.assertEqual(
            findings[1].detail,
            "codex/work is ahead by 3 commit(s).",
        )

    def test_branch_findings_distinguish_merged_and_unpublished_work(self) -> None:
        repo = RepoState(
            path="/repo",
            name="repo",
            default_branch="main",
            default_ref="origin/main",
            branches=[
                BranchState(name="main", current=False, merged_to_default=True),
                BranchState(name="codex/current", current=True, unique_commit_count=5),
                BranchState(
                    name="codex/merged",
                    upstream="origin/codex/merged",
                    merged_to_default=True,
                    unique_commit_count=0,
                ),
                BranchState(name="codex/local-only", unique_commit_count=2),
            ],
        )

        findings = classify_repo(repo, ScannerConfig(), datetime.now(timezone.utc))

        self.assertEqual(
            [finding.category for finding in findings],
            ["merged-local-branch", "branch-without-upstream"],
        )
        self.assertEqual(findings[0].severity, "low")
        self.assertIn("codex/merged is an ancestor of origin/main.", findings[0].detail)
        self.assertEqual(findings[1].severity, "medium")
        self.assertIn("codex/local-only has 2 unique commit(s)", findings[1].detail)

    def test_fetch_prune_failure_is_reported(self) -> None:
        repo = RepoState(
            path="/repo",
            name="repo",
            fetch_prune_status="fatal: could not read Username",
        )

        findings = classify_repo(repo, ScannerConfig(), datetime.now(timezone.utc))

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "fetch-prune-failed")
        self.assertEqual(findings[0].severity, "medium")
        self.assertEqual(findings[0].detail, "fatal: could not read Username")
        self.assertEqual(
            findings[0].recommended_action,
            "Check network/authentication or run git fetch --prune origin manually.",
        )

    def test_stale_linked_worktree_reports_exact_safe_conditions(self) -> None:
        repo = RepoState(
            path="/repo",
            name="repo",
            default_ref="origin/main",
            linked_worktrees=[
                LinkedWorktreeState(
                    path="/tmp/repo-old",
                    branch="codex/old",
                    upstream="origin/codex/old",
                    upstream_gone=True,
                    default_ref="origin/main",
                    tree_matches_default=True,
                    unique_commit_count=0,
                )
            ],
        )

        findings = classify_repo(repo, ScannerConfig(), datetime.now(timezone.utc))

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "stale-linked-worktree")
        self.assertEqual(findings[0].severity, "low")
        self.assertIn("approval-required", findings[0].recommended_action or "")

    def test_stale_linked_worktree_with_diff_is_not_safe(self) -> None:
        repo = RepoState(
            path="/repo",
            name="repo",
            default_ref="origin/main",
            linked_worktrees=[
                LinkedWorktreeState(
                    path="/tmp/repo-garmin",
                    branch="codex/service-token-hardening-7",
                    upstream="origin/codex/service-token-hardening-7",
                    upstream_gone=True,
                    default_ref="origin/main",
                    tree_matches_default=False,
                    unique_commit_count=0,
                )
            ],
        )

        findings = classify_repo(repo, ScannerConfig(), datetime.now(timezone.utc))

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "stale-linked-worktree")
        self.assertEqual(findings[0].severity, "medium")
        self.assertIn("exact safe conditions were not met", findings[0].recommended_action or "")

    def test_stale_linked_worktree_detail_includes_errors_and_base(self) -> None:
        repo = RepoState(
            path="/repo",
            name="repo",
            default_ref="origin/trunk",
            linked_worktrees=[
                LinkedWorktreeState(
                    path="/tmp/repo-old",
                    branch=None,
                    upstream=None,
                    upstream_gone=True,
                    errors=["diff failed"],
                )
            ],
        )

        findings = classify_repo(repo, ScannerConfig(), datetime.now(timezone.utc))

        stale = next(finding for finding in findings if finding.category == "stale-linked-worktree")
        self.assertEqual(stale.severity, "medium")
        self.assertIn(
            "unknown branch tracks unknown upstream [gone]",
            stale.detail,
        )
        self.assertIn("tree_matches_origin/trunk=None", stale.detail)
        self.assertIn("Inspection errors: diff failed.", stale.detail)
        self.assertEqual(stale.repo_path, "/tmp/repo-old")

    def test_stale_linked_worktree_exact_conditions_are_all_required(self) -> None:
        base_values = {
            "path": "/tmp/repo-old",
            "branch": "codex/old",
            "upstream": "origin/codex/old",
            "upstream_gone": True,
            "tree_matches_default": True,
            "unique_commit_count": 0,
        }
        cases = [
            {"upstream_gone": False},
            {"errors": ["status failed"]},
            {"dirty_files": ["src/app.py"]},
            {"untracked_files": ["scratch.txt"]},
            {"tree_matches_default": False},
            {"tree_matches_default": None},
            {"unique_commit_count": 1},
            {"unique_commit_count": None},
        ]

        for override in cases:
            with self.subTest(override=override):
                values = dict(base_values)
                values.update(override)
                repo = RepoState(
                    path="/repo",
                    name="repo",
                    linked_worktrees=[LinkedWorktreeState(**values)],
                )

                findings = classify_repo(
                    repo,
                    ScannerConfig(),
                    datetime.now(timezone.utc),
                )

                if override == {"upstream_gone": False}:
                    self.assertEqual(findings, [])
                else:
                    stale = next(finding for finding in findings if finding.category == "stale-linked-worktree")
                    self.assertEqual(stale.severity, "medium")


class ClassifyReportTests(unittest.TestCase):
    def test_report_combines_and_sorts_repo_and_pr_findings(self) -> None:
        repo = RepoState(
            path="/repo",
            name="repo",
            dirty_files=["src/app.py"],
        )
        medium_pr = PullRequestState(
            repo="owner/repo",
            number=2,
            title="Update token handling",
            url=None,
            head_ref="token-update",
            base_ref="main",
            is_draft=False,
            merge_state="CLEAN",
            review_decision=None,
            check_status="success",
        )
        low_pr = PullRequestState(
            repo="owner/repo",
            number=3,
            title="Feature",
            url=None,
            head_ref="feature",
            base_ref="main",
            is_draft=False,
            merge_state="CLEAN",
            review_decision=None,
            check_status="pending",
        )

        findings = classify_report(
            [repo],
            [low_pr, medium_pr],
            ScannerConfig(),
            datetime(2026, 7, 3, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [(finding.severity, finding.category) for finding in findings],
            [
                ("high", "dirty-worktree"),
                ("medium", "green-high-risk-pr"),
                ("low", "pr-pending"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
