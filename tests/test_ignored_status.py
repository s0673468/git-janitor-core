"""Ignored build output is ordinary scan noise but remains a deletion blocker."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_janitor.autonomy import AutomationPolicy, decide_touched_repo_candidate
from git_janitor.classify import classify_repo
from git_janitor.config import ScannerConfig
from git_janitor.execute import ActionExecutor
from git_janitor.fleet import TouchedRepoMaintainerConfig, build_touched_repo_candidates
from git_janitor.git import parse_status_porcelain, run_command, scan_repo
from git_janitor.ledger import AuditLedger
from git_janitor.models import AutomationDecision, CommandResult, RepoState
from git_janitor.safe_delete import evaluate_delete


class IgnoredStatusTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(prefix="janitor-ignored-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.git("init", "-b", "main", str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Ignored Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / ".gitignore").write_text(".venv/\nnode_modules/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "synthetic base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", "https://github.com/fixture-owner/fixture.git")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git("branch", "--set-upstream-to=origin/main", "main")
        self.config = ScannerConfig(fetch_prune=False)

    def git(self, *args, cwd=None):
        result = run_command(["git", *args], cwd or self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def build_output(self, path, count=1):
        for directory in (".venv", "node_modules"):
            output = path / directory / "nested"
            output.mkdir(parents=True)
            for index in range(count):
                (output / f"file-{index}.txt").write_text("synthetic valuable output\n")

    def runner(self, args, cwd, timeout):
        if args[:1] == ["gh"]:
            self.assertEqual(args[:3], ["gh", "pr", "list"])
            payload = [{"mergeCommit": {"oid": self.base}}] if "merged" in args else []
            return CommandResult(args, 0, json.dumps(payload), "")
        self.assertEqual(args[0], "git")
        self.assertNotIn(args[1], {"fetch", "push", "pull"})
        return run_command(args, cwd, timeout)

    def test_parser_reports_ignored_files_separately(self):
        state = RepoState(path="fixture", name="fixture")
        output = ("## main...origin/main [behind 1]\n M tracked.txt\n"
                  "?? scratch.txt\n!! .venv/cache\n!! node_modules/package\n")
        dirty, untracked, ahead, behind, upstream = parse_status_porcelain(
            output, ignored_files=state.ignored_files,
        )
        self.assertEqual(dirty, ["tracked.txt"])
        self.assertEqual(untracked, ["scratch.txt"])
        self.assertEqual(state.ignored_files, [".venv/cache", "node_modules/package"])
        self.assertEqual((ahead, behind, upstream), (0, 1, "origin/main"))
        self.assertEqual(parse_status_porcelain(output)[:2], (dirty, untracked))

    def test_build_output_allows_maintainer_fast_forward(self):
        self.build_output(self.repo)
        self.git("switch", "-c", "upstream-change")
        (self.repo / "tracked.txt").write_text("synthetic upstream change\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "synthetic upstream change")
        upstream_head = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", upstream_head)
        self.git("switch", "main")

        state = scan_repo(self.repo, self.config)
        self.assertEqual(state.errors, [])
        self.assertEqual(state.dirty_files, [])
        self.assertEqual(state.untracked_files, [])
        self.assertFalse(any(f.category == "dirty-worktree" for f in classify_repo(
            state, self.config, datetime.now(timezone.utc),
        )))
        candidates = build_touched_repo_candidates(
            [state], [], TouchedRepoMaintainerConfig(active_window_minutes=0),
        )
        self.assertEqual(len(candidates), 1)
        decision = decide_touched_repo_candidate(candidates[0])
        self.assertEqual(decision.category, "fast-forward-default-branch")
        executor = ActionExecutor(
            policy=AutomationPolicy(auto_fast_forward_default_branch=True,
                                    apply_categories=frozenset({decision.category})),
            ledger=AuditLedger(self.root / "audit.jsonl"), mode="dry-run",
            runner=self.runner, repo_paths={"repo": self.repo}, config=self.config,
        )
        self.assertEqual(executor.execute(decision).status, "would-apply")
        # Execute the fast-forward locally, without fetching or contacting the fixture URL.
        self.git("merge", "--ff-only", "origin/main")
        self.assertEqual(self.git("rev-parse", "HEAD"), upstream_head)
        self.assertTrue((self.repo / ".venv/nested/file-0.txt").exists())
        self.assertTrue((self.repo / "node_modules/nested/file-0.txt").exists())

    def test_large_ignored_trees_do_not_inflate_primary_or_linked_scan(self):
        linked = self.root / "linked"
        self.git("worktree", "add", "-b", "topic", str(linked))
        self.build_output(self.repo, count=500)
        self.build_output(linked, count=500)
        self.git("config", "status.showUntrackedFiles", "no")
        for checkout in (self.repo, linked):
            scratch = checkout / "scratch"
            scratch.mkdir()
            (scratch / "visible.txt").write_text("synthetic untracked work\n")
        statuses = []

        def scanner_runner(args, *, cwd=None, timeout=45):
            result = run_command(args, cwd, timeout)
            if args[:2] == ["git", "status"]:
                statuses.append((args, result))
            return result

        with patch("git_janitor.git.run_command", side_effect=scanner_runner):
            state = scan_repo(self.repo, self.config)
        self.assertEqual(len(statuses), 2)
        for args, result in statuses:
            self.assertIn("--untracked-files=normal", args)
            self.assertIn("--ignore-submodules=none", args)
            self.assertNotIn("--ignored", args)
            self.assertNotIn("--untracked-files=all", args)
            self.assertLess(len(result.stdout), 200)
        for checkout in (state, state.linked_worktrees[0]):
            self.assertEqual(checkout.dirty_files, [])
            self.assertEqual(checkout.untracked_files, ["scratch/"])
            self.assertEqual(checkout.ignored_files, [])

    def test_ignored_primary_files_block_executor_deletion_with_separate_evidence(self):
        self.git("branch", "topic")
        self.build_output(self.repo)
        executor = ActionExecutor(
            policy=AutomationPolicy(auto_delete_merged_branches=True,
                                    apply_categories=frozenset({"delete-merged-branch"})),
            ledger=AuditLedger(self.root / "audit.jsonl"), mode="apply",
            runner=self.runner, repo_paths={"repo": self.repo}, config=self.config,
        )
        result = executor.execute(AutomationDecision(
            disposition="auto-act", category="delete-merged-branch",
            title="repo:topic can be deleted", reason="synthetic proof",
            recommended_action="synthetic proof", evidence=("default_ref=origin/main",),
        ))
        self.assertEqual(result.status, "drifted")
        self.assertEqual(result.before["dirty_files"], [])
        self.assertEqual(result.before["untracked_files"], [])
        self.assertCountEqual(result.before["ignored_files"],
                              [".venv/nested/file-0.txt", "node_modules/nested/file-0.txt"])
        self.assertEqual(self.git("rev-parse", "refs/heads/topic"), self.base)

    def test_owned_worktree_ignored_output_blocks_deletion_proof(self):
        linked = self.repo / ".worktrees" / "task"
        self.git("worktree", "add", "-b", "topic", str(linked))
        (self.repo / ".git/info/exclude").write_text(".worktrees/\n")
        kwargs = dict(repo_path=self.repo, runner=self.runner, owned_worktree=linked,
                      expected_head=self.base, verified_merge_commit=self.base)
        clean = evaluate_delete("topic", **kwargs)
        self.assertTrue(clean.passed, [p for p in clean.proofs if not p.passed])
        self.build_output(linked)
        report = evaluate_delete("topic", **kwargs)
        self.assertFalse(report.passed)
        self.assertFalse(next(p.passed for p in report.proofs if p.name == "owned_worktree_clean"))
        self.assertTrue((linked / ".venv/nested/file-0.txt").exists())
        self.assertTrue((linked / "node_modules/nested/file-0.txt").exists())
