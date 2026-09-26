from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from git_janitor.agent_ops_eval import REQUIRED_AGENT_OPS_DOMAINS, run_agent_ops_eval


FIXTURE = Path(__file__).parent / "fixtures" / "agent_ops" / "eval_harness.json"


class AgentOpsEvalHarnessTests(unittest.TestCase):
    def test_eval_manifest_matches_expected_decisions(self) -> None:
        results = run_agent_ops_eval(FIXTURE)

        self.assertTrue(results)
        failed = [result.to_dict() for result in results if not result.passed]
        self.assertEqual(failed, [])
        self.assertLessEqual(REQUIRED_AGENT_OPS_DOMAINS, {result.domain for result in results})

    def test_default_policy_cases_do_not_auto_act(self) -> None:
        results = run_agent_ops_eval(FIXTURE)

        violations = [
            result.to_dict()
            for result in results
            if result.default_policy_read_only and result.has_auto_act
        ]
        self.assertEqual(violations, [])

    def test_plan_state_path_must_stay_inside_fixture_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_dir = Path(tmpdir)
            (fixture_dir / "eval.json").write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "domain": "touched-repo maintenance",
                                "source": "touched_repo_maintainer_plan.json",
                                "case": "outside state path",
                                "expected": {
                                    "dispositions": ["wait"],
                                    "categories": ["automation-memory-required"],
                                },
                                "default_policy_read_only": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (fixture_dir / "touched_repo_maintainer_plan.json").write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "name": "outside state path",
                                "policy": {},
                                "max_auto_prs": 2,
                                "max_changed_repos": 2,
                                "memory_path": "/tmp/memory.md",
                                "state_path": "../live-state.json",
                                "candidates": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "state_path escapes"):
                run_agent_ops_eval(fixture_dir / "eval.json")
