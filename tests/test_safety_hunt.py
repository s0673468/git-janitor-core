"""Deletion safety probes using only disposable, synthetic Git repositories."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_janitor.autonomy import AutomationPolicy, decide_branch_cleanup, decide_linked_worktree_cleanup
from git_janitor.classify import classify_repo
from git_janitor.config import ScannerConfig
from git_janitor.git import run_command, scan_repo
from git_janitor.execute import ActionExecutor
from git_janitor.ledger import AuditLedger
from git_janitor.models import AutomationDecision, CommandResult
from git_janitor.safe_delete import evaluate_delete


class SafetyHuntTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory(prefix="janitor-safety-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.git("init", "-b", "main", str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Safety Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.commit("base.txt", "base\n")
        self.base = self.git("rev-parse", "HEAD")
        # A synthetic URL is only read as metadata. The runner rejects every gh call.
        self.git("remote", "add", "origin", "https://github.com/fixture-owner/fixture.git")
        self.publish()
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.config = ScannerConfig(fetch_prune=False)
        self.merged = []

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd or self.repo,
            check=True, capture_output=True, text=True,
        ).stdout.rstrip("\n")

    def commit(self, filename, content, *, cwd=None):
        path = cwd or self.repo
        (path / filename).write_text(content, encoding="utf-8")
        self.git("add", filename, cwd=path)
        self.git("commit", "-m", "synthetic change", cwd=path)

    def publish(self):
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def runner(self, args, cwd, timeout):
        if args[:1] == ["gh"]:
            self.assertEqual(args[:3], ["gh", "pr", "list"])
            return CommandResult(args, 0, json.dumps(self.merged if "merged" in args else []), "")
        self.assertEqual(args[0], "git")
        self.assertNotIn(args[1], {"fetch", "push", "pull"})
        return run_command(args, cwd, timeout)

    def report(self, branch="topic", **kwargs):
        return evaluate_delete(branch, repo_path=self.repo, runner=self.runner, **kwargs)

    def execution_preflight(self):
        executor = ActionExecutor(
            policy=AutomationPolicy(auto_delete_merged_branches=True,
                                    apply_categories=frozenset({"delete-merged-branch"})),
            ledger=AuditLedger(self.root / "audit.jsonl"), mode="dry-run",
            runner=self.runner, repo_paths={"repo": self.repo}, config=self.config,
        )
        return executor.execute(AutomationDecision(
            disposition="auto-act", category="delete-merged-branch",
            title="repo:topic can be deleted", reason="synthetic probe",
            recommended_action="synthetic probe", evidence=("default_ref=origin/main",),
        ))

    def linked(self, branch="topic", *, detach=False, name="linked worktree"):
        path = self.root / name
        args = ["--detach"] if detach else ["-b", branch]
        self.git("worktree", "add", *args, str(path), "HEAD")
        if not detach:
            self.git("config", f"branch.{branch}.remote", "origin")
            self.git("config", f"branch.{branch}.merge", f"refs/heads/{branch}")
        return path

    def assert_linked_preserved(self, path):
        state = scan_repo(self.repo, self.config)
        worktree = next(w for w in state.linked_worktrees if w.path == str(path))
        decision = decide_linked_worktree_cleanup(state, worktree)
        self.assertNotEqual(decision.category, "stale-linked-worktree-cleanup", decision)
        findings = classify_repo(state, self.config, datetime.now(timezone.utc))
        self.assertFalse(any(f.category == "stale-linked-worktree" and f.severity == "low"
                             and f.repo_path == str(path) for f in findings))
        self.assertTrue(path.exists())
        return worktree

    def test_dirty_staged_untracked_and_ignored_linked_worktrees_are_preserved(self):
        for kind in ("unstaged", "staged", "untracked", "ignored", "hidden-untracked"):
            with self.subTest(kind=kind):
                linked = self.linked(branch=kind, name=f"worktree {kind}")
                if kind in {"unstaged", "staged"}:
                    (linked / "base.txt").write_text("unfinished\n")
                    if kind == "staged":
                        self.git("add", "base.txt", cwd=linked)
                else:
                    (linked / "scratch.txt").write_text("unfinished\n")
                    if kind == "ignored":
                        self.git("config", "core.excludesFile", str(self.root / "ignore"))
                        (self.root / "ignore").write_text("scratch.txt\n")
                    if kind == "hidden-untracked":
                        self.git("config", "status.showUntrackedFiles", "no")
                state = self.assert_linked_preserved(linked)
                # The hidden-untracked iteration inherits the scratch.txt ignore rule.
                if kind in {"ignored", "hidden-untracked"}:
                    self.assertEqual(state.dirty_files, [])
                    self.assertEqual(state.untracked_files, [])
                    self.assertEqual(state.ignored_files, [])  # Not enumerated by a general scan.
                else:
                    self.assertTrue(state.dirty_files or state.untracked_files)

    def test_ignored_primary_work_blocks_branch_deletion(self):
        self.git("branch", "topic")
        (self.repo / ".git" / "info" / "exclude").write_text("scratch.txt\n")
        (self.repo / "scratch.txt").write_text("unfinished\n")
        self.assertFalse(self.report().passed)
        self.assertNotEqual(self.execution_preflight().status, "would-apply")
        state = scan_repo(self.repo, self.config)
        topic = next(b for b in state.branches if b.name == "topic")
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")

    def test_hidden_untracked_primary_work_blocks_branch_deletion(self):
        self.git("branch", "topic")
        self.git("config", "status.showUntrackedFiles", "no")
        (self.repo / "scratch.txt").write_text("unfinished\n")
        self.assertFalse(self.report().passed)
        self.assertNotEqual(self.execution_preflight().status, "would-apply")

    def test_gone_upstream_never_proves_unmerged_work_safe(self):
        linked = self.linked()
        self.commit("local.txt", "unmerged\n", cwd=linked)
        self.assert_linked_preserved(linked)
        self.assertFalse(self.report().passed)

    def test_checked_out_branch_with_dirty_work_is_not_a_deletion_candidate(self):
        linked = self.linked()
        self.git("config", "--remove-section", "branch.topic")
        (linked / "base.txt").write_text("unfinished\n")
        state = scan_repo(self.repo, self.config)
        topic = next(b for b in state.branches if b.name == "topic")
        self.assertTrue(topic.merged_to_default)
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")
        self.assertNotEqual(self.execution_preflight().status, "would-apply")

    def test_stash_keeps_branch_and_linked_worktree_out_of_cleanup(self):
        linked = self.linked()
        (linked / "base.txt").write_text("stashed unfinished work\n")
        self.git("stash", "push", "-m", "synthetic unfinished work", cwd=linked)
        stash = self.git("rev-parse", "refs/stash")
        self.assert_linked_preserved(linked)
        self.git("worktree", "remove", str(linked))
        self.git("config", "--remove-section", "branch.topic")
        self.assertFalse(self.report().passed)
        self.assertNotEqual(self.execution_preflight().status, "would-apply")
        state = scan_repo(self.repo, self.config)
        topic = next(b for b in state.branches if b.name == "topic")
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")
        self.assertEqual(self.git("rev-parse", "refs/stash"), stash)

    def test_submodule_ignored_work_cannot_be_called_clean(self):
        module = self.root / "module"
        self.git("init", "-b", "main", str(module), cwd=self.root)
        self.git("config", "user.name", "Safety Fixture", cwd=module)
        self.git("config", "user.email", "fixture@example.invalid", cwd=module)
        self.git("config", "commit.gpgsign", "false", cwd=module)
        self.commit("module.txt", "module\n", cwd=module)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", str(module), "module")
        self.git("commit", "-am", "synthetic submodule")
        self.publish()
        self.git("config", "submodule.module.ignore", "all")
        self.git("branch", "topic")
        nested = self.repo / "module"
        exclude = Path(self.git("rev-parse", "--absolute-git-dir", cwd=nested)) / "info" / "exclude"
        exclude.write_text("scratch.txt\n")
        (nested / "scratch.txt").write_text("ignored unfinished work\n")
        self.assertFalse(self.report().passed)
        self.assertNotEqual(self.execution_preflight().status, "would-apply")
        state = scan_repo(self.repo, self.config)
        topic = next(b for b in state.branches if b.name == "topic")
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")

    def test_detached_work_is_preserved(self):
        linked = self.linked(detach=True)
        self.commit("detached.txt", "unmerged\n", cwd=linked)
        worktree = self.assert_linked_preserved(linked)
        self.assertIsNone(worktree.branch)
        self.assertEqual(worktree.unique_commit_count, 1)

    def test_rebase_patch_equivalence_does_not_hide_new_work(self):
        linked = self.linked()
        self.commit("one.txt", "one\n", cwd=linked)
        topic = self.git("rev-parse", "HEAD", cwd=linked)
        self.commit("other.txt", "other\n")
        self.git("cherry-pick", topic)
        self.publish()
        self.assertTrue(self.git("cherry", "origin/main", "topic").startswith("-"))
        state = scan_repo(self.repo, self.config)
        self.assertEqual(state.linked_worktrees[0].unique_commit_count, 0)
        self.commit("unfinished.txt", "unmerged\n", cwd=linked)
        worktree = self.assert_linked_preserved(linked)
        self.assertEqual(worktree.unique_commit_count, 1)
        self.assertFalse(self.report().passed)

    def test_squash_equivalence_and_post_merge_changes_are_distinguished(self):
        self.git("switch", "-c", "topic")
        self.commit("one.txt", "one\n")
        self.commit("two.txt", "two\n")
        self.git("switch", "main")
        self.git("merge", "--squash", "topic")
        self.git("commit", "-m", "synthetic squash")
        merge = self.git("rev-parse", "HEAD")
        self.publish()
        self.git("config", "branch.topic.remote", "origin")
        self.git("config", "branch.topic.merge", "refs/heads/topic")
        self.merged = [{"mergeCommit": {"oid": merge}}]
        self.assertTrue(self.report().passed)
        self.git("switch", "topic")
        self.commit("unfinished.txt", "unmerged\n")
        self.git("switch", "main")
        self.assertFalse(self.report().passed)

    def test_squash_on_another_base_does_not_prove_default_publication(self):
        self.git("switch", "-c", "topic")
        self.commit("one.txt", "one\n")
        head = self.git("rev-parse", "HEAD")
        tree = self.git("rev-parse", "HEAD^{tree}")
        merge = self.git("commit-tree", tree, "-p", self.base, "-m", "synthetic other-base squash")
        self.git("switch", "main")
        self.git("config", "branch.topic.remote", "origin")
        self.git("config", "branch.topic.merge", "refs/heads/topic")
        self.merged = [{"mergeCommit": {"oid": merge}}]
        self.assertFalse(self.report().passed)
        self.assertFalse(self.report(expected_head=head, verified_merge_commit=merge).passed)

    def test_merge_only_commit_is_not_lost_by_patch_id_comparison(self):
        # All non-merge commits are on default; the unpublished merge has its tree.
        self.git("switch", "-c", "side")
        self.commit("side.txt", "side\n")
        self.git("switch", "-c", "topic", self.base)
        self.git("merge", "--no-ff", "side", "-m", "synthetic merge")
        self.git("switch", "main")
        self.git("merge", "--ff-only", "side")
        self.publish()
        linked = self.root / "linked"
        self.git("worktree", "add", str(linked), "topic")
        self.git("config", "branch.topic.remote", "origin")
        self.git("config", "branch.topic.merge", "refs/heads/topic")
        self.assertEqual(self.git("cherry", "origin/main", "topic"), "")
        self.assert_linked_preserved(linked)

    def test_tag_ambiguity_cannot_prove_an_unmerged_branch_safe(self):
        self.git("switch", "-c", "topic")
        self.commit("unfinished.txt", "unmerged\n")
        self.git("switch", "main")
        self.git("tag", "topic", "HEAD")
        self.assertFalse(self.report().passed)
        self.assertNotEqual(self.execution_preflight().status, "would-apply")
        state = scan_repo(self.repo, self.config)
        topic = next(b for b in state.branches if b.name == "topic")
        self.assertFalse(topic.merged_to_default)
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")

    def test_clean_merged_branch_remains_eligible(self):
        self.git("branch", "topic")
        self.assertTrue(self.report().passed)
        self.assertEqual(self.execution_preflight().status, "would-apply")

    def test_owned_worktree_ignored_files_block_even_with_pinned_merge(self):
        linked = self.repo / ".worktrees" / "task 修正"
        self.git("worktree", "add", "-b", "topic", str(linked))
        (self.repo / ".git" / "info" / "exclude").write_text(".worktrees/\nscratch.txt\n")
        self.merged = [{"mergeCommit": {"oid": self.base}}]
        kwargs = dict(owned_worktree=linked, expected_head=self.base, verified_merge_commit=self.base)
        clean = self.report(**kwargs)
        self.assertTrue(clean.passed, [p for p in clean.proofs if not p.passed])
        (linked / "scratch.txt").write_text("unfinished\n")
        self.assertFalse(self.report(**kwargs).passed)
        (linked / "scratch.txt").unlink()
        sibling = linked.parent / "other"
        sibling.mkdir()
        (sibling / "scratch.txt").write_text("unfinished sibling work\n")
        self.assertFalse(self.report(**kwargs).passed)

    def test_saved_work_inspection_failure_blocks_cleanup(self):
        self.git("branch", "topic")
        for failed in (["git", "stash", "list", "--format=%H"],
                       ["git", "ls-files", "--stage", "-z"]):
            for code in (124, 128):
                with self.subTest(command=failed[1], code=code):
                    def runner(args, cwd, timeout):
                        if args == failed:
                            return CommandResult(args, code, "", "synthetic inspection failure")
                        return self.runner(args, cwd, timeout)

                    self.assertFalse(evaluate_delete("topic", repo_path=self.repo, runner=runner).passed)

                    def scanner_runner(args, *, cwd=None, timeout=45):
                        return runner(args, cwd, timeout)

                    with patch("git_janitor.git.run_command", side_effect=scanner_runner):
                        # Keep real Git execution outside the patched module to avoid recursion.
                        state = scan_repo(self.repo, self.config)
                    topic = next(b for b in state.branches if b.name == "topic")
                    self.assertNotEqual(decide_branch_cleanup(state, topic).category,
                                        "delete-merged-branch")

    def test_tag_or_revision_expression_is_not_an_existing_local_branch(self):
        self.git("tag", "topic", "HEAD")
        for name in ("topic", "main~0"):
            report = self.report(name)
            self.assertFalse(report.passed)
            self.assertFalse(next(p.passed for p in report.proofs if p.name == "branch_exists"))

    def test_local_branch_cannot_shadow_the_remote_default(self):
        self.git("switch", "-c", "topic")
        self.commit("unfinished.txt", "unmerged\n")
        self.git("branch", "origin/main", "HEAD")
        self.git("switch", "main")
        self.assertFalse(self.report().passed)
        self.assertNotEqual(self.execution_preflight().status, "would-apply")
        state = scan_repo(self.repo, self.config)
        topic = next(b for b in state.branches if b.name == "topic")
        self.assertFalse(topic.merged_to_default)
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")

    def test_missing_default_head_does_not_fall_back_to_a_local_shadow(self):
        self.git("switch", "-c", "topic")
        self.commit("unfinished.txt", "unmerged\n")
        self.git("branch", "origin/main", "HEAD")
        self.git("switch", "main")
        self.git("symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        state = scan_repo(self.repo, self.config)
        topic = next(b for b in state.branches if b.name == "topic")
        self.assertFalse(topic.merged_to_default)
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")

    def test_same_name_tag_does_not_hide_the_current_branch(self):
        self.git("switch", "-c", "topic")
        self.git("tag", "topic", "HEAD")
        state = scan_repo(self.repo, self.config)
        topic = next(b for b in state.branches if b.name == "topic")
        self.assertTrue(topic.current)
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")

    def test_unicode_and_separator_branch_names_keep_their_identity(self):
        for branch in ("tópico/修正", "topic\u2028tail", "topic|tail"):
            self.git("branch", branch)
        state = scan_repo(self.repo, self.config)
        self.assertCountEqual([b.name for b in state.branches],
                              ["main", "tópico/修正", "topic\u2028tail", "topic|tail"])
        # Git forbids ASCII spaces in branch names; exercise that boundary explicitly.
        result = run_command(["git", "check-ref-format", "--branch", "topic with space"], self.repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.report("topic with space").passed)

    def test_non_origin_remote_fails_closed_without_a_known_default(self):
        self.git("branch", "topic")
        self.git("remote", "rename", "origin", "upstream")
        self.assertFalse(self.report().passed)
        state = scan_repo(self.repo, self.config)
        topic = next(b for b in state.branches if b.name == "topic")
        self.assertNotEqual(decide_branch_cleanup(state, topic).category, "delete-merged-branch")


if __name__ == "__main__":
    unittest.main()
