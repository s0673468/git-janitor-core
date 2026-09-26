from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from git_janitor.autonomy import (
    AutomationPolicy,
    TouchedRepoCandidate,
    decide_branch_cleanup,
    decide_linked_worktree_cleanup,
    decide_pull_request,
    decide_runner_failure,
    decide_touched_repo_candidate,
    decide_workflow_parse_error,
)
from git_janitor.config import ScannerConfig
from git_janitor.fleet import (
    TouchedRepoMaintainerConfig,
    build_touched_repo_candidates,
    plan_touched_repo_maintainer,
)
from git_janitor.models import (
    AutomationDecision,
    BranchState,
    LinkedWorktreeState,
    PullRequestState,
    RepoState,
    ScanReport,
)
from git_janitor.report import render_json, render_markdown


FIXTURES = Path(__file__).parent / "fixtures" / "agent_ops"


class AgentOpsAutonomyFixtureTests(unittest.TestCase):
    def test_pull_request_decisions_match_chaos_fixtures(self) -> None:
        for case in _load_cases("pull_requests.json"):
            with self.subTest(case=case["name"]):
                config = ScannerConfig(
                    high_risk_patterns=[
                        pattern.lower() for pattern in case.get("high_risk_patterns", [])
                    ]
                )
                decision = decide_pull_request(
                    _pull_request(case["pr"]),
                    config,
                    _policy(case.get("policy", {})),
                )

                self.assertEqual(decision.disposition, case["expected"]["disposition"])
                self.assertEqual(decision.category, case["expected"]["category"])

    def test_stale_branch_decisions_match_chaos_fixtures(self) -> None:
        for case in _load_cases("stale_branches.json"):
            with self.subTest(case=case["name"]):
                decision = decide_branch_cleanup(
                    _repo(case["repo"]),
                    _branch(case["branch"]),
                    _policy(case.get("policy", {})),
                )

                self.assertEqual(decision.disposition, case["expected"]["disposition"])
                self.assertEqual(decision.category, case["expected"]["category"])

    def test_stale_linked_worktree_decisions_match_chaos_fixtures(self) -> None:
        for case in _load_cases("linked_worktrees.json"):
            with self.subTest(case=case["name"]):
                decision = decide_linked_worktree_cleanup(
                    _repo(case["repo"]),
                    _linked_worktree(case["worktree"]),
                )

                self.assertEqual(decision.disposition, case["expected"]["disposition"])
                self.assertEqual(decision.category, case["expected"]["category"])

    def test_touched_repo_maintainer_decisions_match_chaos_fixtures(self) -> None:
        for case in _load_cases("touched_repo_maintainer.json"):
            with self.subTest(case=case["name"]):
                decision = decide_touched_repo_candidate(
                    _touched_repo_candidate(case["candidate"]),
                    _policy(case.get("policy", {})),
                )

                self.assertEqual(decision.disposition, case["expected"]["disposition"])
                self.assertEqual(decision.category, case["expected"]["category"])

    def test_touched_repo_maintainer_plan_matches_chaos_fixtures(self) -> None:
        for case in _load_cases("touched_repo_maintainer_plan.json"):
            with self.subTest(case=case["name"]):
                with tempfile.TemporaryDirectory() as tmpdir:
                    decisions = plan_touched_repo_maintainer(
                        [_touched_repo_candidate(candidate) for candidate in case["candidates"]],
                        _policy(case.get("policy", {})),
                        max_auto_prs=case["max_auto_prs"],
                        max_changed_repos=case["max_changed_repos"],
                        memory_path=case["memory_path"],
                        state_path=case.get("state_path", str(Path(tmpdir) / "state.json")),
                    )

                    self.assertEqual(
                        [decision.category for decision in decisions],
                        case["expected_categories"],
                    )
                    self.assertEqual(
                        [decision.disposition for decision in decisions],
                        case["expected_dispositions"],
                    )

    def test_touched_repo_plan_exposes_repeated_noop_state_path(self) -> None:
        decisions = plan_touched_repo_maintainer(
            [
                TouchedRepoCandidate(
                    repo="SampleWeb",
                    candidate="extra scoring tests",
                    safety_score=3,
                    changed_paths=("score-model.js",),
                )
            ],
            memory_path="/tmp/maintainer-memory.md",
            state_path="/tmp/maintainer-state.json",
        )

        memory_decision = decisions[-1]
        self.assertEqual(memory_decision.category, "automation-memory-required")
        self.assertIn("memory=/tmp/maintainer-memory.md", memory_decision.evidence)
        self.assertIn("state=/tmp/maintainer-state.json", memory_decision.evidence)
        self.assertTrue(any(item.startswith("touched_set=") for item in memory_decision.evidence))

    def test_touched_repo_planner_collects_scope_and_codex_pr_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            touched_file = repo_path / "README.md"
            touched_file.write_text("# demo\n", encoding="utf-8")
            graph_file = repo_path / ".understand-anything" / "knowledge-graph.json"
            graph_file.parent.mkdir()
            graph_file.write_text("{}", encoding="utf-8")
            repo = RepoState(
                path=str(repo_path),
                name="demo",
                current_branch="main",
                github_repo="owner/demo",
                remote_url="https://github.com/owner/demo.git",
                untracked_files=[".understand-anything/"],
            )
            pr = PullRequestState(
                repo="owner/demo",
                number=7,
                title="Codex maintenance",
                url="https://github.com/owner/demo/pull/7",
                head_ref="codex/docs-maintenance",
                base_ref="main",
                is_draft=False,
                merge_state="CLEAN",
                review_decision=None,
                check_status="pending",
            )

            candidates = build_touched_repo_candidates(
                [repo],
                [pr],
                TouchedRepoMaintainerConfig(
                    exclude_dirs=frozenset({".git"}),
                    max_file_evidence=4,
                ),
                pr_lookup_errors_by_repo={"owner/demo": ("owner/demo: gh pr list failed",)},
            )
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].repo, "demo")
            self.assertTrue(candidates[0].changed_paths)
            self.assertEqual(candidates[0].pr_lookup_errors, ("owner/demo: gh pr list failed",))
            self.assertEqual(
                candidates[0].open_codex_prs,
                ("https://github.com/owner/demo/pull/7:codex/docs-maintenance",),
            )
            self.assertFalse(
                any(".understand-anything" in item for item in candidates[0].recent_files)
            )
            self.assertEqual(candidates[0].preserved_paths, (".understand-anything/",))

    def test_touched_repo_planner_marks_stale_open_pr_ci_as_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = RepoState(
                path=tmpdir,
                name="SampleApp",
                current_branch="main",
                github_repo="owner/SampleApp",
                remote_url="https://github.com/owner/SampleApp.git",
                branches=[
                    BranchState(
                        name="main",
                        upstream="origin/main",
                        last_commit_iso="2026-06-25 08:00:00 -0300",
                        current=True,
                    )
                ],
            )
            pr = PullRequestState(
                repo="owner/SampleApp",
                number=95,
                title="Dependabot update",
                url="https://github.com/owner/SampleApp/pull/95",
                head_ref="dependabot/github_actions/actions/setup-python-6",
                base_ref="main",
                is_draft=False,
                merge_state="CLEAN",
                review_decision=None,
                check_status="stale",
            )

            candidates = build_touched_repo_candidates(
                [repo],
                [pr],
                TouchedRepoMaintainerConfig(max_file_evidence=4),
                now=datetime.fromisoformat("2026-06-25T12:00:00+00:00"),
            )
            decision = decide_touched_repo_candidate(candidates[0])

            self.assertEqual(
                candidates[0].stale_ci,
                ("https://github.com/owner/SampleApp/pull/95:checks=stale",),
            )
            self.assertEqual(decision.disposition, "report-blocked")
            self.assertEqual(decision.category, "stale-ci")

    def test_touched_repo_planner_attaches_approval_only_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = RepoState(
                path=tmpdir,
                name="SampleTool",
                current_branch="main",
                default_ref="origin/main",
                github_repo="owner/SampleTool",
                remote_url="https://github.com/owner/SampleTool.git",
                upstream="origin/main",
                branches=[
                    BranchState(
                        name="main",
                        upstream="origin/main",
                        last_commit_iso="2026-06-26 08:00:00 -0300",
                        current=True,
                    )
                ],
            )

            candidates = build_touched_repo_candidates(
                [repo],
                [],
                TouchedRepoMaintainerConfig(max_file_evidence=4),
                now=datetime.fromisoformat("2026-06-26T12:00:00+00:00"),
                approval_only_findings_by_repo={
                    "SampleTool": (
                        "delete-merged-branch:SampleTool:codex/judgment-eval-harness appears safe to delete",
                    )
                },
            )
            decision = decide_touched_repo_candidate(candidates[0])

            self.assertEqual(decision.disposition, "needs-approval")
            self.assertEqual(decision.category, "approval-only-finding")
            self.assertEqual(
                candidates[0].approval_only_findings,
                (
                    "delete-merged-branch:SampleTool:codex/judgment-eval-harness appears safe to delete",
                ),
            )

    def test_touched_repo_decision_orders_hard_stops_before_stale_and_approval(self) -> None:
        active = TouchedRepoCandidate(
            repo="SampleApp",
            candidate="docs drift",
            safety_score=5,
            recent_files=("recent-file:sample-app.db",),
            open_codex_prs=("https://github.com/owner/SampleApp/pull/92:codex/sync-fix",),
            stale_ci=("https://github.com/owner/SampleApp/pull/91:checks=stale",),
            approval_only_findings=("delete-merged-branch:codex/old-work",),
            changed_paths=("README.md",),
        )
        codex_pr = TouchedRepoCandidate(
            repo="SampleMetrics",
            candidate="test hardening",
            safety_score=5,
            open_codex_prs=("https://github.com/owner/SampleMetrics/pull/40:codex/watch-cap",),
            stale_ci=("https://github.com/owner/SampleMetrics/pull/41:checks=stale",),
            approval_only_findings=("delete-merged-branch:codex/old-work",),
            changed_paths=("tests/test_watch_levels.py",),
        )

        self.assertEqual(decide_touched_repo_candidate(active).category, "recent-activity")
        self.assertEqual(
            decide_touched_repo_candidate(codex_pr).category,
            "in-flight-codex-pr",
        )

    def test_touched_repo_planner_ignores_understand_anything_as_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            graph_file = repo_path / ".understand-anything" / "knowledge-graph.json"
            graph_file.parent.mkdir()
            graph_file.write_text("{}", encoding="utf-8")
            repo = RepoState(
                path=str(repo_path),
                name="graph-only",
                current_branch="main",
                github_repo="owner/graph-only",
                remote_url="https://github.com/owner/graph-only.git",
            )

            candidates = build_touched_repo_candidates(
                [repo],
                [],
                TouchedRepoMaintainerConfig(max_file_evidence=4),
            )

            self.assertEqual(candidates, [])

    def test_touched_repo_planner_marks_clean_behind_default_for_fast_forward(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = RepoState(
                path=tmpdir,
                name="SampleMetrics",
                current_branch="main",
                default_branch="main",
                default_ref="origin/main",
                remote_url="https://github.com/owner/SampleMetrics.git",
                github_repo="owner/SampleMetrics",
                behind=2,
                upstream="origin/main",
                branches=[
                    BranchState(
                        name="main",
                        upstream="origin/main",
                        last_commit_iso="2026-06-26 08:00:00 -0300",
                        current=True,
                    )
                ],
            )

            candidates = build_touched_repo_candidates(
                [repo],
                [],
                TouchedRepoMaintainerConfig(max_file_evidence=4),
                now=datetime.fromisoformat("2026-06-26T12:00:00+00:00"),
            )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(
                candidates[0].fast_forward,
                ("branch=main", "upstream=origin/main", "behind=2"),
            )
            self.assertEqual(candidates[0].remote_state, ())

    def test_touched_repo_planner_blocks_fast_forward_after_fetch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = RepoState(
                path=tmpdir,
                name="SampleMetrics",
                current_branch="main",
                default_branch="main",
                default_ref="origin/main",
                remote_url="https://github.com/owner/SampleMetrics.git",
                github_repo="owner/SampleMetrics",
                behind=2,
                upstream="origin/main",
                fetch_prune_status="fatal: could not read from remote repository",
                branches=[
                    BranchState(
                        name="main",
                        upstream="origin/main",
                        last_commit_iso="2026-06-26 08:00:00 -0300",
                        current=True,
                    )
                ],
            )

            candidates = build_touched_repo_candidates(
                [repo],
                [],
                TouchedRepoMaintainerConfig(max_file_evidence=4),
                now=datetime.fromisoformat("2026-06-26T12:00:00+00:00"),
            )
            decision = decide_touched_repo_candidate(candidates[0])

            self.assertEqual(candidates[0].fast_forward, ())
            self.assertEqual(
                candidates[0].repo_errors,
                ("fetch_prune_status=fatal: could not read from remote repository",),
            )
            self.assertEqual(decision.category, "repo-inspection-failed")

    def test_touched_repo_planner_keeps_missing_origin_as_publication_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = RepoState(
                path=tmpdir,
                name="Study",
                current_branch="master",
                default_branch="master",
                fetch_prune_status="fatal: 'origin' does not appear to be a git repository",
                branches=[
                    BranchState(
                        name="master",
                        last_commit_iso="2026-06-26 08:00:00 -0300",
                        current=True,
                    )
                ],
            )

            candidates = build_touched_repo_candidates(
                [repo],
                [],
                TouchedRepoMaintainerConfig(max_file_evidence=4),
                now=datetime.fromisoformat("2026-06-26T12:00:00+00:00"),
            )
            decision = decide_touched_repo_candidate(candidates[0])

            self.assertEqual(
                candidates[0].publication_boundary,
                ("origin=missing", "github_repo=unresolved"),
            )
            self.assertEqual(
                candidates[0].repo_errors,
                ("fetch_prune_status=fatal: 'origin' does not appear to be a git repository",),
            )
            self.assertEqual(decision.disposition, "needs-approval")
            self.assertEqual(decision.category, "publication-boundary-blocked")

    def test_touched_repo_plan_suppresses_repeated_publication_boundary(self) -> None:
        candidate = TouchedRepoCandidate(
            repo="Study",
            candidate="validation docs",
            safety_score=5,
            publication_boundary=("origin=missing", "github_repo=unresolved"),
            changed_paths=("AGENTS.md",),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "publication_boundaries": {
                            "Study": {
                                "fingerprint": _publication_boundary_fingerprint(candidate),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            decisions = plan_touched_repo_maintainer(
                [candidate],
                state_path=str(state_path),
            )

            self.assertEqual(decisions[0].disposition, "no-op")
            self.assertEqual(decisions[0].category, "publication-boundary-already-reported")

    def test_touched_repo_plan_stops_repeated_below_threshold_set(self) -> None:
        candidates = [
            TouchedRepoCandidate(
                repo="SampleWeb",
                candidate="speculative hardening",
                safety_score=3,
                changed_paths=("app.js",),
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_state = Path(tmpdir) / "missing-state.json"
            first = plan_touched_repo_maintainer(candidates, state_path=str(missing_state))
            touched_set = next(
                item.removeprefix("touched_set=")
                for item in first[-1].evidence
                if item.startswith("touched_set=")
            )
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "last_score_below_threshold": {
                            "fingerprint": touched_set,
                            "count": 1,
                        }
                    }
                ),
                encoding="utf-8",
            )

            second = plan_touched_repo_maintainer(candidates, state_path=str(state_path))

            self.assertIn("repeated-noop-stop", [decision.category for decision in second])

    def test_touched_repo_plan_does_not_hide_material_blockers_as_repeated_noop(self) -> None:
        candidates = [
            TouchedRepoCandidate(
                repo="SampleApp",
                candidate="stale CI triage",
                safety_score=3,
                stale_ci=("https://github.com/owner/SampleApp/pull/95:checks=stale",),
                changed_paths=("recent-commit:main:2026-06-25 08:00:00 -0300",),
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            touched_set = _touched_set_fingerprint(candidates)
            state_path.write_text(
                json.dumps(
                    {
                        "last_score_below_threshold": {
                            "fingerprint": touched_set,
                            "count": 1,
                        }
                    }
                ),
                encoding="utf-8",
            )

            decisions = plan_touched_repo_maintainer(candidates, state_path=str(state_path))

            self.assertIn("stale-ci", [decision.category for decision in decisions])
            self.assertNotIn("repeated-noop-stop", [decision.category for decision in decisions])

    def test_runner_failure_decisions_match_chaos_fixtures(self) -> None:
        for case in _load_cases("runner_failures.json"):
            with self.subTest(case=case["name"]):
                decision = decide_runner_failure(
                    case["log"],
                    _policy(case.get("policy", {})),
                )

                self.assertIsNotNone(decision)
                assert decision is not None
                self.assertEqual(decision.disposition, case["expected"]["disposition"])
                self.assertEqual(decision.category, case["expected"]["category"])

    def test_workflow_error_decisions_match_chaos_fixtures(self) -> None:
        for case in _load_cases("workflow_errors.json"):
            with self.subTest(case=case["name"]):
                decision = decide_workflow_parse_error(
                    case["message"],
                    case["workflow_path"],
                    _policy(case.get("policy", {})),
                )

                self.assertEqual(decision.disposition, case["expected"]["disposition"])
                self.assertEqual(decision.category, case["expected"]["category"])

    def test_report_exposes_automation_decisions(self) -> None:
        report = ScanReport(
            generated_at="2026-06-26T09:00:00-03:00",
            repos=[],
            pull_requests=[],
            findings=[],
            automation_decisions=[
                AutomationDecision(
                    disposition="auto-act",
                    category="low-risk-maintainer-fix",
                    title="git-janitor: docs can be fixed autonomously",
                    reason="The candidate is low risk and score-qualified.",
                    recommended_action="Implement the focused fix and validate locally.",
                    evidence=("README.md",),
                )
            ],
        )

        markdown = render_markdown(report)
        serialized = json.loads(render_json(report))

        self.assertIn("## Automation Decisions", markdown)
        self.assertIn("Disposition: `auto-act`", markdown)
        self.assertEqual(
            serialized["automation_decisions"][0]["category"],
            "low-risk-maintainer-fix",
        )


def _load_cases(name: str) -> list[dict]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)["cases"]


def _policy(raw: dict) -> AutomationPolicy:
    return AutomationPolicy(**raw)


def _pull_request(raw: dict) -> PullRequestState:
    return PullRequestState(
        repo=raw["repo"],
        number=raw["number"],
        title=raw["title"],
        url=raw.get("url"),
        head_ref=raw["head_ref"],
        base_ref=raw.get("base_ref"),
        is_draft=raw["is_draft"],
        merge_state=raw.get("merge_state"),
        review_decision=raw.get("review_decision"),
        check_status=raw["check_status"],
        changed_files=list(raw.get("changed_files", [])),
    )


def _repo(raw: dict) -> RepoState:
    return RepoState(
        path=raw["path"],
        name=raw["name"],
        default_branch=raw.get("default_branch", "main"),
        default_ref=raw.get("default_ref"),
        remote_url=raw.get("remote_url"),
        github_repo=raw.get("github_repo"),
        dirty_files=list(raw.get("dirty_files", [])),
        untracked_files=list(raw.get("untracked_files", [])),
        ahead=raw.get("ahead", 0),
        behind=raw.get("behind", 0),
        upstream=raw.get("upstream"),
        fetch_prune_status=raw.get("fetch_prune_status"),
        errors=list(raw.get("errors", [])),
    )


def _branch(raw: dict) -> BranchState:
    return BranchState(
        name=raw["name"],
        upstream=raw.get("upstream"),
        merged_to_default=raw.get("merged_to_default", False),
        unique_commit_count=raw.get("unique_commit_count"),
        current=raw.get("current", False),
    )


def _linked_worktree(raw: dict) -> LinkedWorktreeState:
    return LinkedWorktreeState(
        path=raw["path"],
        branch=raw.get("branch"),
        head=raw.get("head"),
        upstream=raw.get("upstream"),
        upstream_gone=raw.get("upstream_gone", False),
        default_ref=raw.get("default_ref"),
        tree_matches_default=raw.get("tree_matches_default"),
        unique_commit_count=raw.get("unique_commit_count"),
        dirty_files=list(raw.get("dirty_files", [])),
        untracked_files=list(raw.get("untracked_files", [])),
        errors=list(raw.get("errors", [])),
    )


def _touched_repo_candidate(raw: dict) -> TouchedRepoCandidate:
    return TouchedRepoCandidate(
        repo=raw["repo"],
        candidate=raw["candidate"],
        safety_score=raw.get("safety_score"),
        recent_files=tuple(raw.get("recent_files", [])),
        repo_errors=tuple(raw.get("repo_errors", [])),
        dirty_files=tuple(raw.get("dirty_files", [])),
        fast_forward=tuple(raw.get("fast_forward", [])),
        remote_state=tuple(raw.get("remote_state", [])),
        publication_boundary=tuple(raw.get("publication_boundary", [])),
        pr_lookup_errors=tuple(raw.get("pr_lookup_errors", [])),
        open_codex_prs=tuple(raw.get("open_codex_prs", [])),
        stale_ci=tuple(raw.get("stale_ci", [])),
        approval_only_findings=tuple(raw.get("approval_only_findings", [])),
        permission_sensitive=raw.get("permission_sensitive", False),
        changed_paths=tuple(raw.get("changed_paths", [])),
        preserved_paths=tuple(raw.get("preserved_paths", [])),
        score_components=tuple(raw.get("score_components", [])),
    )


def _publication_boundary_fingerprint(candidate: TouchedRepoCandidate) -> str:
    digest = hashlib.sha256()
    digest.update(candidate.repo.encode())
    digest.update(b"\0")
    for entry in sorted(candidate.publication_boundary):
        digest.update(entry.encode())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _touched_set_fingerprint(candidates: list[TouchedRepoCandidate]) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(candidates, key=lambda item: item.repo):
        digest.update(candidate.repo.encode())
        digest.update(b"\0")
        for entry in sorted(candidate.changed_paths):
            digest.update(entry.encode())
            digest.update(b"\0")
    return digest.hexdigest()[:16]


if __name__ == "__main__":
    unittest.main()
