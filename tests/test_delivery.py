from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_janitor import pr_shepherd
from git_janitor.delivery import finish_delivery
from git_janitor.git import run_command
from git_janitor.models import CommandResult


class ReviewOverlapTests(unittest.TestCase):
    def test_only_nonempty_known_pass_and_pending_sets_are_reviewable(self):
        for code, checks, expected in (
            (0, [{"bucket": "pass"}], True),
            (8, [{"bucket": "pending"}, {"bucket": "pass"}], True),
            (1, [{"bucket": "pending"}], False),
            (8, [{"bucket": "fail"}, {"bucket": "pending"}], False),
            (0, [], False), (0, {}, False), (0, [{"bucket": "cancel"}], False),
            (0, [{"bucket": "skipping"}], False), (0, [{}], False),
        ):
            with self.subTest(code=code, checks=checks):
                def runner(args, cwd, timeout):
                    return CommandResult(args, code, json.dumps(checks), "")
                self.assertEqual(pr_shepherd.required_checks_reviewable(repo="o/r", pr=1, runner=runner), expected)
                if code == 8:
                    self.assertFalse(pr_shepherd.required_checks_green(repo="o/r", pr=1, runner=runner))

    def test_parser_keeps_overlap_opt_in(self):
        parser = pr_shepherd.build_parser()
        self.assertFalse(parser.parse_args(["review", "--high-risk"]).allow_pending_ci)
        self.assertTrue(parser.parse_args(["review", "--high-risk", "--allow-pending-ci"]).allow_pending_ci)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.root / "file.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", "https://github.com/o/r.git")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.worktree = self.root / ".worktrees" / "task"
        self.git("worktree", "add", "-b", "codex/task", str(self.worktree))
        (self.worktree / "file.txt").write_text("changed\n")
        self.git("add", ".", cwd=self.worktree)
        self.git("commit", "-m", "task", cwd=self.worktree)
        self.head = self.git("rev-parse", "HEAD", cwd=self.worktree)
        # A separate squash commit with an identical tree.
        tree = self.git("rev-parse", f"{self.head}^{{tree}}")
        self.merge = self.git("commit-tree", tree, "-p", self.base, "-m", "squashed")
        self.git("update-ref", "refs/remotes/origin/main", self.merge)
        # Keep another feature checkout active, matching the common real fleet state.
        self.git("switch", "-c", "user/other")
        (self.root / ".git" / "info" / "exclude").write_text(".worktrees/\n")
        self.ledger = Path(self.tmp.name).resolve() / "receipt.jsonl"
        self.state = "MERGED"
        self.pr_head = self.head
        self.threads = []
        self.calls = []
        self.move_before_delete = False
        self.open_prs = []
        self.commit_before_remove = False
        self.claim_after_remove = False

    def git(self, *args, cwd=None):
        result = run_command(["git", *args], cwd or self.root, 30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def runner(self, args, cwd, timeout):
        self.calls.append(args)
        value = None
        if args[:3] == ["git", "fetch", "--prune"]:
            value = ""
        elif args[:3] == ["gh", "api", "repos/o/r"]:
            value = json.dumps({"default_branch": "main"})
        elif args[:3] == ["gh", "pr", "view"]:
            value = json.dumps({"state": self.state, "headRefOid": self.pr_head,
                                "headRefName": "codex/task", "baseRefName": "main",
                                "mergeCommit": {"oid": self.merge}, "isCrossRepository": False})
        elif args[:3] == ["gh", "pr", "list"]:
            value = json.dumps(self.open_prs if "open" in args else [{"mergeCommit": {"oid": self.merge}}])
        if value is not None:
            return CommandResult(args, 0, value, "")
        if self.move_before_delete and args[:3] == ["git", "update-ref", "-d"]:
            self.git("update-ref", "refs/heads/codex/task", self.base)
        if self.commit_before_remove and args[:3] == ["git", "worktree", "remove"]:
            self.git("commit", "--allow-empty", "-m", "concurrent work", cwd=self.worktree)
        result = run_command(args, cwd, timeout)
        if self.claim_after_remove and args[:3] == ["git", "worktree", "remove"] and result.returncode == 0:
            self.git("worktree", "add", str(self.root / ".worktrees" / "claimed"), "codex/task")
        return result

    def finish(self):
        with patch.object(pr_shepherd, "list_threads", return_value=self.threads), redirect_stdout(io.StringIO()):
            return finish_delivery(repo="o/r", pr=1, worktree=self.worktree,
                                   expected_head=self.head, ledger_path=self.ledger,
                                   merge_options={}, runner=self.runner)

    def test_finish_cli_infers_target_pr_from_surviving_unrelated_checkout(self):
        def runner(args, cwd, timeout):
            if args == ["gh", "pr", "view", "codex/task", "--repo", "o/r", "--json", "number"]:
                self.assertEqual(cwd, self.worktree)
                return CommandResult(args, 0, '{"number": 1}', "")
            if args[:3] == ["gh", "pr", "view"]:
                return CommandResult(args, 1, "", "no pull requests found for caller branch")
            return run_command(args, cwd or self.root, timeout)

        with patch("git_janitor.delivery.finish_delivery", return_value=0) as finish:
            code = pr_shepherd.main([
                "finish", "--worktree", str(self.worktree), "--expected-head", self.head,
            ], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(finish.call_args.kwargs["repo"], "o/r")
        self.assertEqual(finish.call_args.kwargs["pr"], 1)
        self.assertEqual(self.git("branch", "--show-current"), "user/other")

    def test_finish_missing_target_requires_explicit_pr_instead_of_caller_branch(self):
        missing = self.root / ".worktrees" / "removed"
        with patch("git_janitor.delivery.finish_delivery") as finish, \
             patch("git_janitor.pr_shepherd.infer_pr") as infer:
            with self.assertRaises(SystemExit) as stopped:
                pr_shepherd.main(["finish", "--worktree", str(missing),
                                  "--expected-head", self.head], runner=self.runner)
        self.assertIn("--pr", str(stopped.exception))
        infer.assert_not_called()
        finish.assert_not_called()

    def test_finish_missing_target_explicit_pr_can_resume_from_parent(self):
        missing = self.root / ".worktrees" / "removed"
        calls = []
        def runner(args, cwd, timeout):
            calls.append((args, cwd))
            return run_command(args, cwd or self.root, timeout)
        with patch("git_janitor.delivery.finish_delivery", return_value=0) as finish:
            code = pr_shepherd.main(["--pr", "1", "finish", "--worktree", str(missing),
                                      "--expected-head", self.head], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(finish.call_args.kwargs["pr"], 1)
        self.assertEqual(calls, [(["git", "remote", "get-url", "origin"], missing.parent)])

    def test_squash_delivery_syncs_default_without_switching_user_branch_and_is_resumable(self):
        self.assertEqual(self.finish(), 0)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.git("rev-parse", "main"), self.merge)
        self.assertEqual(self.git("branch", "--show-current"), "user/other")
        self.assertEqual(self.finish(), 0)
        self.assertFalse(any(args[:3] == ["git", "push", "origin"] for args in self.calls))
        self.assertEqual(json.loads(self.ledger.read_text().splitlines()[-1])["status"], "complete")

    def test_unignored_owned_worktree_can_be_cleaned(self):
        (self.root / ".git" / "info" / "exclude").write_text("")
        self.assertEqual(self.finish(), 0)
        self.assertFalse(self.worktree.exists())

    def test_unignored_sibling_file_is_preserved(self):
        (self.root / ".git" / "info" / "exclude").write_text("")
        sibling = self.worktree.parent / "user-notes.txt"
        sibling.write_text("user state")
        self.assertEqual(self.finish(), 3)
        self.assertTrue(self.worktree.exists())
        self.assertEqual(sibling.read_text(), "user state")

    def test_unignored_sibling_worktree_is_preserved(self):
        (self.root / ".git" / "info" / "exclude").write_text("")
        sibling = self.worktree.parent / "other-task"
        self.git("worktree", "add", "-b", "user/second", str(sibling), self.base)
        self.assertEqual(self.finish(), 3)
        self.assertTrue(self.worktree.exists())
        self.assertTrue(sibling.exists())

    def test_generic_ignored_state_preserved(self):
        with (self.root / ".git" / "info" / "exclude").open("a") as stream:
            stream.write(".env.local\n")
        local = self.worktree / ".env.local"
        local.write_text("private fixture")
        self.assertEqual(self.finish(), 3)
        self.assertEqual(local.read_text(), "private fixture")

    def test_clean_concurrent_commit_before_removal_restores_worktree(self):
        self.commit_before_remove = True
        self.assertEqual(self.finish(), 3)
        advanced = self.git("rev-parse", "codex/task")
        self.assertNotEqual(advanced, self.head)
        self.assertTrue(self.worktree.exists())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worktree), advanced)
        receipt = json.loads(self.ledger.read_text().splitlines()[-1])
        self.assertTrue(receipt["worktree_restored"])

    def test_branch_claimed_after_removal_is_preserved_with_partial_receipt(self):
        self.claim_after_remove = True
        self.assertEqual(self.finish(), 3)
        self.assertEqual(self.git("rev-parse", "codex/task"), self.head)
        self.assertTrue((self.root / ".worktrees" / "claimed").exists())
        self.assertEqual(json.loads(self.ledger.read_text().splitlines()[-1])["status"], "partial_cleanup")

    def test_default_checkout_sync_excludes_only_unignored_owned_task(self):
        self.git("switch", "main")
        (self.root / ".git" / "info" / "exclude").write_text("")
        self.assertEqual(self.finish(), 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.merge)
        self.assertFalse(self.worktree.exists())

    def test_default_checkout_sync_preserves_unignored_sibling_dirt(self):
        self.git("switch", "main")
        (self.root / ".git" / "info" / "exclude").write_text("")
        sibling = self.worktree.parent / "user-note.txt"
        sibling.write_text("user state")
        self.assertEqual(self.finish(), 3)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertTrue(self.worktree.exists())
        self.assertEqual(sibling.read_text(), "user state")

    def test_dirty_task_is_preserved(self):
        (self.worktree / "untracked.txt").write_text("user work")
        self.assertEqual(self.finish(), 3)
        self.assertTrue(self.worktree.exists())

    def test_changed_head_is_preserved_without_merge(self):
        self.pr_head = self.base
        self.state = "OPEN"
        with patch.object(pr_shepherd, "guarded_merge") as merge:
            self.assertEqual(self.finish(), 3)
        merge.assert_not_called()
        self.assertTrue(self.worktree.exists())

    def test_pending_auto_merge_can_resume(self):
        self.state = "OPEN"
        with patch.object(pr_shepherd, "guarded_merge", return_value=0) as merge:
            self.assertEqual(self.finish(), 2)
        self.assertEqual(merge.call_args.kwargs["expected_head"], self.head)
        self.assertTrue(merge.call_args.kwargs["preserve_head_branch"])
        self.assertTrue(self.worktree.exists())
        self.state = "MERGED"
        self.assertEqual(self.finish(), 0)

    def test_cleanup_ref_cas_preserves_concurrently_advanced_branch(self):
        self.move_before_delete = True
        self.assertEqual(self.finish(), 3)
        self.assertEqual(self.git("rev-parse", "codex/task"), self.base)

    def test_merge_tree_mismatch_preserves(self):
        self.merge = self.base
        self.assertEqual(self.finish(), 3)
        self.assertTrue(self.worktree.exists())

    def test_open_pr_preserves(self):
        for key, value in (("open_prs", [{"number": 2}]),):
            with self.subTest(key=key):
                setattr(self, key, value)
                self.assertEqual(self.finish(), 3)
                self.assertTrue(self.worktree.exists())
                setattr(self, key, [])

    def test_locked_worktree_and_opt_out_preserved(self):
        self.git("worktree", "lock", str(self.worktree))
        self.assertEqual(self.finish(), 3)
        self.git("worktree", "unlock", str(self.worktree))
        (self.worktree / ".no-cleanup").touch()
        self.assertEqual(self.finish(), 3)
        self.assertTrue(self.worktree.exists())

    def test_ignored_local_analysis_is_preserved(self):
        with (self.root / ".git" / "info" / "exclude").open("a") as stream:
            stream.write(".understand-anything/\n")
        (self.worktree / ".understand-anything").mkdir()
        (self.worktree / ".understand-anything" / "graph.json").write_text("{}")
        self.assertEqual(self.finish(), 3)
        self.assertTrue((self.worktree / ".understand-anything" / "graph.json").exists())

    def test_divergent_default_preserved(self):
        self.git("update-ref", "refs/heads/main", self.head)
        self.assertEqual(self.finish(), 3)
        self.assertTrue(self.worktree.exists())

    def test_ledger_inside_task_refused(self):
        self.ledger = self.worktree / "receipt.jsonl"
        self.assertEqual(self.finish(), 3)
        self.assertTrue(self.worktree.exists())

    def test_main_checkout_refused(self):
        self.worktree = self.root
        self.assertEqual(self.finish(), 3)
        self.assertTrue(self.root.exists())
