from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from git_janitor.autonomy import AutomationPolicy
from git_janitor.config import DEFAULT_PROJECT_REGISTRY_PATH, load_config
from git_janitor.scan_plans import MAX_PROJECTS_PER_PLAN, ScanPlanError


class ConfigExecutionPolicyTests(unittest.TestCase):
    def test_actions_apply_metadata_defaults_disabled(self) -> None:
        config = _load(
            """
            [scanner]
            scan_roots = []
            """
        )

        policy = AutomationPolicy.from_config(config)

        self.assertFalse(config.auto_fast_forward_default_branch)
        self.assertEqual(config.apply_categories, frozenset())
        self.assertIsNone(config.ledger_path)
        self.assertEqual(policy.apply_categories, frozenset())
        self.assertIsNone(policy.ledger_path)

    def test_actions_allowlist_ledger_and_flags_parse_from_config(self) -> None:
        config = _load(
            """
            [scanner]
            scan_roots = []

            [actions]
            auto_merge_green_prs = true
            auto_delete_merged_branches = true
            auto_mark_drafts_ready = true
            auto_fast_forward_default_branch = true
            apply_categories = ["merge-green-pr", "delete-merged-branch"]
            ledger_path = "/tmp/git-janitor/audit.jsonl"
            """
        )

        policy = AutomationPolicy.from_config(config)

        self.assertTrue(policy.auto_merge_green_prs)
        self.assertTrue(policy.auto_delete_merged_branches)
        self.assertTrue(policy.auto_mark_drafts_ready)
        self.assertTrue(policy.auto_fast_forward_default_branch)
        self.assertEqual(
            policy.apply_categories,
            frozenset({"merge-green-pr", "delete-merged-branch"}),
        )
        self.assertEqual(policy.ledger_path, "/tmp/git-janitor/audit.jsonl")

    def test_empty_allowlist_survives_enabled_action_flags(self) -> None:
        config = _load(
            """
            [scanner]
            scan_roots = []

            [actions]
            auto_delete_merged_branches = true
            """
        )

        policy = AutomationPolicy.from_config(config)

        self.assertTrue(policy.auto_delete_merged_branches)
        self.assertEqual(policy.apply_categories, frozenset())


class SavedScanPlanConfigTests(unittest.TestCase):
    def test_saved_scan_plans_parse_without_changing_scanner_or_action_fields(self) -> None:
        config = _load(
            """
            [scanner]
            repos = ["/tmp/repo"]
            fetch_prune = true
            github_author = "@me"

            [actions]
            auto_merge_green_prs = true
            apply_categories = ["merge-green-pr"]

            [scan_plans.branch-review]
            description = "Review branch cleanup evidence."
            project_ids = ["git-janitor", "sample-app"]
            finding_categories = ["merged-local-branch", "stale-linked-worktree"]
            """
        )

        plan = config.scan_plans["branch-review"]
        self.assertEqual(plan.name, "branch-review")
        self.assertEqual(plan.description, "Review branch cleanup evidence.")
        self.assertEqual(plan.project_ids, ("git-janitor", "sample-app"))
        self.assertEqual(
            plan.finding_categories,
            ("merged-local-branch", "stale-linked-worktree"),
        )
        self.assertEqual(config.repos, [Path("/tmp/repo")])
        self.assertTrue(config.fetch_prune)
        self.assertEqual(config.github_author, "@me")
        self.assertTrue(config.auto_merge_green_prs)
        self.assertEqual(config.apply_categories, frozenset({"merge-green-pr"}))

    def test_saved_scan_plan_defaults_to_empty_mapping(self) -> None:
        config = _load(
            """
            [scanner]
            scan_roots = []
            """
        )

        self.assertEqual(config.scan_plans, {})

    def test_saved_scan_plan_rejects_behavioral_or_source_keys(self) -> None:
        forbidden_values = {
            "actions": "{ apply_categories = [] }",
            "apply": "true",
            "dry_run": "true",
            "apply_categories": "[]",
            "ledger_path": '"/tmp/audit.jsonl"',
            "out": '"report.md"',
            "json_out": '"report.json"',
            "fetch_prune": "false",
            "no_fetch": "true",
            "registry_path": '"/tmp/registry.json"',
            "github_author": '"someone"',
            "high_risk_patterns": "[]",
        }

        for key, value in forbidden_values.items():
            with self.subTest(key=key), self.assertRaisesRegex(
                ScanPlanError,
                "unknown key",
            ):
                _load(
                    f"""
                    [scanner]
                    scan_roots = []

                    [scan_plans.review]
                    project_ids = ["git-janitor"]
                    finding_categories = ["dirty-worktree"]
                    {key} = {value}
                    """
                )

    def test_saved_scan_plan_rejects_empty_duplicate_malformed_or_unknown_values(self) -> None:
        cases = {
            "empty projects": 'project_ids = []\nfinding_categories = ["dirty-worktree"]',
            "duplicate projects": (
                'project_ids = ["git-janitor", "git-janitor"]\n'
                'finding_categories = ["dirty-worktree"]'
            ),
            "malformed project": (
                'project_ids = ["git-*"]\nfinding_categories = ["dirty-worktree"]'
            ),
            "noncanonical project id": (
                'project_ids = ["Owner/repo"]\nfinding_categories = ["dirty-worktree"]'
            ),
            "empty categories": 'project_ids = ["git-janitor"]\nfinding_categories = []',
            "duplicate categories": (
                'project_ids = ["git-janitor"]\n'
                'finding_categories = ["dirty-worktree", "dirty-worktree"]'
            ),
            "unknown category": (
                'project_ids = ["git-janitor"]\nfinding_categories = ["future-category"]'
            ),
        }

        for name, body in cases.items():
            with self.subTest(name=name), self.assertRaises(ScanPlanError):
                _load(
                    f"""
                    [scanner]
                    scan_roots = []

                    [scan_plans.review]
                    {body}
                    """
                )

    def test_saved_scan_plan_rejects_more_than_bounded_project_cap(self) -> None:
        projects = ", ".join(f'"project-{index}"' for index in range(MAX_PROJECTS_PER_PLAN + 1))

        with self.assertRaisesRegex(ScanPlanError, "at most"):
            _load(
                f"""
                [scanner]
                scan_roots = []

                [scan_plans.review]
                project_ids = [{projects}]
                finding_categories = ["dirty-worktree"]
                """
            )


class InventoryConfigTests(unittest.TestCase):
    def test_project_registry_path_has_neutral_default_and_expands_override(self) -> None:
        default = _load("[scanner]\nscan_roots = []\n")
        configured = _load(
            """
            [scanner]
            scan_roots = []

            [inventory]
            project_registry_path = "~/.config/example/projects.json"
            """
        )

        self.assertEqual(default.project_registry_path, DEFAULT_PROJECT_REGISTRY_PATH)
        self.assertEqual(
            configured.project_registry_path,
            Path("~/.config/example/projects.json").expanduser(),
        )

    def test_inventory_config_rejects_unknown_or_malformed_values(self) -> None:
        for body in (
            'project_registry_path = ""',
            'project_registry_path = 42',
            'github_owner = "owner"',
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                _load(f"[scanner]\nscan_roots = []\n[inventory]\n{body}\n")


def _load(contents: str):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.toml"
        path.write_text(contents, encoding="utf-8")
        return load_config(path)


if __name__ == "__main__":
    unittest.main()
