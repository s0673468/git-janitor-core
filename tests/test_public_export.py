"""Generic package defaults keep scope empty and retain explicit mutation gates."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from git_janitor import pr_shepherd, repo_facts
from git_janitor.config import load_config
from git_janitor.safe_delete import DEFAULT_PRESERVATION_DIR


ROOT = Path(__file__).resolve().parents[1]


class PublicConfigurationTests(unittest.TestCase):
    def test_example_has_no_scope_fetch_or_mutation_authority(self):
        config = load_config(ROOT / 'config.example.toml')
        self.assertEqual(config.repos, [])
        self.assertEqual(config.scan_roots, [])
        self.assertFalse(config.fetch_prune)
        self.assertFalse(config.auto_merge_green_prs)
        self.assertFalse(config.auto_delete_merged_branches)
        self.assertFalse(config.auto_fast_forward_default_branch)
        self.assertFalse(config.auto_mark_drafts_ready)
        self.assertEqual(config.apply_categories, frozenset())
        self.assertEqual(config.project_registry_path, Path.home() / '.config/git-janitor/projects.json')
        self.assertEqual(DEFAULT_PRESERVATION_DIR, Path.home() / '.local/share/git-janitor/preservation')

    def test_account_sweeps_require_explicit_owner_before_external_access(self):
        runner = Mock(side_effect=AssertionError('unexpected external access'))
        for command in ('watch', 'stalls', 'orphans'):
            with self.subTest(command=command), patch.object(pr_shepherd, 'DEFAULT_OWNER', ''), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                pr_shepherd.main([command], runner=runner)
            self.assertEqual(caught.exception.code, 2)
        with patch.object(repo_facts, 'DEFAULT_OWNER', ''), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            repo_facts.main([], runner=runner)
        self.assertEqual(caught.exception.code, 2)
        runner.assert_not_called()

    def test_runner_policy_requires_explicit_account_match(self):
        with patch.object(repo_facts, 'ORG_OWNER', ''):
            self.assertFalse(repo_facts.uses_org_runner_pool('example-org'))
            self.assertFalse(repo_facts.uses_org_runner_pool(''))
        with patch.object(repo_facts, 'ORG_OWNER', 'chosen-org'):
            self.assertTrue(repo_facts.uses_org_runner_pool('chosen-org'))
            self.assertFalse(repo_facts.uses_org_runner_pool('other-org'))


if __name__ == '__main__':
    unittest.main()
