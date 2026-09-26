from __future__ import annotations

from pathlib import Path
import unittest

from git_janitor.models import Finding
from git_janitor.scan_plans import (
    FINDING_CATEGORIES,
    MAX_SCAN_PLANS,
    SavedScanPlan,
    ScanPlanError,
    filter_findings,
    parse_scan_plans,
    require_scan_plan,
    select_project_paths,
)


class SavedScanPlanSelectionTests(unittest.TestCase):
    def test_finding_category_allowlist_matches_current_classifier_contract(self) -> None:
        self.assertEqual(
            FINDING_CATEGORIES,
            {
                "branch-without-upstream",
                "dirty-worktree",
                "fetch-prune-failed",
                "green-draft-pr",
                "green-high-risk-pr",
                "green-mergeable-pr",
                "merged-local-branch",
                "pr-blocked-or-needs-review",
                "pr-conflicted",
                "pr-failing",
                "pr-inspection-warning",
                "pr-pending",
                "pr-stale-ci",
                "scanner-error",
                "stale-linked-worktree",
                "unpushed-commits",
            },
        )

    def test_parser_rejects_non_table_and_more_than_bounded_plan_count(self) -> None:
        with self.assertRaisesRegex(ScanPlanError, "TOML table"):
            parse_scan_plans([])

        raw_plan = {
            "project_ids": ["git-janitor"],
            "finding_categories": ["dirty-worktree"],
        }
        with self.assertRaisesRegex(ScanPlanError, "at most"):
            parse_scan_plans(
                {f"plan-{index}": raw_plan for index in range(MAX_SCAN_PLANS + 1)}
            )

    def test_project_selection_is_an_ordered_subset_of_discovered_paths(self) -> None:
        plan = _plan(project_ids=("sample-app", "git-janitor"))
        discovered = [Path("/tmp/other"), Path("/tmp/git-janitor"), Path("/tmp/sample-app")]

        selected = select_project_paths(
            plan,
            canonical_project_paths={
                "git-janitor": Path("/tmp/git-janitor"),
                "sample-app": Path("/tmp/sample-app"),
                "other": Path("/tmp/other"),
            },
            discovered_repo_paths=discovered,
        )

        self.assertEqual(selected, [Path("/tmp/sample-app").resolve(), Path("/tmp/git-janitor").resolve()])
        self.assertTrue(set(selected) < {path.resolve() for path in discovered})

    def test_project_selection_requires_every_canonical_project(self) -> None:
        plan = _plan(project_ids=("missing",))

        with self.assertRaisesRegex(ScanPlanError, "missing"):
            select_project_paths(
                plan,
                canonical_project_paths={"git-janitor": Path("/tmp/git-janitor")},
                discovered_repo_paths=[Path("/tmp/git-janitor")],
            )

    def test_project_selection_rejects_path_outside_discovered_scope(self) -> None:
        plan = _plan(project_ids=("git-janitor",))

        with self.assertRaisesRegex(ScanPlanError, "not in the discovered scan scope"):
            select_project_paths(
                plan,
                canonical_project_paths={"git-janitor": Path("/tmp/git-janitor")},
                discovered_repo_paths=[Path("/tmp/other")],
            )

    def test_project_selection_rejects_ambiguous_discovered_or_registry_paths(self) -> None:
        cases = (
            {
                "plan": _plan(project_ids=("git-janitor",)),
                "canonical": {"git-janitor": Path("/tmp/git-janitor")},
                "discovered": [Path("/tmp/git-janitor"), Path("/tmp/git-janitor/../git-janitor")],
            },
            {
                "plan": _plan(project_ids=("git-janitor", "alias")),
                "canonical": {
                    "git-janitor": Path("/tmp/git-janitor"),
                    "alias": Path("/tmp/git-janitor"),
                },
                "discovered": [Path("/tmp/git-janitor")],
            },
        )

        for case in cases:
            with self.subTest(case=case), self.assertRaisesRegex(ScanPlanError, "ambiguous"):
                select_project_paths(
                    case["plan"],
                    canonical_project_paths=case["canonical"],
                    discovered_repo_paths=case["discovered"],
                )

    def test_finding_filter_keeps_selected_and_unfilterable_evidence_gaps(self) -> None:
        plan = _plan(finding_categories=("merged-local-branch",))
        findings = [
            _finding("dirty-worktree"),
            _finding("scanner-error"),
            _finding("merged-local-branch"),
            _finding("fetch-prune-failed"),
            _finding("pr-inspection-warning"),
            _finding("pr-stale-ci"),
            _finding("green-mergeable-pr"),
        ]

        filtered = filter_findings(plan, findings)

        self.assertEqual(
            [finding.category for finding in filtered],
            [
                "scanner-error",
                "merged-local-branch",
                "fetch-prune-failed",
                "pr-inspection-warning",
                "pr-stale-ci",
            ],
        )

    def test_require_scan_plan_fails_closed_for_unknown_name(self) -> None:
        plan = _plan()

        self.assertIs(require_scan_plan({"review": plan}, "review"), plan)
        with self.assertRaisesRegex(ScanPlanError, "unknown saved scan plan"):
            require_scan_plan({"review": plan}, "missing")


def _plan(
    *,
    project_ids: tuple[str, ...] = ("git-janitor",),
    finding_categories: tuple[str, ...] = ("dirty-worktree",),
) -> SavedScanPlan:
    return SavedScanPlan(
        name="review",
        project_ids=project_ids,
        finding_categories=finding_categories,
    )


def _finding(category: str) -> Finding:
    return Finding(
        severity="medium",
        category=category,
        title=category,
        detail="fixture",
    )


if __name__ == "__main__":
    unittest.main()
