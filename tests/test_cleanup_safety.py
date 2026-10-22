"""Branch cleanup and preservation safety regressions on generated temporary repositories."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from git_janitor.autonomy import AutomationPolicy
from git_janitor.closeout_facts import _worktree_facts
from git_janitor.config import ScannerConfig
from git_janitor.execute import ActionExecutor
from git_janitor.git import run_command, scan_repo
from git_janitor.ledger import AuditLedger
from git_janitor.models import AutomationDecision, CommandResult
from git_janitor.safe_delete import evaluate_delete, git_safe_delete_main, preserve_branch


class _Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base_dir = Path(self.tmp.name).resolve()
        self.root = self.base_dir / "repo"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("config", "commit.gpgsign", "false")
        self.commit("file.txt", "base\n", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", "https://github.com/o/r.git")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.merged_payload = "[]"
        self.overrides: dict[tuple[str, ...], CommandResult] = {}

    def git(self, *args, cwd=None):
        result = run_command(["git", *args], cwd or self.root, 30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, name, text, message):
        (self.root / name).write_text(text)
        self.git("add", ".")
        self.git("commit", "-m", message)

    def runner(self, args, cwd, timeout):
        if tuple(args) in self.overrides:
            return self.overrides[tuple(args)]
        if args[:3] == ["gh", "pr", "list"]:
            payload = self.merged_payload if "merged" in args else "[]"
            return CommandResult(args, 0, payload, "")
        return run_command(args, cwd, timeout)

    def gone_upstream_topic(self):
        """topic carries one commit absent from origin/main; its upstream ref is gone."""
        self.git("switch", "-c", "topic")
        self.commit("topic.txt", "topic\n", "topic work")
        self.git("config", "branch.topic.remote", "origin")
        self.git("config", "branch.topic.merge", "refs/heads/topic")
        self.git("switch", "main")

    def squash_of(self, branch):
        tree = self.git("rev-parse", f"{branch}^{{tree}}")
        return self.git("commit-tree", tree, "-p", self.base, "-m", "squashed")


def _failed(report):
    return [proof.name for proof in report.proofs if not proof.passed]


class DefaultBranchDeletionTests(_Repo):
    def test_local_default_branch_is_never_a_cleanup_target(self):
        self.git("switch", "-c", "feature")

        report = evaluate_delete("main", repo_path=self.root, runner=self.runner)

        self.assertFalse(report.passed)
        self.assertIn("not_default_branch", _failed(report))

    def test_execute_keeps_local_default_branch(self):
        self.git("switch", "-c", "feature")
        ledger = self.base_dir / "delete.jsonl"
        with redirect_stdout(io.StringIO()):
            status = git_safe_delete_main(
                ["main", "--execute", "--repo", str(self.root), "--ledger", str(ledger)],
                runner=self.runner,
            )

        self.assertEqual(status, 3)
        self.assertEqual(self.git("rev-parse", "refs/heads/main"), self.base)
        self.assertEqual(json.loads(ledger.read_text())["status"], "blocked")

    def test_nested_default_branch_is_matched_by_full_name(self):
        self.git("branch", "release/2026")
        self.git("update-ref", "refs/remotes/origin/release/2026", self.base)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/release/2026")
        self.git("branch", "2026")

        nested = evaluate_delete("release/2026", repo_path=self.root, runner=self.runner)
        leaf = evaluate_delete("2026", repo_path=self.root, runner=self.runner)

        self.assertIn("not_default_branch", _failed(nested))
        # A different branch that only shares the last path segment stays eligible.
        self.assertTrue(leaf.passed, _failed(leaf))

    def test_merged_feature_branch_still_passes(self):
        self.git("branch", "old-docs")

        report = evaluate_delete("old-docs", repo_path=self.root, runner=self.runner)

        self.assertTrue(report.passed, _failed(report))
        self.assertEqual(report.command, ["git", "branch", "-d", "old-docs"])


class UpstreamProbeFailureTests(_Repo):
    def test_upstream_probe_timeout_or_fatal_is_not_gone(self):
        self.gone_upstream_topic()
        merge = self.squash_of("topic")
        self.git("update-ref", "refs/remotes/origin/main", merge)
        self.merged_payload = json.dumps([{"number": 3, "mergeCommit": {"oid": merge}}])
        probe = ("git", "rev-parse", "--verify", "--quiet", "origin/topic")
        for code, stderr in ((124, "command timed out after 30s"), (128, "fatal: bad object")):
            with self.subTest(code=code):
                self.overrides[probe] = CommandResult(list(probe), code, "", stderr)

                report = evaluate_delete("topic", repo_path=self.root, runner=self.runner)

                self.assertFalse(report.passed)
                self.assertIn("upstream_gone_or_ancestor", _failed(report))
                proof = next(p for p in report.proofs if p.name == "upstream_gone_or_ancestor")
                self.assertIn("upstream_gone=False", proof.detail)

    def test_missing_upstream_ref_still_counts_as_gone(self):
        self.gone_upstream_topic()
        merge = self.squash_of("topic")
        self.git("update-ref", "refs/remotes/origin/main", merge)
        self.merged_payload = json.dumps([{"number": 3, "mergeCommit": {"oid": merge}}])

        report = evaluate_delete("topic", repo_path=self.root, runner=self.runner)

        self.assertTrue(report.passed, _failed(report))
        head = self.git("rev-parse", "refs/heads/topic")
        self.assertEqual(report.command, ["git", "update-ref", "-d", "refs/heads/topic", head])


class SquashForceDeletePinTests(_Repo):
    """The squash path deletes only the exact head its proofs inspected."""

    def squash_merged_topic(self):
        self.gone_upstream_topic()
        merge = self.squash_of("topic")
        self.git("update-ref", "refs/remotes/origin/main", merge)
        self.merged_payload = json.dumps([{"number": 3, "mergeCommit": {"oid": merge}}])

    def execute(self, branch):
        ledger = self.base_dir / "delete.jsonl"
        with redirect_stdout(io.StringIO()):
            status = git_safe_delete_main(
                [branch, "--execute", "--repo", str(self.root), "--ledger", str(ledger)],
                runner=self.runner,
            )
        return status, json.loads(ledger.read_text().splitlines()[-1])

    def test_squash_equivalent_branch_is_still_deleted(self):
        self.squash_merged_topic()

        status, entry = self.execute("topic")

        self.assertEqual(status, 0)
        self.assertEqual(entry["status"], "applied")
        missing = run_command(["git", "rev-parse", "--verify", "--quiet", "refs/heads/topic"], self.root, 30)
        self.assertEqual(missing.returncode, 1)

    def test_branch_advanced_after_proofs_is_kept(self):
        self.squash_merged_topic()
        proved = self.git("rev-parse", "refs/heads/topic")
        self.git("switch", "topic")
        self.commit("late.txt", "late\n", "late unpushed work")
        late = self.git("rev-parse", "HEAD")
        self.git("switch", "main")
        self.git("update-ref", "refs/heads/topic", proved)
        real_runner = self.runner

        def advancing_runner(args, cwd, timeout):
            # Another process advances topic right before the deletion runs.
            if args[:2] in (["git", "branch"], ["git", "update-ref"]):
                run_command(["git", "update-ref", "refs/heads/topic", late], self.root, 30)
            return real_runner(args, cwd, timeout)

        self.runner = advancing_runner
        status, entry = self.execute("topic")

        self.assertNotEqual(status, 0)
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(self.git("rev-parse", "refs/heads/topic"), late)

    def test_same_name_tag_cannot_authorize_deleting_branch_work(self):
        self.squash_merged_topic()
        # The tag sits at the squash-merged state; the branch gains unpushed work.
        self.git("tag", "topic", "topic")
        self.git("switch", "topic")
        self.commit("extra.txt", "extra\n", "unpushed extra work")
        extra = self.git("rev-parse", "HEAD")
        self.git("switch", "main")

        status, entry = self.execute("topic")

        self.assertNotEqual(status, 0)
        self.assertNotIn(entry["command"], (["git", "branch", "-D", "topic"],))
        self.assertEqual(self.git("rev-parse", "refs/heads/topic"), extra)


class MissingDefaultRefTests(_Repo):
    def test_unreadable_origin_head_blocks_instead_of_guessing_main(self):
        # Real default is master; a stale origin/main already contains it.
        self.git("branch", "-m", "main", "master")
        self.git("update-ref", "refs/remotes/origin/master", self.base)
        self.git("symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        self.git("switch", "-c", "feature")
        probe = ("git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        for override in (None, CommandResult(list(probe), 124, "", "command timed out after 30s")):
            with self.subTest(override=override):
                self.overrides.pop(probe, None)
                if override is not None:
                    self.overrides[probe] = override

                report = evaluate_delete("master", repo_path=self.root, runner=self.runner)

                self.assertFalse(report.passed)
                self.assertIn("not_default_branch", _failed(report))

    def test_readable_origin_head_still_permits_merged_branch(self):
        self.git("branch", "-m", "main", "master")
        self.git("update-ref", "refs/remotes/origin/master", self.base)
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
        self.git("branch", "old-docs")

        report = evaluate_delete("old-docs", repo_path=self.root, runner=self.runner)
        default = evaluate_delete("master", repo_path=self.root, runner=self.runner)

        self.assertTrue(report.passed, _failed(report))
        self.assertIn("not_default_branch", _failed(default))


class MergedReadbackShapeTests(_Repo):
    def test_merged_readback_without_merge_commit_is_not_merge_proof(self):
        self.gone_upstream_topic()
        for payload in (
            "[{}]", '[{"number": 3}]', '[{"number": 3, "mergeCommit": null}]',
            '[{"number": 3, "mergeCommit": {}}]', '[{"number": 3, "mergeCommit": {"oid": ""}}]',
            '{"number": 3}', '"merged"', "[1]", "[null]", "7",
        ):
            with self.subTest(payload=payload):
                self.merged_payload = payload

                report = evaluate_delete("topic", repo_path=self.root, runner=self.runner)

                self.assertFalse(report.passed)
                self.assertIn("merged_pr_readback_or_ancestry", _failed(report))
                self.assertIsNone(report.merge_commit)

    def test_real_merge_commit_shapes_remain_accepted(self):
        self.gone_upstream_topic()
        merge = self.squash_of("topic")
        self.git("update-ref", "refs/remotes/origin/main", merge)
        for entry in (
            {"number": 3, "title": "t", "url": "u", "mergeCommit": {"oid": merge}},
            {"mergeCommit": merge},
        ):
            with self.subTest(entry=entry):
                self.merged_payload = json.dumps([entry])

                report = evaluate_delete("topic", repo_path=self.root, runner=self.runner)

                self.assertTrue(report.passed, _failed(report))
                self.assertEqual(report.merge_commit, merge)


class PreservationOverwriteTests(_Repo):
    def preserve(self, branch):
        return preserve_branch(
            branch, repo_path=self.root, reason="test", validation="unit test",
            output_dir=self.base_dir / "preserved", runner=self.runner,
        )

    def heads(self, bundle):
        return self.git("bundle", "list-heads", bundle)

    def test_flattened_name_collision_keeps_first_preservation(self):
        self.git("branch", "feature/x")
        self.git("switch", "-c", "feature-x")
        self.commit("other.txt", "other\n", "other work")
        self.git("switch", "main")

        first = self.preserve("feature/x")
        second = self.preserve("feature-x")

        self.assertTrue(first["ok"], first)
        self.assertFalse(second["ok"])
        self.assertIn("refusing to overwrite", second["error"])
        self.assertEqual(self.heads(first["bundle"]), f"{self.base} refs/heads/feature/x")

    def test_rewritten_branch_keeps_earlier_preservation(self):
        self.git("switch", "-c", "topic")
        self.commit("topic.txt", "v1\n", "v1")
        old = self.git("rev-parse", "HEAD")
        self.git("switch", "main")
        first = self.preserve("topic")
        self.git("branch", "-f", "topic", "main")
        self.git("switch", "topic")
        self.commit("topic.txt", "v2\n", "v2")
        self.git("switch", "main")

        second = self.preserve("topic")

        self.assertTrue(first["ok"], first)
        self.assertFalse(second["ok"])
        self.assertEqual(self.heads(first["bundle"]), f"{old} refs/heads/topic")

    def test_rerun_and_fast_forward_rerun_still_succeed(self):
        self.git("branch", "topic")
        first = self.preserve("topic")
        again = self.preserve("topic")
        self.git("switch", "topic")
        self.commit("topic.txt", "more\n", "more")
        new = self.git("rev-parse", "HEAD")
        self.git("switch", "main")
        advanced = self.preserve("topic")

        self.assertTrue(first["ok"], first)
        self.assertTrue(again["ok"], again)
        self.assertTrue(advanced["ok"], advanced)
        self.assertEqual(self.heads(advanced["bundle"]), f"{new} refs/heads/topic")

    def test_concurrent_colliding_preservation_cannot_overwrite(self):
        self.git("branch", "feature/x")
        self.git("switch", "-c", "feature-x")
        self.commit("other.txt", "other\n", "other work")
        self.git("switch", "main")
        real_runner = self.runner
        racing: list[dict] = []

        def racing_runner(args, cwd, timeout):
            # A second run starts after the first checked for an existing bundle
            # and finishes before the first creates its own.
            if args[:3] == ["git", "bundle", "create"] and not racing:
                self.runner = real_runner
                racing.append(self.preserve("feature/x"))
            return real_runner(args, cwd, timeout)

        self.runner = racing_runner
        first = self.preserve("feature-x")
        self.runner = real_runner

        results = [first, *racing]
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for result in results if result["ok"]), 1, results)
        kept = next(result for result in results if result["ok"])
        branch = "feature-x" if kept is first else "feature/x"
        self.assertEqual(self.heads(kept["bundle"]), f"{self.git('rev-parse', branch)} refs/heads/{branch}")
        loser = next(result for result in results if not result["ok"])
        self.assertIn("in progress", loser["error"])
        # The lock is released afterwards, and a later run sees the kept bundle.
        later = self.preserve("feature/x" if branch == "feature-x" else "feature-x")
        self.assertFalse(later["ok"])
        self.assertIn("refusing to overwrite", later["error"])


class PrunableLinkedWorktreeTests(_Repo):
    """A linked worktree whose .git link is gone makes git read the parent checkout."""

    def setUp(self):
        super().setUp()
        (self.root / ".git" / "info" / "exclude").write_text(".worktrees/\n")
        self.worktree = self.root / ".worktrees" / "feat"
        self.git("worktree", "add", "-b", "feat", str(self.worktree))
        (self.worktree / "local.txt").write_text("local only\n")
        self.git("add", ".", cwd=self.worktree)
        self.git("commit", "-m", "local only", cwd=self.worktree)
        (self.worktree / "edit.txt").write_text("uncommitted\n")
        (self.worktree / ".git").unlink()

    def test_scanner_reports_prunable_worktree_as_unknown_not_clean(self):
        repo = scan_repo(self.root, ScannerConfig(fetch_prune=False))

        [worktree] = repo.linked_worktrees
        self.assertTrue(any("prunable" in error for error in worktree.errors), worktree.errors)
        self.assertIsNone(worktree.unique_commit_count)
        self.assertIsNone(worktree.tree_matches_default)
        self.assertTrue(any("prunable" in error for error in repo.errors))

    def test_closeout_reports_prunable_worktree_as_incomplete(self):
        errors: list[str] = []
        details: dict[str, str] = {}

        _worktree_facts(
            self.root, remote_default_ref="origin/main", remote_default_head=self.base,
            runner=self.runner, errors=errors, error_details=details,
        )

        self.assertIn("worktree-1-prunable", errors)

    def test_healthy_linked_worktree_is_still_inspected(self):
        healthy = self.root / ".worktrees" / "ok"
        self.git("worktree", "add", "-b", "ok", str(healthy))

        repo = scan_repo(self.root, ScannerConfig(fetch_prune=False))

        state = next(w for w in repo.linked_worktrees if w.branch == "ok")
        self.assertEqual(state.errors, [])
        self.assertEqual(state.unique_commit_count, 0)
        self.assertTrue(state.tree_matches_default)


class ExecuteNestedDefaultBranchTests(unittest.TestCase):
    def execute(self, branch):
        calls = []
        repo = Path("/fixture/repo")

        def runner(args, cwd, timeout):
            calls.append(list(args))
            if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                return CommandResult(args, 0, "feature", "")
            if args[:2] == ["git", "status"]:
                return CommandResult(args, 0, "## feature", "")
            if args[:3] == ["git", "branch", "-d"]:
                return CommandResult(args, 0, "", "")
            if args[:2] == ["git", "rev-parse"]:
                return CommandResult(args, 0, "oldsha", "")
            return CommandResult(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            executor = ActionExecutor(
                policy=AutomationPolicy(
                    auto_delete_merged_branches=True,
                    apply_categories=frozenset({"delete-merged-branch"}),
                ),
                ledger=AuditLedger(Path(tmp) / "audit.jsonl"),
                mode="apply",
                runner=runner,
                repo_paths={"repo": repo},
                config=ScannerConfig(),
            )
            result = executor.execute(AutomationDecision(
                disposition="auto-act",
                category="delete-merged-branch",
                title=f"repo:{branch} can be deleted",
                reason="fixture",
                recommended_action="fixture",
                evidence=("default_ref=origin/release/2026", "unique_commit_count=0"),
            ))
        return result, calls

    def test_nested_default_branch_is_refused(self):
        result, calls = self.execute("release/2026")

        self.assertEqual(result.status, "drifted")
        self.assertEqual(result.detail, "branch is the default branch")
        self.assertFalse(any(call[:3] == ["git", "branch", "-d"] for call in calls))

    def test_branch_sharing_only_the_leaf_name_is_not_the_default(self):
        result, _calls = self.execute("2026")

        self.assertNotEqual(result.detail, "branch is the default branch")
        self.assertEqual(result.status, "applied")


if __name__ == "__main__":
    unittest.main()
