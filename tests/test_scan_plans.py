from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
                "detached-worktree",
                "diverged-upstream",
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

    def test_GQ19_physical_alias_selects_discovered_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            canonical = root / "Health"
            canonical.mkdir()
            discovered = root / "health"

            with _mapped_directory_identity(discovered, canonical):
                self.assertTrue(canonical.samefile(discovered))
                self.assertNotEqual(canonical.resolve(), discovered.resolve())
                selected = select_project_paths(
                    _plan(project_ids=("health",)),
                    canonical_project_paths={"health": canonical},
                    discovered_repo_paths=[discovered],
                )

            self.assertEqual(selected, [discovered.resolve()])
            self.assertNotIn(canonical.resolve(), selected)

    def test_GQ19_physical_aliases_preserve_discovery_and_ownership_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            canonical = root / "Health"
            canonical.mkdir()
            alias = root / "health"
            cases = (
                (("health",), {"health": canonical}, [canonical, alias], "discovered path"),
                (("health",), {"health": canonical}, [alias, alias], "discovered path"),
                (
                    ("health", "health-alias"),
                    {"health": canonical, "health-alias": alias},
                    [alias],
                    "canonical path shared",
                ),
            )

            with _mapped_directory_identity(alias, canonical):
                for project_ids, registered, discovered, message in cases:
                    with self.subTest(message=message, discovered=discovered):
                        with self.assertRaisesRegex(ScanPlanError, "ambiguous " + message):
                            select_project_paths(
                                _plan(project_ids=project_ids),
                                canonical_project_paths=registered,
                                discovered_repo_paths=discovered,
                            )

    def test_GQ19_distinct_or_inaccessible_case_path_is_outside_scope(self) -> None:
        for inaccessible in (False, True):
            with self.subTest(inaccessible=inaccessible), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                canonical = root / "Health"
                canonical.mkdir()
                other = root / "other-checkout"
                other.mkdir()
                alias = root / "health"
                target = canonical if inaccessible else other

                with _mapped_directory_identity(alias, target, inaccessible=inaccessible):
                    if not inaccessible:
                        self.assertFalse(canonical.samefile(alias))
                    with self.assertRaisesRegex(ScanPlanError, "not in the discovered scan scope"):
                        select_project_paths(
                            _plan(project_ids=("health",)),
                            canonical_project_paths={"health": canonical},
                            discovered_repo_paths=[alias],
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


def _mapped_directory_identity(alias: Path, target: Path, *, inaccessible: bool = False):
    """Model casing identity independently of the test host's filesystem."""
    native_stat = os.stat

    def fixture_stat(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            candidate = Path(os.fsdecode(path))
            if candidate == alias or alias in candidate.parents:
                if inaccessible and candidate == alias:
                    raise PermissionError("fixture checkout identity unavailable")
                path = target / candidate.relative_to(alias)
        return native_stat(path, *args, **kwargs)

    return patch("os.stat", side_effect=fixture_stat)


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
