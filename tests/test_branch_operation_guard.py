"""Detached operation ownership in temporary Git repos; GitHub reads are fixtures."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

import test_delivery as fixtures
from git_janitor import safe_delete
from git_janitor.models import CommandResult


class BranchOperationGuardTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def fixture(self, *, linked, finish=False, outside=False):
        f = fixtures.DeliveryTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        # A midpoint lets a real bisect detach HEAD without ending immediately.
        f.git("commit", "--allow-empty", "-m", "second synthetic task commit", cwd=f.worktree)
        f.head = f.git("rev-parse", "HEAD", cwd=f.worktree)
        f.pr_head = f.head
        f.merge = f.git("commit-tree", f"{f.head}^{{tree}}", "-p", f.base, "-m", "synthetic squash")
        f.git("update-ref", "refs/remotes/origin/main", f.merge)
        # Model the usual post-merge remote branch removal so the public
        # upstream-or-ancestry proof reaches the new operation-ownership guard.
        f.git("config", "branch.codex/task.remote", "origin")
        f.git("config", "branch.codex/task.merge", "refs/heads/codex/task")
        if not linked or finish:
            f.git("worktree", "remove", str(f.worktree))
        if linked:
            operation = f.root.parent / "operation" if finish or outside else f.worktree
            if finish:
                f.git("worktree", "add", str(operation), "codex/task")
            elif outside:
                # A separate linked checkout must not be hidden behind the
                # public tool's independent ignored-files cleanup blocker.
                f.git("worktree", "move", str(f.worktree), str(operation))
                f.worktree = operation
        else:
            operation = f.root
            f.git("switch", "codex/task")
        return f, operation

    def start_operation(self, f, worktree, operation, *, rebase_owner="refs/heads/codex/task"):
        git_dir = Path(f.git("rev-parse", "--absolute-git-dir", cwd=worktree))
        if operation == "rebase":
            editor = f.root.parent / "edit-first.py"
            editor.write_text("import pathlib,sys\np=pathlib.Path(sys.argv[1])\n"
                              "p.write_text(p.read_text().replace('pick ', 'edit ', 1))\n")
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(editor))}"
            f.git("-c", f"sequence.editor={command}", "rebase", "-i", f.base, cwd=worktree)
            self.assertEqual((git_dir / "rebase-merge/head-name").read_text(), rebase_owner + "\n")
        else:
            f.git("bisect", "start", "HEAD", f.base, cwd=worktree)
            self.assertEqual((git_dir / "BISECT_START").read_text(), "codex/task\n")
        self.assertEqual(f.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree), "HEAD")
        self.assertEqual(f.git("status", "--porcelain=v1", cwd=worktree), "")
        self.assertNotIn("branch refs/heads/codex/task",
                         f.git("worktree", "list", "--porcelain").splitlines())

    def assert_no_ref_deletion(self, f):
        self.assertEqual(f.git("rev-parse", "refs/heads/codex/task"), f.head)
        self.assertFalse(any(args[:3] == ["git", "update-ref", "-d"] for args in f.calls))

    def test_safe_delete_dry_run_and_execute_hold_detached_rebase_and_bisect(self):
        for operation in ("rebase", "bisect"):
            for linked in (False, True):
                with self.subTest(operation=operation, linked=linked):
                    f, worktree = self.fixture(linked=linked, outside=True)
                    self.start_operation(f, worktree, operation)
                    for execute in (False, True):
                        args = ["codex/task", "--repo", str(f.root), "--ledger", str(f.ledger)]
                        if execute:
                            args.append("--execute")
                        with redirect_stdout(io.StringIO()):
                            code = safe_delete.git_safe_delete_main(args, runner=f.runner)
                        receipt = json.loads(f.ledger.read_text().splitlines()[-1])
                        self.assertEqual((code, receipt["status"]), (3, "blocked"), receipt)
                        operation_proofs = [p for p in receipt["proofs"]
                                            if p["name"] == "no_branch_operation"]
                        self.assertTrue(operation_proofs, receipt)
                        self.assertFalse(operation_proofs[-1]["passed"])
                        self.assert_no_ref_deletion(f)

    def test_finish_holds_detached_rebase_and_bisect_in_main_and_linked_worktrees(self):
        for operation in ("rebase", "bisect"):
            for linked in (False, True):
                with self.subTest(operation=operation, linked=linked):
                    f, worktree = self.fixture(linked=linked, finish=True)
                    self.start_operation(f, worktree, operation)
                    self.assertEqual(f.finish(), 3, f.ledger.read_text())
                    self.assert_no_ref_deletion(f)
                    receipt = json.loads(f.ledger.read_text().splitlines()[-1])
                    operation_proof = receipt["cleanup_proof"]["proofs"][-1]
                    self.assertEqual(operation_proof["name"], "no_branch_operation")
                    self.assertFalse(operation_proof["passed"])

    def test_finish_rechecks_new_operation_after_task_worktree_removal(self):
        for operation in ("rebase", "bisect"):
            with self.subTest(operation=operation):
                f, _ = self.fixture(linked=True)
                original = f.runner

                def runner(args, cwd, timeout):
                    result = original(args, cwd, timeout)
                    if args[:3] == ["git", "worktree", "remove"] and result.returncode == 0:
                        f.git("switch", "codex/task")
                        self.start_operation(f, f.root, operation)
                    return result

                f.runner = runner
                self.assertEqual(f.finish(), 3, f.ledger.read_text())
                self.assert_no_ref_deletion(f)
                self.assertFalse(any(args[:3] == ["git", "worktree", "add"] for args in f.calls))
                receipt = json.loads(f.ledger.read_text().splitlines()[-1])
                self.assertEqual(receipt["status"], "partial_cleanup")
                self.assertFalse(receipt["cleanup_proof"]["proofs"][-1]["passed"])

    def test_apply_backend_and_malformed_markers_fail_closed(self):
        f, _ = self.fixture(linked=True)
        marker = f.root / ".git/rebase-apply/head-name"
        marker.parent.mkdir()
        for content in (b"refs/heads/codex/task\n", b"codex/task\n", b"\xff", b"",
                        b"bad\x00state", b"bad owner\n", b"user/other\nextra\n", b"x" * 8193):
            with self.subTest(content=content[:40]):
                marker.write_bytes(content)
                proof = safe_delete.branch_operation_proof("codex/task", repo_path=f.root, runner=f.runner)
                self.assertFalse(proof.passed, proof)

    def test_unreadable_worktree_git_dir_fails_closed(self):
        f, _ = self.fixture(linked=True)

        def runner(args, cwd, timeout):
            if args == ["git", "rev-parse", "--absolute-git-dir"] and cwd == f.worktree:
                return CommandResult(args, 1, "", "synthetic metadata read failure")
            return f.runner(args, cwd, timeout)

        proof = safe_delete.branch_operation_proof("codex/task", repo_path=f.root, runner=runner)
        self.assertFalse(proof.passed, proof)

    def test_unreadable_marker_fails_closed(self):
        f, _ = self.fixture(linked=True)
        marker = f.root / ".git/BISECT_START"
        marker.write_text("user/other\n")
        original_open = os.open

        def unreadable(path, flags, *args, **kwargs):
            if path == "BISECT_START":
                raise PermissionError("synthetic unreadable operation marker")
            return original_open(path, flags, *args, **kwargs)

        with patch.object(os, "open", unreadable):
            proof = safe_delete.branch_operation_proof("codex/task", repo_path=f.root, runner=f.runner)
        self.assertFalse(proof.passed, proof)

    def test_symlink_and_fifo_markers_fail_closed_without_following_or_blocking(self):
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                f, _ = self.fixture(linked=True)
                marker = f.root / ".git/BISECT_START"
                unrelated = f.root.parent / "unrelated-file"
                unrelated.write_text("user/other\n")
                if kind == "symlink":
                    marker.symlink_to(unrelated)
                else:
                    os.mkfifo(marker)
                proof = safe_delete.branch_operation_proof("codex/task", repo_path=f.root, runner=f.runner)
                self.assertFalse(proof.passed, proof)
                self.assertEqual(unrelated.read_text(), "user/other\n")

    def test_state_directory_and_git_directory_aliases_fail_closed(self):
        f, _ = self.fixture(linked=True)
        outside = f.root.parent / "unrelated-state"
        outside.mkdir()
        (outside / "head-name").write_text("user/other\n")
        (f.root / ".git/rebase-merge").symlink_to(outside, target_is_directory=True)
        proof = safe_delete.branch_operation_proof("codex/task", repo_path=f.root, runner=f.runner)
        self.assertFalse(proof.passed, proof)
        (f.root / ".git/rebase-merge").unlink()
        alias = f.root.parent / "git-directory-alias"
        alias.symlink_to(f.root / ".git", target_is_directory=True)

        def runner(args, cwd, timeout):
            if args == ["git", "rev-parse", "--absolute-git-dir"]:
                return CommandResult(args, 0, str(alias), "")
            return f.runner(args, cwd, timeout)

        proof = safe_delete.branch_operation_proof("codex/task", repo_path=f.root, runner=runner)
        self.assertFalse(proof.passed, proof)
        self.assertEqual((outside / "head-name").read_text(), "user/other\n")

    def test_other_branch_operation_does_not_block_the_target(self):
        f, _ = self.fixture(linked=True)
        marker = f.root / ".git/BISECT_START"
        for owner in ("user/other", "refs/heads/user/other"):
            with self.subTest(owner=owner):
                marker.write_text(owner + "\n")
                proof = safe_delete.branch_operation_proof("codex/task", repo_path=f.root, runner=f.runner)
                self.assertTrue(proof.passed, proof)

    def test_rebase_started_detached_allows_unrelated_branch_cleanup(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                f, worktree = self.fixture(linked=linked, outside=True)
                f.git("switch", "--detach", cwd=worktree)
                self.start_operation(f, worktree, "rebase", rebase_owner="detached HEAD")
                git_dir = Path(f.git("rev-parse", "--absolute-git-dir", cwd=worktree))
                marker = git_dir / "rebase-merge/head-name"
                before = marker.read_bytes()
                for execute in (False, True):
                    args = ["codex/task", "--repo", str(f.root), "--ledger", str(f.ledger)]
                    if execute:
                        args.append("--execute")
                    with redirect_stdout(io.StringIO()):
                        code = safe_delete.git_safe_delete_main(args, runner=f.runner)
                    self.assertEqual(code, 0, f.ledger.read_text())
                    self.assertEqual(marker.read_bytes(), before)
                    self.assertEqual(f.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree), "HEAD")
                self.assertEqual(f.git("branch", "--list", "codex/task"), "")

    def test_apply_rebase_started_detached_has_no_branch_owner(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                f, worktree = self.fixture(linked=linked, outside=True)
                upstream = f.root.parent / "upstream"
                f.git("worktree", "add", "-b", "synthetic-upstream", str(upstream), f.base)
                (upstream / "file.txt").write_text("conflicting upstream\n")
                f.git("commit", "-am", "synthetic conflicting upstream", cwd=upstream)
                target = f.git("rev-parse", "HEAD", cwd=upstream)
                f.git("switch", "--detach", cwd=worktree)
                result = safe_delete.run_command(["git", "rebase", "--apply", target], worktree, 30)
                self.assertEqual(result.returncode, 1, result)
                git_dir = Path(f.git("rev-parse", "--absolute-git-dir", cwd=worktree))
                marker = git_dir / "rebase-apply/head-name"
                self.assertEqual(marker.read_bytes(), b"detached HEAD\n")
                proof = safe_delete.branch_operation_proof("codex/task", repo_path=f.root, runner=f.runner)
                self.assertTrue(proof.passed, proof)
                self.assertEqual(marker.read_bytes(), b"detached HEAD\n")
                self.assertEqual(f.git("rev-parse", "refs/heads/codex/task"), f.head)

    def test_no_owner_sentinel_is_exact_and_only_valid_for_rebase(self):
        for marker_name in ("rebase-merge/head-name", "rebase-apply/head-name", "BISECT_START"):
            with self.subTest(marker=marker_name):
                f, _ = self.fixture(linked=True)
                marker = f.root / ".git" / marker_name
                marker.parent.mkdir(exist_ok=True)
                for raw in (b"detached HEAD", b"detached HEAD\n", b" detached HEAD\n",
                            b"detached HEAD \n", b"detached HEAD\nextra\n"):
                    with self.subTest(raw=raw):
                        marker.write_bytes(raw)
                        proof = safe_delete.branch_operation_proof("codex/task", repo_path=f.root,
                                                                  runner=f.runner)
                        expected = marker_name != "BISECT_START" and raw in (b"detached HEAD", b"detached HEAD\n")
                        self.assertEqual(proof.passed, expected, proof)
