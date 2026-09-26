from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from git_janitor import pr_shepherd


class _PersistentAlertGroupProcess:
    pid = 424242
    returncode = None

    def __init__(self) -> None:
        self.communications = 0

    def communicate(self, **_kwargs: object) -> tuple[str, str]:
        self.communications += 1
        if self.communications == 1:
            raise subprocess.TimeoutExpired(["alert"], 1)
        self.returncode = -signal.SIGKILL
        return "", ""


class _SuccessfulAlertProcess:
    pid = 424242
    returncode = 0

    def __init__(self) -> None:
        self.communications = 0

    def communicate(self, **_kwargs: object) -> tuple[str, str]:
        self.communications += 1
        return "", ""


def _watch_record(
    *, completed_at: str, status: str, outcome: str, run_index: int = 1
) -> dict[str, object]:
    return {
        "schema": pr_shepherd.WATCH_RECEIPT_SCHEMA,
        "completed_at": completed_at,
        "status": status,
        "outcome": outcome,
        "exit_code": 3 if status == "failure" else 0,
        "facts_refresh": "succeeded",
        "facts_cache": "available",
        "stalls_count": 0,
        "orphans_count": 0,
        "alert_delivery": "not_needed",
        "destination_alias": "fleet-ops",
        "destination_target_sha256": "a" * 64,
        "failure_codes": ["synthetic_failure"] if status == "failure" else [],
        "run_id": f"{run_index:064x}",
        "runtime_sha": "b" * 40,
        "runtime_manifest_sha256": "c" * 64,
        "gh_executable_sha256": "d" * 64,
        "hermes_runtime_sha": "e" * 40,
        "hermes_executable_sha256": "f" * 64,
        "hermes_runtime_manifest_sha256": "9" * 64,
        "process_id": 4242,
    }


@unittest.skip("retired direct Codex review path")
class WaitForReviewTests(unittest.TestCase):
    def _runner(
        self,
        *,
        checks_returncode: int = 0,
        comments: list[dict[str, object]] | None = None,
        comments_stdout: str | None = None,
        comment_returncode: int = 0,
        review_present: bool = False,
        head: str = "abc123",
        review_commit: str | None = None,
    ) -> tuple[object, list[list[str]]]:
        commands: list[list[str]] = []

        def runner(
            args: list[str], _cwd: Path | None, _timeout: int
        ) -> pr_shepherd.CommandResult:
            commands.append(args)
            if args[:3] == ["gh", "pr", "view"]:
                return pr_shepherd.CommandResult(
                    args, 0, json.dumps({"headRefOid": head}), ""
                )
            if args[:3] == ["gh", "pr", "checks"]:
                return pr_shepherd.CommandResult(
                    args=args,
                    returncode=checks_returncode,
                    stdout=(
                        json.dumps([{"bucket": "pass", "state": "SUCCESS"}])
                        if checks_returncode == 0
                        else ""
                    ),
                    stderr="checks are not green" if checks_returncode else "",
                )
            if args[:2] == ["gh", "api"] and args[-1].endswith("/reviews"):
                reviews = (
                    [
                        {
                            "user": {"login": pr_shepherd.BOT_LOGIN},
                            "commit_id": review_commit or head,
                        }
                    ]
                    if review_present
                    else []
                )
                return pr_shepherd.CommandResult(args, 0, json.dumps(reviews), "")
            if args[:2] == ["gh", "api"] and "--slurp" in args:
                return pr_shepherd.CommandResult(
                    args,
                    0,
                    comments_stdout
                    if comments_stdout is not None
                    else json.dumps([comments or []]),
                    "",
                )
            if args[:3] == ["gh", "pr", "comment"]:
                return pr_shepherd.CommandResult(
                    args,
                    comment_returncode,
                    "commented" if comment_returncode == 0 else "",
                    "comment failed" if comment_returncode else "",
                )
            raise AssertionError(f"unexpected command: {args}")

        return runner, commands

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.lock_dir = Path(self._temp_dir.name)

    def test_default_wait_is_ten_minutes(self) -> None:
        args = pr_shepherd.build_parser().parse_args(
            ["--repo", "owner/repo", "--pr", "7", "wait", "--high-risk"]
        )

        self.assertEqual(args.timeout, 600)

    def test_nudge_requires_explicit_high_risk_acknowledgement(self) -> None:
        runner, commands = self._runner()

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=True,
            high_risk=False,
            lock_dir=self.lock_dir,
            runner=runner,
        )

        self.assertEqual(result, 2)
        self.assertEqual(commands, [])

    def test_nudge_refuses_before_required_checks_are_green(self) -> None:
        runner, commands = self._runner(checks_returncode=8)

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=True,
            high_risk=True,
            lock_dir=self.lock_dir,
            runner=runner,
        )

        self.assertEqual(result, 3)
        self.assertTrue(any(command[:3] == ["gh", "pr", "checks"] for command in commands))
        self.assertFalse(any(command[:3] == ["gh", "pr", "comment"] for command in commands))

    def test_empty_required_check_set_is_not_treated_as_green(self) -> None:
        runner, commands = self._runner()

        def empty_checks_runner(
            args: list[str], cwd: Path | None, timeout: int
        ) -> pr_shepherd.CommandResult:
            if args[:3] == ["gh", "pr", "checks"]:
                return pr_shepherd.CommandResult(args, 0, "[]", "")
            return runner(args, cwd, timeout)

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=True,
            high_risk=True,
            lock_dir=self.lock_dir,
            runner=empty_checks_runner,
        )

        self.assertEqual(result, 3)
        self.assertFalse(any(command[:3] == ["gh", "pr", "comment"] for command in commands))

    def test_existing_request_is_not_posted_again(self) -> None:
        runner, commands = self._runner(comments=[{"body": "  @codex review  "}])

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=True,
            high_risk=True,
            lock_dir=self.lock_dir,
            runner=runner,
        )

        self.assertEqual(result, 2)
        self.assertFalse(any(command[:3] == ["gh", "pr", "comment"] for command in commands))

    def test_first_request_is_posted_once(self) -> None:
        runner, commands = self._runner()

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=True,
            high_risk=True,
            lock_dir=self.lock_dir,
            runner=runner,
        )

        self.assertEqual(result, 2)
        comments = [command for command in commands if command[:3] == ["gh", "pr", "comment"]]
        self.assertEqual(len(comments), 1)

    def test_new_request_unanswered_for_ten_minutes_permits_guarded_merge(self) -> None:
        runner, commands = self._runner()

        with patch.object(pr_shepherd.time, "monotonic", side_effect=[0.0, 600.0]):
            result = pr_shepherd.wait_for_review(
                repo="owner/repo",
                pr=7,
                timeout_seconds=600,
                poll_interval=0,
                nudge=True,
                high_risk=True,
                lock_dir=self.lock_dir,
                runner=runner,
            )

        self.assertEqual(result, 0)
        comments = [command for command in commands if command[:3] == ["gh", "pr", "comment"]]
        self.assertEqual(len(comments), 1)

    def test_existing_request_unanswered_for_ten_minutes_permits_guarded_merge(self) -> None:
        runner, _commands = self._runner(comments=[{"body": "@codex review"}])

        with patch.object(pr_shepherd.time, "monotonic", side_effect=[0.0, 600.0]):
            result = pr_shepherd.wait_for_review(
                repo="owner/repo",
                pr=7,
                timeout_seconds=600,
                poll_interval=0,
                nudge=False,
                high_risk=True,
                lock_dir=self.lock_dir,
                runner=runner,
            )

        self.assertEqual(result, 0)

    def test_ten_minute_timeout_without_request_fails_closed(self) -> None:
        runner, _commands = self._runner()

        with patch.object(pr_shepherd.time, "monotonic", side_effect=[0.0, 600.0]):
            result = pr_shepherd.wait_for_review(
                repo="owner/repo",
                pr=7,
                timeout_seconds=600,
                poll_interval=0,
                nudge=False,
                high_risk=True,
                lock_dir=self.lock_dir,
                runner=runner,
            )

        self.assertEqual(result, 3)

    def test_failed_request_post_fails_closed(self) -> None:
        runner, commands = self._runner(comment_returncode=1)

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=True,
            high_risk=True,
            lock_dir=self.lock_dir,
            runner=runner,
        )

        self.assertEqual(result, 3)
        comments = [command for command in commands if command[:3] == ["gh", "pr", "comment"]]
        self.assertEqual(len(comments), 1)

    def test_existing_review_needs_no_check_or_nudge(self) -> None:
        runner, commands = self._runner(checks_returncode=8, review_present=True)

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=True,
            high_risk=True,
            lock_dir=self.lock_dir,
            runner=runner,
        )

        self.assertEqual(result, 0)
        self.assertFalse(any(command[:3] == ["gh", "pr", "checks"] for command in commands))
        self.assertFalse(any(command[:3] == ["gh", "pr", "comment"] for command in commands))

    def test_wait_without_nudge_still_requires_high_risk(self) -> None:
        runner, commands = self._runner()

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=False,
            high_risk=False,
            lock_dir=self.lock_dir,
            runner=runner,
        )

        self.assertEqual(result, 2)
        self.assertEqual(commands, [])

    def test_stale_review_does_not_satisfy_current_head(self) -> None:
        runner, commands = self._runner(
            review_present=True, head="new-head", review_commit="old-head"
        )

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=False,
            high_risk=True,
            lock_dir=self.lock_dir,
            runner=runner,
        )

        self.assertEqual(result, 2)
        self.assertTrue(any(command[-1].endswith("/reviews") for command in commands))

    def test_unanswered_timeout_fails_if_pr_head_changes(self) -> None:
        base_runner, _commands = self._runner(head="abc123")
        head_reads = 0

        def moving_runner(
            args: list[str], cwd: Path | None, timeout: int
        ) -> pr_shepherd.CommandResult:
            nonlocal head_reads
            if args[:3] == ["gh", "pr", "view"]:
                head_reads += 1
                head = "abc123" if head_reads == 1 else "def456"
                return pr_shepherd.CommandResult(
                    args,
                    0,
                    json.dumps({"headRefOid": head}),
                    "",
                )
            return base_runner(args, cwd, timeout)

        with patch.object(pr_shepherd.time, "monotonic", side_effect=[0.0, 0.0]):
            result = pr_shepherd.wait_for_review(
                repo="owner/repo",
                pr=7,
                timeout_seconds=600,
                poll_interval=0,
                nudge=False,
                high_risk=True,
                expected_head_sha="abc123",
                lock_dir=self.lock_dir,
                runner=moving_runner,
            )

        self.assertEqual(result, 3)

    def test_malformed_comment_history_fails_closed(self) -> None:
        runner, commands = self._runner(comments_stdout="not-json")

        result = pr_shepherd.wait_for_review(
            repo="owner/repo",
            pr=7,
            timeout_seconds=0,
            poll_interval=0,
            nudge=True,
            high_risk=True,
            lock_dir=self.lock_dir,
            runner=runner,
        )

        self.assertEqual(result, 3)
        self.assertFalse(any(command[:3] == ["gh", "pr", "comment"] for command in commands))

    def test_concurrent_nudges_post_only_once(self) -> None:
        runner, commands = self._runner()

        def invoke() -> int:
            return pr_shepherd.wait_for_review(
                repo="owner/repo",
                pr=7,
                timeout_seconds=0,
                poll_interval=0,
                nudge=True,
                high_risk=True,
                lock_dir=self.lock_dir,
                runner=runner,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: invoke(), range(2)))

        self.assertEqual(results, [2, 2])
        comments = [command for command in commands if command[:3] == ["gh", "pr", "comment"]]
        self.assertEqual(len(comments), 1)


class WatchAlertBodyTests(unittest.TestCase):
    def test_new_watch_receipt_commits_empty_name_before_success_payload(self) -> None:
        real_fsync = os.fsync
        sync_kinds: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipts" / "watch.jsonl"

            def record_sync(fd: int) -> None:
                mode = os.fstat(fd).st_mode
                sync_kinds.append("file" if stat.S_ISREG(mode) else "directory")
                real_fsync(fd)

            with patch.object(pr_shepherd.os, "fsync", side_effect=record_sync):
                accepted = pr_shepherd.append_watch_receipt(
                    receipt_path,
                    _watch_record(
                        completed_at="2026-07-15T12:00:00Z",
                        status="success",
                        outcome="clean",
                    ),
                )

        self.assertTrue(accepted)
        self.assertEqual(sync_kinds[-3:], ["file", "directory", "file"])

    def test_new_watch_receipt_directory_sync_failure_cannot_report_success(self) -> None:
        real_fsync = os.fsync
        file_synced = False
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipts" / "watch.jsonl"

            def fail_after_file_sync(fd: int) -> None:
                nonlocal file_synced
                mode = os.fstat(fd).st_mode
                if stat.S_ISREG(mode):
                    file_synced = True
                    real_fsync(fd)
                    return
                if file_synced:
                    raise OSError("synthetic containing-directory fsync failure")
                real_fsync(fd)

            with (
                patch.object(pr_shepherd.os, "fsync", side_effect=fail_after_file_sync),
                self.assertRaises(OSError),
            ):
                pr_shepherd.append_watch_receipt(
                    receipt_path,
                    _watch_record(
                        completed_at="2026-07-15T12:00:00Z",
                        status="success",
                        outcome="clean",
                    ),
                )
            receipt_bytes = receipt_path.read_bytes()

        self.assertTrue(file_synced)
        self.assertEqual(receipt_bytes, b"")

    def test_zero_length_receipt_retries_durable_initialization_before_append(self) -> None:
        real_fsync = os.fsync
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipts" / "watch.jsonl"
            record = _watch_record(
                completed_at="2026-07-15T12:00:00Z",
                status="success",
                outcome="clean",
            )
            for attempt in range(2):
                sync_kinds: list[str] = []
                receipt_file_synced = False

                def fail_directory_sync(fd: int) -> None:
                    nonlocal receipt_file_synced
                    kind = "file" if stat.S_ISREG(os.fstat(fd).st_mode) else "directory"
                    if kind == "file":
                        receipt_file_synced = True
                    if receipt_file_synced:
                        sync_kinds.append(kind)
                    if kind == "directory" and receipt_file_synced:
                        raise OSError(f"synthetic retry {attempt} directory failure")
                    if kind != "directory" or not receipt_file_synced:
                        real_fsync(fd)

                with (
                    patch.object(
                        pr_shepherd.os,
                        "fsync",
                        side_effect=fail_directory_sync,
                    ),
                    self.assertRaises(OSError),
                ):
                    pr_shepherd.append_watch_receipt(receipt_path, record)
                self.assertEqual(receipt_path.read_bytes(), b"")
                self.assertEqual(sync_kinds, ["file", "directory"])

            successful_syncs: list[str] = []

            def record_sync(fd: int) -> None:
                successful_syncs.append(
                    "file" if stat.S_ISREG(os.fstat(fd).st_mode) else "directory"
                )
                real_fsync(fd)

            with patch.object(pr_shepherd.os, "fsync", side_effect=record_sync):
                accepted = pr_shepherd.append_watch_receipt(receipt_path, record)

        self.assertTrue(accepted)
        self.assertEqual(successful_syncs, ["file", "directory", "file"])

    def test_watch_receipt_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_dir = Path(tmp) / "receipts"
            receipt_dir.mkdir(mode=0o700)
            fifo = receipt_dir / "watch.jsonl"
            os.mkfifo(fifo, mode=0o600)

            with self.assertRaises(OSError):
                pr_shepherd.append_watch_receipt(
                    fifo,
                    _watch_record(
                        completed_at="2026-07-15T12:00:00Z",
                        status="success",
                        outcome="clean",
                    ),
                )

    def test_watch_receipt_rejects_hardlink_and_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_dir = Path(tmp) / "receipts"
            receipt_dir.mkdir(mode=0o700)
            original = receipt_dir / "original.jsonl"
            original.write_text("", encoding="utf-8")
            original.chmod(0o600)
            hardlink = receipt_dir / "hardlink.jsonl"
            hardlink.hardlink_to(original)
            symlink = receipt_dir / "symlink.jsonl"
            symlink.symlink_to(original)

            with self.assertRaises(OSError):
                pr_shepherd.append_watch_receipt(
                    hardlink,
                    _watch_record(
                        completed_at="2026-07-15T12:00:00Z",
                        status="success",
                        outcome="clean",
                    ),
                )
            with self.assertRaises(OSError):
                pr_shepherd.append_watch_receipt(
                    symlink,
                    _watch_record(
                        completed_at="2026-07-15T12:00:00Z",
                        status="success",
                        outcome="clean",
                    ),
                )

    def test_watch_receipt_rejects_ancestor_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir(mode=0o700)
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)

            with self.assertRaises(OSError):
                pr_shepherd.append_watch_receipt(
                    linked / "receipts" / "watch.jsonl",
                    _watch_record(
                        completed_at="2026-07-15T12:00:00Z",
                        status="success",
                        outcome="clean",
                    ),
                )

    def test_watch_receipt_detects_named_file_replacement_after_fsync(self) -> None:
        real_fsync = os.fsync
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipts" / "watch.jsonl"

            def replace_named_file(fd: int) -> None:
                real_fsync(fd)
                stale = receipt_path.with_suffix(".stale")
                receipt_path.rename(stale)
                receipt_path.write_text("replacement\n", encoding="utf-8")
                receipt_path.chmod(0o600)

            with (
                patch.object(pr_shepherd.os, "fsync", side_effect=replace_named_file),
                self.assertRaises(OSError),
            ):
                pr_shepherd.append_watch_receipt(
                    receipt_path,
                    _watch_record(
                        completed_at="2026-07-15T12:00:00Z",
                        status="success",
                        outcome="clean",
                    ),
                )

    def test_watch_receipt_detects_parent_replacement_after_fsync(self) -> None:
        real_fsync = os.fsync
        with tempfile.TemporaryDirectory() as tmp:
            receipt_dir = Path(tmp) / "receipts"
            receipt_path = receipt_dir / "watch.jsonl"

            def replace_parent(fd: int) -> None:
                real_fsync(fd)
                receipt_dir.rename(Path(tmp) / "stale-receipts")
                receipt_dir.mkdir(mode=0o700)
                receipt_path.write_text("replacement\n", encoding="utf-8")
                receipt_path.chmod(0o600)

            with (
                patch.object(pr_shepherd.os, "fsync", side_effect=replace_parent),
                self.assertRaises(OSError),
            ):
                pr_shepherd.append_watch_receipt(
                    receipt_path,
                    _watch_record(
                        completed_at="2026-07-15T12:00:00Z",
                        status="success",
                        outcome="clean",
                    ),
                )

    def test_watch_receipt_rejects_broad_existing_directory_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_dir = Path(tmp) / "shared"
            receipt_dir.mkdir(mode=0o755)
            receipt_dir.chmod(0o755)

            with self.assertRaises(OSError):
                pr_shepherd.append_watch_receipt(
                    receipt_dir / "watch.jsonl",
                    _watch_record(
                        completed_at="2026-07-15T12:00:00Z",
                        status="success",
                        outcome="clean",
                    ),
                )

            self.assertEqual(receipt_dir.stat().st_mode & 0o777, 0o755)
            self.assertFalse((receipt_dir / "watch.jsonl").exists())

    def test_watch_receipt_concurrent_appends_remain_valid_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipts" / "watch.jsonl"

            def append(index: int) -> None:
                pr_shepherd.append_watch_receipt(
                    receipt_path,
                    _watch_record(
                        completed_at="2026-07-15T12:00:00+00:00",
                        status="success",
                        outcome="clean",
                        run_index=index + 1,
                    ),
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(append, range(40)))
            raw = receipt_path.read_text(encoding="utf-8")
            records = [json.loads(line) for line in raw.splitlines()]

        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(len(records), 40)
        self.assertEqual(
            {record["run_id"] for record in records},
            {f"{index:064x}" for index in range(1, 41)},
        )

    def test_watch_receipt_older_success_cannot_mask_newer_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipts" / "watch.jsonl"
            newer_failure = _watch_record(
                completed_at="2026-07-15T12:01:00+00:00",
                status="failure",
                outcome="broken",
            )
            older_success = _watch_record(
                completed_at="2026-07-15T12:00:00+00:00",
                status="success",
                outcome="clean",
            )

            pr_shepherd.append_watch_receipt(receipt_path, newer_failure)
            pr_shepherd.append_watch_receipt(receipt_path, older_success)
            records = [
                json.loads(line)
                for line in receipt_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(records, [newer_failure])

    def test_watch_receipt_equal_time_failure_dominates_and_duplicate_is_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipts" / "watch.jsonl"
            success = _watch_record(
                completed_at="2026-07-15T12:00:00Z",
                status="success",
                outcome="clean",
            )
            failure = _watch_record(
                completed_at="2026-07-15T12:00:00+00:00",
                status="failure",
                outcome="broken",
            )
            recovery = _watch_record(
                completed_at="2026-07-15T12:01:00+00:00",
                status="success",
                outcome="clean",
            )

            pr_shepherd.append_watch_receipt(receipt_path, success)
            pr_shepherd.append_watch_receipt(receipt_path, failure)
            pr_shepherd.append_watch_receipt(receipt_path, failure)
            pr_shepherd.append_watch_receipt(receipt_path, success)
            pr_shepherd.append_watch_receipt(receipt_path, recovery)
            records = [
                json.loads(line)
                for line in receipt_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(records, [success, failure, recovery])
        self.assertEqual(records[-1]["status"], "success")

    def test_watch_receipt_recovers_partial_tail_without_promoting_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_dir = Path(tmp) / "receipts"
            receipt_dir.mkdir(mode=0o700)
            receipt_path = receipt_dir / "watch.jsonl"
            receipt_path.write_bytes(
                b'{"schema":"pr-shepherd.watch-receipt/v1","status":"success"'
            )
            receipt_path.chmod(0o600)
            failure = _watch_record(
                completed_at="2026-07-15T12:01:00+00:00",
                status="failure",
                outcome="broken",
            )

            pr_shepherd.append_watch_receipt(receipt_path, failure)
            lines = receipt_path.read_bytes().splitlines()

        self.assertIn(b"\x00", lines[0])
        with self.assertRaises((UnicodeDecodeError, json.JSONDecodeError)):
            json.loads(lines[0].decode("utf-8"))
        self.assertEqual(json.loads(lines[-1]), failure)
        self.assertEqual(json.loads(lines[-1])["status"], "failure")

    def test_watch_receipt_complete_failure_without_newline_dominates_delayed_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_dir = Path(tmp) / "receipts"
            receipt_dir.mkdir(mode=0o700)
            receipt_path = receipt_dir / "watch.jsonl"
            earlier_success = _watch_record(
                completed_at="2026-07-15T11:59:00+00:00",
                status="success",
                outcome="clean",
            )
            interrupted_failure = _watch_record(
                completed_at="2026-07-15T12:01:00+00:00",
                status="failure",
                outcome="broken",
            )
            delayed_success = _watch_record(
                completed_at="2026-07-15T12:00:00+00:00",
                status="success",
                outcome="clean",
            )
            receipt_path.write_bytes(
                pr_shepherd._receipt_payload(earlier_success)
                + pr_shepherd._receipt_payload(interrupted_failure)[:-1]
            )
            receipt_path.chmod(0o600)

            accepted = pr_shepherd.append_watch_receipt(receipt_path, delayed_success)
            lines = receipt_path.read_bytes().splitlines()

        self.assertFalse(accepted)
        self.assertIn(b"\x00", lines[-2])
        self.assertEqual(json.loads(lines[-1]), interrupted_failure)

    def test_watch_receipt_unparseable_tail_commits_failure_until_later_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_dir = Path(tmp) / "receipts"
            receipt_dir.mkdir(mode=0o700)
            receipt_path = receipt_dir / "watch.jsonl"
            earlier_success = _watch_record(
                completed_at="2026-07-15T11:59:00+00:00",
                status="success",
                outcome="clean",
            )
            unproven_success = _watch_record(
                completed_at="2026-07-15T12:00:00+00:00",
                status="success",
                outcome="clean",
            )
            later_recovery = _watch_record(
                completed_at="2026-07-15T12:01:00+00:00",
                status="success",
                outcome="clean",
            )
            receipt_path.write_bytes(
                pr_shepherd._receipt_payload(earlier_success) + b'{"status":"failure"'
            )
            receipt_path.chmod(0o600)

            accepted = pr_shepherd.append_watch_receipt(receipt_path, unproven_success)
            marker = json.loads(receipt_path.read_bytes().splitlines()[-1])
            recovered = pr_shepherd.append_watch_receipt(receipt_path, later_recovery)
            final = json.loads(receipt_path.read_bytes().splitlines()[-1])

        self.assertFalse(accepted)
        self.assertEqual(marker["status"], "failure")
        self.assertIn("receipt_tail_recovery_unproven", marker["failure_codes"])
        self.assertTrue(recovered)
        self.assertEqual(final, later_recovery)

    def test_watch_receipt_complete_malformed_tail_cannot_mask_newer_failure(self) -> None:
        malformed_tails = (b"{bad}\n", b'{"status":"success"}\n')
        for malformed_tail in malformed_tails:
            with self.subTest(tail=malformed_tail), tempfile.TemporaryDirectory() as tmp:
                receipt_dir = Path(tmp) / "receipts"
                receipt_dir.mkdir(mode=0o700)
                receipt_path = receipt_dir / "watch.jsonl"
                newer_failure = _watch_record(
                    completed_at="2026-07-15T12:05:00+00:00",
                    status="failure",
                    outcome="broken",
                )
                delayed_success = _watch_record(
                    completed_at="2026-07-15T12:04:00+00:00",
                    status="success",
                    outcome="clean",
                )
                receipt_path.write_bytes(
                    pr_shepherd._receipt_payload(newer_failure) + malformed_tail
                )
                receipt_path.chmod(0o600)

                accepted = pr_shepherd.append_watch_receipt(receipt_path, delayed_success)
                lines = receipt_path.read_bytes().splitlines()
                marker = json.loads(lines[-1])

            self.assertFalse(accepted)
            self.assertEqual(marker["status"], "failure")
            self.assertEqual(marker["completed_at"], newer_failure["completed_at"])
            self.assertIn("receipt_tail_recovery_unproven", marker["failure_codes"])

    def test_watch_receipt_semantically_invalid_success_cannot_anchor_chronology(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_dir = Path(tmp) / "receipts"
            receipt_dir.mkdir(mode=0o700)
            receipt_path = receipt_dir / "watch.jsonl"
            newer_failure = _watch_record(
                completed_at="2026-07-15T12:05:00+00:00",
                status="failure",
                outcome="broken",
            )
            invalid_success = _watch_record(
                completed_at="2026-07-15T12:06:00+00:00",
                status="success",
                outcome="clean",
            )
            invalid_success["stalls_count"] = 1
            delayed_success = _watch_record(
                completed_at="2026-07-15T12:04:00+00:00",
                status="success",
                outcome="clean",
            )
            receipt_path.write_bytes(
                pr_shepherd._receipt_payload(newer_failure)
                + pr_shepherd._receipt_payload(invalid_success)
            )
            receipt_path.chmod(0o600)

            accepted = pr_shepherd.append_watch_receipt(receipt_path, delayed_success)
            marker = json.loads(receipt_path.read_bytes().splitlines()[-1])

        self.assertFalse(accepted)
        self.assertEqual(marker["status"], "failure")
        self.assertEqual(marker["completed_at"], newer_failure["completed_at"])
        self.assertIn("receipt_tail_recovery_unproven", marker["failure_codes"])

    def test_watch_receipt_unanchored_malformed_tail_blocks_premature_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt_dir = Path(tmp) / "receipts"
            receipt_dir.mkdir(mode=0o700)
            receipt_path = receipt_dir / "watch.jsonl"
            receipt_path.write_bytes(b"{bad}\n")
            receipt_path.chmod(0o600)
            delayed_success = _watch_record(
                completed_at="2026-07-15T12:00:00+00:00",
                status="success",
                outcome="clean",
            )
            premature_recovery = _watch_record(
                completed_at="2026-07-15T12:01:00+00:00",
                status="success",
                outcome="clean",
            )

            accepted = pr_shepherd.append_watch_receipt(receipt_path, delayed_success)
            marker = json.loads(receipt_path.read_bytes().splitlines()[-1])
            premature = pr_shepherd.append_watch_receipt(
                receipt_path,
                premature_recovery,
            )
            marker_at = datetime.fromisoformat(marker["completed_at"])
            proven_recovery = _watch_record(
                completed_at=(marker_at + timedelta(seconds=1)).isoformat(),
                status="success",
                outcome="clean",
            )
            recovered = pr_shepherd.append_watch_receipt(receipt_path, proven_recovery)
            final = json.loads(receipt_path.read_bytes().splitlines()[-1])

        self.assertFalse(accepted)
        self.assertFalse(premature)
        self.assertEqual(marker["status"], "failure")
        self.assertGreater(marker["completed_at"], delayed_success["completed_at"])
        self.assertTrue(recovered)
        self.assertEqual(final, proven_recovery)

    def test_watch_alert_body_is_summary_not_raw_json(self) -> None:
        body = pr_shepherd._watch_alert_body(
            stalls=[
                {
                    "repo": "example-org/SampleApp",
                    "number": 338,
                    "title": "CI is still pending",
                    "url": "https://github.com/example-org/SampleApp/pull/338",
                    "check_status": "pending",
                }
            ],
            orphans=[
                {
                    "repo": "example-org/SampleTool",
                    "number": 192,
                    "priority": "P2",
                    "path": "html/2026-07-10.html",
                    "line": 42,
                    "title": "Fix unsupported citation",
                    "url": "https://github.com/example-org/SampleTool/pull/192",
                }
            ],
        )

        self.assertIn("Stalled auto-merge PRs:", body)
        self.assertIn("example-org/SampleApp#338", body)
        self.assertIn("checks=pending", body)
        self.assertIn("Orphaned unresolved Codex P1 findings", body)
        self.assertIn("html/2026-07-10.html:42", body)
        self.assertIn("Full JSON stays", body)
        self.assertNotIn('"orphans"', body)
        self.assertNotIn('"stalls"', body)

    def test_watch_alert_body_strips_codex_badge_markup(self) -> None:
        body = pr_shepherd._watch_alert_body(
            stalls=[],
            orphans=[
                {
                    "repo": "example-owner/SampleApp",
                    "number": 224,
                    "priority": "P2",
                    "title": "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Name the stale nutrition gap**",
                }
            ],
        )

        self.assertIn("P2", body)
        self.assertIn("Name the stale nutrition gap", body)
        self.assertNotIn("<sub>", body)
        self.assertNotIn("Badge](", body)
        self.assertNotIn("**", body)

    def test_deliver_alert_rejects_oversize_fields_before_process_start(self) -> None:
        with (
            patch.object(pr_shepherd.subprocess, "run") as run,
            redirect_stderr(io.StringIO()),
        ):
            subject_ok = pr_shepherd.deliver_alert(
                alert_command=Path("/bin/sh"),
                subject="x" * (pr_shepherd.MAX_ALERT_SUBJECT_BYTES + 1),
                body="body",
            )
            body_ok = pr_shepherd.deliver_alert(
                alert_command=Path("/bin/sh"),
                subject="subject",
                body="x" * (pr_shepherd.MAX_ALERT_BODY_BYTES + 1),
            )

        self.assertFalse(subject_ok)
        self.assertFalse(body_ok)
        run.assert_not_called()

    def test_alert_process_timeouts_fail_closed(self) -> None:
        with patch.object(
            pr_shepherd,
            "_run_alert_process",
            side_effect=subprocess.TimeoutExpired(["fleet-send"], 1),
        ):
            with redirect_stderr(io.StringIO()):
                delivered = pr_shepherd.deliver_alert(
                    alert_command=Path("/bin/sh"),
                    subject="subject",
                    body="body",
                )
                configured = pr_shepherd.validate_alert_configuration(
                    alert_command=Path("/bin/sh"),
                    destination_alias="fleet-ops",
                )

        self.assertFalse(delivered)
        self.assertFalse(configured)

    def test_alert_timeouts_kill_term_ignoring_descendant_groups(self) -> None:
        for operation in ("delivery", "config"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                pid_file = root / "descendant.pid"
                alert = root / "alert"
                alert.write_text(
                    "#!/bin/bash\n"
                    "set -eu\n"
                    "trap '' TERM\n"
                    "/bin/bash -c 'trap \"\" TERM; "
                    "printf \"%s\\n\" \"$$\" > \"$ALERT_DESCENDANT_PID\"; "
                    "while :; do /bin/sleep 1; done' &\n"
                    "while :; do /bin/sleep 1; done\n",
                    encoding="utf-8",
                )
                alert.chmod(0o700)
                real_popen = subprocess.Popen

                def start_ready_alert(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if pid_file.is_file() and pid_file.stat().st_size > 0:
                            return process
                        if process.poll() is not None:
                            break
                        time.sleep(0.01)
                    # Startup failure must not leak the real fixture process group.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate(timeout=5)
                    self.fail("alert descendant did not install its TERM handler")

                with (
                    # Keep the real Popen/process and cleanup path, but do not start
                    # the 0.5-second communicate timer before the fixture is ready.
                    patch.object(pr_shepherd.subprocess, "Popen", start_ready_alert),
                    patch.dict(os.environ, {"ALERT_DESCENDANT_PID": str(pid_file)}),
                    patch.object(pr_shepherd, "ALERT_DELIVERY_TIMEOUT_SECONDS", 0.5),
                    patch.object(pr_shepherd, "ALERT_CONFIG_TIMEOUT_SECONDS", 0.5),
                    patch.object(
                        pr_shepherd,
                        "ALERT_TERM_GRACE_SECONDS",
                        0.05,
                        create=True,
                    ),
                    redirect_stderr(io.StringIO()),
                ):
                    if operation == "delivery":
                        outcome = pr_shepherd.deliver_alert(
                            alert_command=alert,
                            subject="subject",
                            body="body",
                        )
                    else:
                        outcome = pr_shepherd.validate_alert_configuration(
                            alert_command=alert,
                            destination_alias="fleet-ops",
                        )
                descendant = int(pid_file.read_text(encoding="ascii"))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(descendant, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                else:
                    os.kill(descendant, 9)
                    self.fail(f"{operation} timeout left descendant {descendant} alive")

            self.assertFalse(outcome)

    def test_alert_parent_signals_cleanup_both_config_and_delivery_groups(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        for operation in ("delivery", "config"):
            for parent_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with (
                    self.subTest(operation=operation, signal=parent_signal),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    descendant_pid = root / "descendant.pid"
                    alert = root / "alert"
                    alert.write_text(
                        "#!/bin/bash\n"
                        "set -eu\n"
                        "trap '' HUP INT TERM\n"
                        "/bin/bash -c 'trap \"\" HUP INT TERM; "
                        "while :; do /bin/sleep 1; done' &\n"
                        "printf '%s %s\\n' \"$$\" \"$!\" > "
                        "\"$ALERT_DESCENDANT_PID\"\n"
                        "while :; do /bin/sleep 1; done\n",
                        encoding="utf-8",
                    )
                    alert.chmod(0o700)
                    code = (
                        "from pathlib import Path\n"
                        "from git_janitor import pr_shepherd as p\n"
                        "import os, signal\n"
                        # Durable supervisors intentionally ignore SIGHUP. This fixture
                        # tests cancellation with the normal signal disposition.
                        "signal.signal(signal.SIGHUP, signal.SIG_DFL)\n"
                        "p.ALERT_CONFIG_TIMEOUT_SECONDS = 30\n"
                        "p.ALERT_DELIVERY_TIMEOUT_SECONDS = 30\n"
                        "p.ALERT_TERM_GRACE_SECONDS = 0.05\n"
                        f"alert = Path({str(alert)!r})\n"
                        f"operation = {operation!r}\n"
                        "result = (p.deliver_alert(alert_command=alert, "
                        "subject='subject', body='body') if operation == 'delivery' "
                        "else p.validate_alert_configuration(alert_command=alert, "
                        "destination_alias='fleet-ops'))\n"
                        "raise SystemExit(0 if result else 1)\n"
                    )
                    child = subprocess.Popen(
                        [sys.executable, "-c", code],
                        env={
                            **os.environ,
                            "PYTHONPATH": str(source_root),
                            "ALERT_DESCENDANT_PID": str(descendant_pid),
                        },
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not descendant_pid.exists():
                        if child.poll() is not None:
                            break
                        time.sleep(0.01)
                    if not descendant_pid.exists():
                        output = child.communicate(timeout=1)
                        self.fail(f"alert helper did not start its descendant: {output!r}")
                    alert_group, descendant = map(
                        int,
                        descendant_pid.read_text(encoding="ascii").split(),
                    )
                    os.kill(child.pid, parent_signal)
                    try:
                        _stdout, stderr = child.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.communicate()
                        self.fail(f"{operation} did not preserve signal cancellation")
                    descendant_gone = False
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        try:
                            os.kill(descendant, 0)
                        except ProcessLookupError:
                            descendant_gone = True
                            break
                        time.sleep(0.01)
                    if not descendant_gone:
                        try:
                            os.killpg(alert_group, signal.SIGKILL)
                        except ProcessLookupError:
                            descendant_gone = True
                    self.assertTrue(
                        descendant_gone,
                        f"{operation} signal {parent_signal} leaked {descendant}",
                    )
                    self.assertNotEqual(child.returncode, 0, stderr)
                    if parent_signal in {signal.SIGHUP, signal.SIGTERM}:
                        self.assertEqual(child.returncode, -parent_signal, stderr)

    def test_alert_communicate_error_uses_group_cleanup_and_restores_handlers(self) -> None:
        class BrokenProcess:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                if self.communications == 1:
                    raise OSError("synthetic communicate failure")
                self.returncode = -signal.SIGKILL
                return "", ""

        for operation in ("delivery", "config"):
            with self.subTest(operation=operation):
                broken = BrokenProcess()
                before = {
                    signum: signal.getsignal(signum)
                    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
                }

                def kill_group(_pgid: int, signum: int) -> None:
                    if signum == 0:
                        raise ProcessLookupError

                with (
                    patch.object(pr_shepherd.subprocess, "Popen", return_value=broken),
                    patch.object(
                        pr_shepherd.os,
                        "killpg",
                        side_effect=kill_group,
                    ) as killpg,
                    patch.object(pr_shepherd.time, "sleep"),
                    redirect_stderr(io.StringIO()),
                ):
                    if operation == "delivery":
                        outcome = pr_shepherd.deliver_alert(
                            alert_command=Path("/bin/sh"),
                            subject="subject",
                            body="body",
                        )
                    else:
                        outcome = pr_shepherd.validate_alert_configuration(
                            alert_command=Path("/bin/sh"),
                            destination_alias="fleet-ops",
                        )

                self.assertFalse(outcome)
                self.assertEqual(broken.communications, 2)
                self.assertEqual(
                    [call.args[1] for call in killpg.call_args_list],
                    [signal.SIGTERM, signal.SIGKILL, 0],
                )
                self.assertEqual(
                    {
                        signum: signal.getsignal(signum)
                        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
                    },
                    before,
                )

    def test_alert_public_surfaces_stop_when_group_survives_direct_reap(self) -> None:
        class PersistentGroupProcess:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                if self.communications == 1:
                    raise subprocess.TimeoutExpired(["alert"], 1)
                self.returncode = -signal.SIGKILL
                return "", ""

        for operation in ("delivery", "config"):
            with self.subTest(operation=operation):
                process = PersistentGroupProcess()
                with (
                    patch.object(pr_shepherd.subprocess, "Popen", return_value=process),
                    patch.object(pr_shepherd.os, "killpg"),
                    patch.object(pr_shepherd.time, "sleep"),
                    patch.object(
                        pr_shepherd,
                        "ALERT_GROUP_VERIFY_SECONDS",
                        0.01,
                        create=True,
                    ),
                    redirect_stderr(io.StringIO()),
                ):
                    if operation == "delivery":
                        outcome = pr_shepherd.deliver_alert(
                            alert_command=Path("/bin/sh"),
                            subject="subject",
                            body="body",
                        )
                    else:
                        outcome = pr_shepherd.validate_alert_configuration(
                            alert_command=Path("/bin/sh"),
                            destination_alias="fleet-ops",
                        )

                self.assertFalse(outcome)
                self.assertEqual(process.communications, 2)

    def test_alert_cleanup_failure_with_persistent_restore_failure_stays_terminal(
        self,
    ) -> None:
        original_restore = pr_shepherd._restore_alert_signal_handlers
        watched_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        for operation in ("delivery", "config"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / "receipts" / "watch.jsonl"
                stalls = (
                    [{"repo": "example-org/SampleApp", "number": 1, "title": "blocked"}]
                    if operation == "delivery"
                    else []
                )
                process = _PersistentAlertGroupProcess()
                restore_calls = [0]
                prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
                prior_handlers = {
                    signum: signal.getsignal(signum) for signum in watched_signals
                }

                def always_fail_restore(_handlers: dict[int, object]) -> None:
                    restore_calls[0] += 1
                    raise OSError(f"restore failure {restore_calls[0]}")

                try:
                    with (
                        patch.object(
                            pr_shepherd,
                            "load_cache",
                            return_value={"repos": {"SampleApp": {}}},
                        ) as load_cache,
                        patch.object(
                            pr_shepherd,
                            "stalled_auto_merges",
                            return_value=stalls,
                        ) as stalled,
                        patch.object(
                            pr_shepherd,
                            "orphaned_findings",
                            return_value=[],
                        ) as orphans,
                        patch.object(
                            pr_shepherd.subprocess,
                            "Popen",
                            return_value=process,
                        ) as popen,
                        patch.object(pr_shepherd.os, "killpg"),
                        patch.object(pr_shepherd.time, "sleep"),
                        patch.object(pr_shepherd, "ALERT_GROUP_VERIFY_SECONDS", 0),
                        patch.object(
                            pr_shepherd,
                            "_restore_alert_signal_handlers",
                            side_effect=always_fail_restore,
                        ),
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        result = pr_shepherd.daily_watch(
                            owner="example-org",
                            facts_path=Path("/tmp/repo-facts.json"),
                            min_age=timedelta(hours=2),
                            days=7,
                            alert_command=Path("/bin/sh"),
                            alert_destination_alias="fleet-ops",
                            require_alert_config=operation == "config",
                            receipt_path=receipt,
                            run_id="a" * 64,
                            runtime_sha="b" * 40,
                            runtime_manifest_sha256="c" * 64,
                            destination_target_sha256="d" * 64,
                            gh_executable_sha256="e" * 64,
                            hermes_runtime_sha="f" * 40,
                            hermes_executable_sha256="1" * 64,
                            hermes_runtime_manifest_sha256="2" * 64,
                        )
                finally:
                    signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
                    try:
                        original_restore(dict(prior_handlers))
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
                records = [
                    json.loads(line)
                    for line in receipt.read_text(encoding="utf-8").splitlines()
                ]

                self.assertEqual(result, 3)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(process.communications, 2)
                self.assertEqual(restore_calls[0], 3)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "failure")
                self.assertEqual(records[0]["outcome"], "broken")
                self.assertEqual(records[0]["exit_code"], 3)
                self.assertEqual(records[0]["alert_delivery"], "failed")
                self.assertEqual(
                    records[0]["failure_codes"],
                    ["alert_cleanup_unproven"],
                )
                if operation == "config":
                    load_cache.assert_not_called()
                    stalled.assert_not_called()
                    orphans.assert_not_called()
                else:
                    load_cache.assert_called_once()
                    stalled.assert_called_once()
                    orphans.assert_not_called()

    def test_successful_alert_with_persistent_restore_failure_stays_terminal(
        self,
    ) -> None:
        original_restore = pr_shepherd._restore_alert_signal_handlers
        watched_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        process = _SuccessfulAlertProcess()
        restore_calls = [0]
        received: list[int] = []
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        original_handlers = {
            signum: signal.getsignal(signum) for signum in watched_signals
        }

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)
            raise InterruptedError("caller cancellation handler")

        def always_fail_restore(_handlers: dict[int, object]) -> None:
            restore_calls[0] += 1
            raise OSError(f"restore failure {restore_calls[0]}")

        def killpg(_pid: int, probe: int) -> None:
            if probe == 0:
                raise ProcessLookupError

        signal.signal(signal.SIGTERM, caller_handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / "receipts" / "watch.jsonl"
                with (
                    patch.object(
                        pr_shepherd,
                        "load_cache",
                        return_value={"repos": {"SampleApp": {}}},
                    ) as load_cache,
                    patch.object(pr_shepherd, "stalled_auto_merges") as stalled,
                    patch.object(pr_shepherd, "orphaned_findings") as orphans,
                    patch.object(
                        pr_shepherd.subprocess,
                        "Popen",
                        return_value=process,
                    ),
                    patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                    patch.object(
                        pr_shepherd,
                        "_restore_alert_signal_handlers",
                        side_effect=always_fail_restore,
                    ),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    result = pr_shepherd.daily_watch(
                        owner="example-org",
                        facts_path=Path("/tmp/repo-facts.json"),
                        min_age=timedelta(hours=2),
                        days=7,
                        alert_command=Path("/bin/sh"),
                        alert_destination_alias="fleet-ops",
                        require_alert_config=True,
                        receipt_path=receipt,
                        run_id="a" * 64,
                        runtime_sha="b" * 40,
                        runtime_manifest_sha256="c" * 64,
                        destination_target_sha256="d" * 64,
                        gh_executable_sha256="e" * 64,
                        hermes_runtime_sha="f" * 40,
                        hermes_executable_sha256="1" * 64,
                        hermes_runtime_manifest_sha256="2" * 64,
                    )
                records = [
                    json.loads(line)
                    for line in receipt.read_text(encoding="utf-8").splitlines()
                ]
                with self.assertRaises(InterruptedError):
                    signal.raise_signal(signal.SIGTERM)
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
            try:
                original_restore(dict(original_handlers))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)

        self.assertEqual(result, 3)
        self.assertEqual(process.communications, 1)
        self.assertEqual(restore_calls[0], 3)
        self.assertEqual(received, [signal.SIGTERM])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["failure_codes"], ["alert_cleanup_unproven"])
        load_cache.assert_not_called()
        stalled.assert_not_called()
        orphans.assert_not_called()

    def test_signal_during_terminal_callback_preempts_restore_failure(self) -> None:
        original_restore = pr_shepherd._restore_alert_signal_handlers
        watched_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        process = _SuccessfulAlertProcess()
        callbacks: list[str] = []
        received: list[int] = []
        restore_calls = [0]
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        original_handlers = {
            signum: signal.getsignal(signum) for signum in watched_signals
        }

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)

        def callback(failure_code: str) -> None:
            callbacks.append(failure_code)
            signal.raise_signal(signal.SIGTERM)

        def always_fail_restore(_handlers: dict[int, object]) -> None:
            restore_calls[0] += 1
            raise OSError(f"restore failure {restore_calls[0]}")

        def killpg(_pid: int, probe: int) -> None:
            if probe == 0:
                raise ProcessLookupError

        signal.signal(signal.SIGTERM, caller_handler)
        try:
            with (
                patch.object(
                    pr_shepherd.subprocess,
                    "Popen",
                    return_value=process,
                ),
                patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                patch.object(
                    pr_shepherd,
                    "_restore_alert_signal_handlers",
                    side_effect=always_fail_restore,
                ),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(InterruptedError):
                    pr_shepherd._run_alert_process(
                        ["/bin/sh", "--check-config"],
                        input_text=None,
                        timeout_seconds=1,
                        cancelled_cleanup_callback=callback,
                    )
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
            try:
                original_restore(dict(original_handlers))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)

        self.assertEqual(callbacks, ["alert_cleanup_unproven"])
        self.assertEqual(received, [signal.SIGTERM])
        self.assertEqual(restore_calls[0], 3)
        self.assertEqual(process.communications, 1)

    def test_signal_during_cleanup_diagnostic_preempts_restore_failure(self) -> None:
        class SignalStderr(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.emitted = False

            def write(self, value: str) -> int:
                if not self.emitted:
                    self.emitted = True
                    signal.raise_signal(signal.SIGTERM)
                return super().write(value)

        original_restore = pr_shepherd._restore_alert_signal_handlers
        watched_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        process = _SuccessfulAlertProcess()
        callbacks: list[str] = []
        received: list[int] = []
        restore_calls = [0]
        stderr = SignalStderr()
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        original_handlers = {
            signum: signal.getsignal(signum) for signum in watched_signals
        }

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)

        def always_fail_restore(_handlers: dict[int, object]) -> None:
            restore_calls[0] += 1
            raise OSError(f"restore failure {restore_calls[0]}")

        def killpg(_pid: int, probe: int) -> None:
            if probe == 0:
                raise ProcessLookupError

        signal.signal(signal.SIGTERM, caller_handler)
        try:
            with (
                patch.object(
                    pr_shepherd.subprocess,
                    "Popen",
                    return_value=process,
                ),
                patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                patch.object(
                    pr_shepherd,
                    "_restore_alert_signal_handlers",
                    side_effect=always_fail_restore,
                ),
                redirect_stderr(stderr),
            ):
                with self.assertRaises(InterruptedError):
                    pr_shepherd._run_alert_process(
                        ["/bin/sh", "--check-config"],
                        input_text=None,
                        timeout_seconds=1,
                        cancelled_cleanup_callback=callbacks.append,
                    )
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
            try:
                original_restore(dict(original_handlers))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)

        self.assertTrue(stderr.emitted)
        self.assertEqual(callbacks, ["alert_cleanup_unproven"])
        self.assertEqual(received, [signal.SIGTERM])
        self.assertEqual(restore_calls[0], 3)
        self.assertEqual(process.communications, 1)

    def test_cross_signal_during_unproven_restore_delegates_both(self) -> None:
        class SignalStderr(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.emitted = False

            def write(self, value: str) -> int:
                if not self.emitted:
                    self.emitted = True
                    signal.raise_signal(signal.SIGTERM)
                return super().write(value)

        original_signal = signal.signal
        original_restore = pr_shepherd._restore_alert_signal_handlers
        watched_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        process = _SuccessfulAlertProcess()
        callbacks: list[str] = []
        received: list[int] = []
        signal_calls = [0]
        restore_calls = [0]
        stderr = SignalStderr()
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        original_handlers = {
            signum: signal.getsignal(signum) for signum in watched_signals
        }

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)

        def callback(failure_code: str) -> None:
            callbacks.append(failure_code)
            signal.raise_signal(signal.SIGINT)

        def fail_first_handoff_install(signum: int, handler: object) -> object:
            signal_calls[0] += 1
            if signal_calls[0] == 4:
                raise OSError("synthetic handoff install failure")
            return original_signal(signum, handler)

        def always_fail_restore(_handlers: dict[int, object]) -> None:
            restore_calls[0] += 1
            raise OSError(f"restore failure {restore_calls[0]}")

        def killpg(_pid: int, probe: int) -> None:
            if probe == 0:
                raise ProcessLookupError

        for signum in (signal.SIGINT, signal.SIGTERM):
            original_signal(signum, caller_handler)
        try:
            with (
                patch.object(
                    pr_shepherd.signal,
                    "signal",
                    side_effect=fail_first_handoff_install,
                ),
                patch.object(
                    pr_shepherd,
                    "_restore_alert_signal_handlers",
                    side_effect=always_fail_restore,
                ),
                patch.object(
                    pr_shepherd.subprocess,
                    "Popen",
                    return_value=process,
                ),
                patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                redirect_stderr(stderr),
            ):
                with self.assertRaises(InterruptedError):
                    pr_shepherd._run_alert_process(
                        ["/bin/sh", "--check-config"],
                        input_text=None,
                        timeout_seconds=1,
                        cancelled_cleanup_callback=callback,
                    )
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
            try:
                original_restore(dict(original_handlers))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)

        self.assertTrue(stderr.emitted)
        self.assertEqual(callbacks, ["alert_cleanup_unproven"])
        self.assertEqual(received, [signal.SIGINT, signal.SIGTERM])
        self.assertEqual(restore_calls[0], 4)
        self.assertEqual(process.communications, 1)

    def test_alert_cleanup_failure_with_broken_stderr_commits_terminal_receipt(
        self,
    ) -> None:
        class BrokenStderr(io.StringIO):
            def write(self, _value: str) -> int:
                raise OSError("synthetic closed stderr")

        for operation in ("delivery", "config"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / "receipts" / "watch.jsonl"
                stalls = (
                    [{"repo": "example-org/SampleApp", "number": 1, "title": "blocked"}]
                    if operation == "delivery"
                    else []
                )
                process = _PersistentAlertGroupProcess()
                with (
                    patch.object(
                        pr_shepherd,
                        "load_cache",
                        return_value={"repos": {"SampleApp": {}}},
                    ) as load_cache,
                    patch.object(
                        pr_shepherd,
                        "stalled_auto_merges",
                        return_value=stalls,
                    ) as stalled,
                    patch.object(
                        pr_shepherd,
                        "orphaned_findings",
                        return_value=[],
                    ) as orphans,
                    patch.object(
                        pr_shepherd.subprocess,
                        "Popen",
                        return_value=process,
                    ) as popen,
                    patch.object(pr_shepherd.os, "killpg"),
                    patch.object(pr_shepherd.time, "sleep"),
                    patch.object(pr_shepherd, "ALERT_GROUP_VERIFY_SECONDS", 0),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(BrokenStderr()),
                ):
                    result = pr_shepherd.daily_watch(
                        owner="example-org",
                        facts_path=Path("/tmp/repo-facts.json"),
                        min_age=timedelta(hours=2),
                        days=7,
                        alert_command=Path("/bin/sh"),
                        alert_destination_alias="fleet-ops",
                        require_alert_config=operation == "config",
                        receipt_path=receipt,
                        run_id="a" * 64,
                        runtime_sha="b" * 40,
                        runtime_manifest_sha256="c" * 64,
                        destination_target_sha256="d" * 64,
                        gh_executable_sha256="e" * 64,
                        hermes_runtime_sha="f" * 40,
                        hermes_executable_sha256="1" * 64,
                        hermes_runtime_manifest_sha256="2" * 64,
                    )

                records = [
                    json.loads(line)
                    for line in receipt.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(result, 3)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(process.communications, 2)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "failure")
                self.assertEqual(records[0]["failure_codes"], ["alert_cleanup_unproven"])
                if operation == "config":
                    load_cache.assert_not_called()
                    stalled.assert_not_called()
                    orphans.assert_not_called()
                else:
                    load_cache.assert_called_once()
                    stalled.assert_called_once()
                    orphans.assert_not_called()

    def test_public_cleanup_failure_with_broken_stderr_returns_false(self) -> None:
        class BrokenStderr(io.StringIO):
            def write(self, _value: str) -> int:
                raise OSError("synthetic closed stderr")

        for operation in ("delivery", "config"):
            with self.subTest(operation=operation):
                process = _PersistentAlertGroupProcess()
                with (
                    patch.object(
                        pr_shepherd.subprocess,
                        "Popen",
                        return_value=process,
                    ) as popen,
                    patch.object(pr_shepherd.os, "killpg"),
                    patch.object(pr_shepherd.time, "sleep"),
                    patch.object(pr_shepherd, "ALERT_GROUP_VERIFY_SECONDS", 0),
                    redirect_stderr(BrokenStderr()),
                ):
                    if operation == "delivery":
                        result = pr_shepherd.deliver_alert(
                            alert_command=Path("/bin/sh"),
                            subject="subject",
                            body="body",
                        )
                    else:
                        result = pr_shepherd.validate_alert_configuration(
                            alert_command=Path("/bin/sh"),
                            destination_alias="fleet-ops",
                        )

                self.assertIs(result, False)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(process.communications, 2)

    def test_cleanup_failure_does_not_swallow_fresh_baseexception(self) -> None:
        class InterruptingStderr(io.StringIO):
            def write(self, _value: str) -> int:
                raise KeyboardInterrupt("fresh caller cancellation")

        for operation in ("delivery", "config"):
            for boundary in ("diagnostic", "receipt"):
                with (
                    self.subTest(operation=operation, boundary=boundary),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    receipt = Path(tmp) / "receipts" / "watch.jsonl"
                    stalls = (
                        [{"repo": "example-org/SampleApp", "number": 1, "title": "blocked"}]
                        if operation == "delivery"
                        else []
                    )
                    stderr = (
                        InterruptingStderr()
                        if boundary == "diagnostic"
                        else io.StringIO()
                    )
                    process = _PersistentAlertGroupProcess()
                    with (
                        patch.object(
                            pr_shepherd,
                            "load_cache",
                            return_value={"repos": {"SampleApp": {}}},
                        ) as load_cache,
                        patch.object(
                            pr_shepherd,
                            "stalled_auto_merges",
                            return_value=stalls,
                        ) as stalled,
                        patch.object(
                            pr_shepherd,
                            "orphaned_findings",
                            return_value=[],
                        ) as orphans,
                        patch.object(
                            pr_shepherd.subprocess,
                            "Popen",
                            return_value=process,
                        ) as popen,
                        patch.object(pr_shepherd.os, "killpg"),
                        patch.object(pr_shepherd.time, "sleep"),
                        patch.object(pr_shepherd, "ALERT_GROUP_VERIFY_SECONDS", 0),
                        patch.object(
                            pr_shepherd,
                            "append_watch_receipt",
                            side_effect=(
                                KeyboardInterrupt("fresh caller cancellation")
                                if boundary == "receipt"
                                else None
                            ),
                        ) as append,
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(stderr),
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            pr_shepherd.daily_watch(
                                owner="example-org",
                                facts_path=Path("/tmp/repo-facts.json"),
                                min_age=timedelta(hours=2),
                                days=7,
                                alert_command=Path("/bin/sh"),
                                alert_destination_alias="fleet-ops",
                                require_alert_config=operation == "config",
                                receipt_path=receipt,
                                run_id="a" * 64,
                                runtime_sha="b" * 40,
                                runtime_manifest_sha256="c" * 64,
                                destination_target_sha256="d" * 64,
                                gh_executable_sha256="e" * 64,
                                hermes_runtime_sha="f" * 40,
                                hermes_executable_sha256="1" * 64,
                                hermes_runtime_manifest_sha256="2" * 64,
                            )

                    self.assertEqual(popen.call_count, 1)
                    self.assertEqual(process.communications, 2)
                    append.assert_called_once()
                    if operation == "config":
                        load_cache.assert_not_called()
                        stalled.assert_not_called()
                        orphans.assert_not_called()
                    else:
                        load_cache.assert_called_once()
                        stalled.assert_called_once()
                        orphans.assert_not_called()

    def test_public_cleanup_failure_does_not_swallow_fresh_baseexception(
        self,
    ) -> None:
        class InterruptingStderr(io.StringIO):
            def write(self, _value: str) -> int:
                raise KeyboardInterrupt("fresh caller cancellation")

        for operation in ("delivery", "config"):
            with self.subTest(operation=operation):
                process = _PersistentAlertGroupProcess()
                with (
                    patch.object(
                        pr_shepherd.subprocess,
                        "Popen",
                        return_value=process,
                    ) as popen,
                    patch.object(pr_shepherd.os, "killpg"),
                    patch.object(pr_shepherd.time, "sleep"),
                    patch.object(pr_shepherd, "ALERT_GROUP_VERIFY_SECONDS", 0),
                    redirect_stderr(InterruptingStderr()),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        if operation == "delivery":
                            pr_shepherd.deliver_alert(
                                alert_command=Path("/bin/sh"),
                                subject="subject",
                                body="body",
                            )
                        else:
                            pr_shepherd.validate_alert_configuration(
                                alert_command=Path("/bin/sh"),
                                destination_alias="fleet-ops",
                            )
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(process.communications, 2)

    def test_cleanup_failure_defers_real_signal_until_after_terminal_callback(
        self,
    ) -> None:
        original_append = pr_shepherd.append_watch_receipt
        signal_cases = (
            (signal.SIGINT, signal.default_int_handler, KeyboardInterrupt),
            (
                signal.SIGHUP,
                lambda _signum, _frame: (_ for _ in ()).throw(
                    OSError("custom HUP handler")
                ),
                OSError,
            ),
            (
                signal.SIGTERM,
                lambda _signum, _frame: (_ for _ in ()).throw(
                    InterruptedError("custom TERM handler")
                ),
                InterruptedError,
            ),
        )

        for operation in ("delivery", "config"):
            for boundary in ("diagnostic", "receipt"):
                for signum, handler, expected_exception in signal_cases:
                    with (
                        self.subTest(
                            operation=operation,
                            boundary=boundary,
                            signum=signum,
                        ),
                        tempfile.TemporaryDirectory() as tmp,
                    ):
                        receipt = Path(tmp) / "receipts" / "watch.jsonl"
                        stalls = (
                            [
                                {
                                    "repo": "example-org/SampleApp",
                                    "number": 1,
                                    "title": "blocked",
                                }
                            ]
                            if operation == "delivery"
                            else []
                        )
                        emitted = [False]
                        process = _PersistentAlertGroupProcess()

                        class SignallingStderr(io.StringIO):
                            def write(self, value: str) -> int:
                                if boundary == "diagnostic" and not emitted[0]:
                                    emitted[0] = True
                                    signal.raise_signal(signum)
                                return super().write(value)

                        def append_with_signal(
                            path: Path,
                            record: dict[str, object],
                        ) -> bool:
                            if boundary == "receipt" and not emitted[0]:
                                emitted[0] = True
                                signal.raise_signal(signum)
                            return original_append(path, record)

                        previous = signal.getsignal(signum)
                        signal.signal(signum, handler)
                        try:
                            with (
                                patch.object(
                                    pr_shepherd,
                                    "load_cache",
                                    return_value={"repos": {"SampleApp": {}}},
                                ) as load_cache,
                                patch.object(
                                    pr_shepherd,
                                    "stalled_auto_merges",
                                    return_value=stalls,
                                ) as stalled,
                                patch.object(
                                    pr_shepherd,
                                    "orphaned_findings",
                                    return_value=[],
                                ) as orphans,
                                patch.object(
                                    pr_shepherd.subprocess,
                                    "Popen",
                                    return_value=process,
                                ) as popen,
                                patch.object(pr_shepherd.os, "killpg"),
                                patch.object(pr_shepherd.time, "sleep"),
                                patch.object(
                                    pr_shepherd,
                                    "ALERT_GROUP_VERIFY_SECONDS",
                                    0,
                                ),
                                patch.object(
                                    pr_shepherd,
                                    "append_watch_receipt",
                                    side_effect=append_with_signal,
                                ) as append,
                                redirect_stdout(io.StringIO()),
                                redirect_stderr(SignallingStderr()),
                            ):
                                with self.assertRaises(expected_exception):
                                    pr_shepherd.daily_watch(
                                        owner="example-org",
                                        facts_path=Path("/tmp/repo-facts.json"),
                                        min_age=timedelta(hours=2),
                                        days=7,
                                        alert_command=Path("/bin/sh"),
                                        alert_destination_alias="fleet-ops",
                                        require_alert_config=operation == "config",
                                        receipt_path=receipt,
                                        run_id="a" * 64,
                                        runtime_sha="b" * 40,
                                        runtime_manifest_sha256="c" * 64,
                                        destination_target_sha256="d" * 64,
                                        gh_executable_sha256="e" * 64,
                                        hermes_runtime_sha="f" * 40,
                                        hermes_executable_sha256="1" * 64,
                                        hermes_runtime_manifest_sha256="2" * 64,
                                    )
                        finally:
                            signal.signal(signum, previous)

                        records = [
                            json.loads(line)
                            for line in receipt.read_text(encoding="utf-8").splitlines()
                        ]
                        self.assertTrue(emitted[0])
                        self.assertEqual(popen.call_count, 1)
                        self.assertEqual(process.communications, 2)
                        self.assertEqual(append.call_count, 1)
                        self.assertEqual(len(records), 1)
                        self.assertEqual(
                            records[0]["failure_codes"],
                            ["alert_cleanup_unproven"],
                        )
                        if operation == "config":
                            load_cache.assert_not_called()
                            stalled.assert_not_called()
                            orphans.assert_not_called()
                        else:
                            load_cache.assert_called_once()
                            stalled.assert_called_once()
                            orphans.assert_not_called()

    def test_existing_parent_signal_replays_when_callback_raises_baseexception(
        self,
    ) -> None:
        class CancelledReapedGroup:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                if self.communications == 1:
                    signal.raise_signal(signal.SIGTERM)
                    raise AssertionError("parent signal was not intercepted")
                self.returncode = -signal.SIGTERM
                return "", ""

        received: list[int] = []
        callback_codes: list[str] = []
        process = CancelledReapedGroup()
        previous = signal.getsignal(signal.SIGTERM)

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)
            raise InterruptedError("caller cancellation handler")

        def callback(failure_code: str) -> None:
            callback_codes.append(failure_code)
            raise SystemExit("synthetic callback cancellation")

        def killpg(_pid: int, signum: int) -> None:
            if signum == 0:
                raise ProcessLookupError

        signal.signal(signal.SIGTERM, caller_handler)
        try:
            with (
                patch.object(
                    pr_shepherd.subprocess,
                    "Popen",
                    return_value=process,
                ) as popen,
                patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(InterruptedError) as replayed:
                    pr_shepherd._run_alert_process(
                        ["alert"],
                        input_text=None,
                        timeout_seconds=1,
                        cancelled_cleanup_callback=callback,
                    )
        finally:
            signal.signal(signal.SIGTERM, previous)

        self.assertEqual(callback_codes, ["alert_cancelled"])
        self.assertEqual(received, [signal.SIGTERM])
        self.assertIsInstance(replayed.exception.__cause__, InterruptedError)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(process.communications, 2)

    def test_cleanup_failure_signal_before_owned_callback_still_receipts_first(
        self,
    ) -> None:
        original_terminal_work = pr_shepherd._attempt_alert_owned_terminal_work

        for operation in ("delivery", "config"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / "receipts" / "watch.jsonl"
                process = _PersistentAlertGroupProcess()
                stalls = (
                    [{"repo": "example-org/SampleApp", "number": 1, "title": "blocked"}]
                    if operation == "delivery"
                    else []
                )

                def signal_before_callback(**kwargs: object) -> BaseException | None:
                    signal.raise_signal(signal.SIGINT)
                    return original_terminal_work(**kwargs)

                previous = signal.getsignal(signal.SIGINT)
                signal.signal(signal.SIGINT, signal.default_int_handler)
                try:
                    with (
                        patch.object(
                            pr_shepherd,
                            "load_cache",
                            return_value={"repos": {"SampleApp": {}}},
                        ) as load_cache,
                        patch.object(
                            pr_shepherd,
                            "stalled_auto_merges",
                            return_value=stalls,
                        ) as stalled,
                        patch.object(
                            pr_shepherd,
                            "orphaned_findings",
                            return_value=[],
                        ) as orphans,
                        patch.object(
                            pr_shepherd.subprocess,
                            "Popen",
                            return_value=process,
                        ) as popen,
                        patch.object(pr_shepherd.os, "killpg"),
                        patch.object(pr_shepherd.time, "sleep"),
                        patch.object(pr_shepherd, "ALERT_GROUP_VERIFY_SECONDS", 0),
                        patch.object(
                            pr_shepherd,
                            "_attempt_alert_owned_terminal_work",
                            side_effect=signal_before_callback,
                        ) as terminal_work,
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            pr_shepherd.daily_watch(
                                owner="example-org",
                                facts_path=Path("/tmp/repo-facts.json"),
                                min_age=timedelta(hours=2),
                                days=7,
                                alert_command=Path("/bin/sh"),
                                alert_destination_alias="fleet-ops",
                                require_alert_config=operation == "config",
                                receipt_path=receipt,
                                run_id="a" * 64,
                                runtime_sha="b" * 40,
                                runtime_manifest_sha256="c" * 64,
                                destination_target_sha256="d" * 64,
                                gh_executable_sha256="e" * 64,
                                hermes_runtime_sha="f" * 40,
                                hermes_executable_sha256="1" * 64,
                                hermes_runtime_manifest_sha256="2" * 64,
                            )
                finally:
                    signal.signal(signal.SIGINT, previous)

                records = [
                    json.loads(line)
                    for line in receipt.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(terminal_work.call_count, 1)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(process.communications, 2)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["failure_codes"], ["alert_cleanup_unproven"])
                if operation == "config":
                    load_cache.assert_not_called()
                    stalled.assert_not_called()
                    orphans.assert_not_called()
                else:
                    load_cache.assert_called_once()
                    stalled.assert_called_once()
                    orphans.assert_not_called()

    def test_noncleanup_failure_pending_signal_receipts_before_replay(self) -> None:
        original_capture = pr_shepherd._capture_pending_alert_parent_signal

        for operation in ("delivery", "config"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / "receipts" / "watch.jsonl"
                process = _PersistentAlertGroupProcess()
                stalls = (
                    [{"repo": "example-org/SampleApp", "number": 1, "title": "blocked"}]
                    if operation == "delivery"
                    else []
                )
                emitted = [False]

                def killpg(_pid: int, signum: int) -> None:
                    if signum == 0:
                        raise ProcessLookupError

                def signal_before_capture(**kwargs: object) -> None:
                    if not emitted[0]:
                        emitted[0] = True
                        signal.raise_signal(signal.SIGINT)
                    original_capture(**kwargs)

                previous = signal.getsignal(signal.SIGINT)
                signal.signal(signal.SIGINT, signal.default_int_handler)
                try:
                    with (
                        patch.object(
                            pr_shepherd,
                            "load_cache",
                            return_value={"repos": {"SampleApp": {}}},
                        ) as load_cache,
                        patch.object(
                            pr_shepherd,
                            "stalled_auto_merges",
                            return_value=stalls,
                        ) as stalled,
                        patch.object(
                            pr_shepherd,
                            "orphaned_findings",
                            return_value=[],
                        ) as orphans,
                        patch.object(
                            pr_shepherd.subprocess,
                            "Popen",
                            return_value=process,
                        ) as popen,
                        patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                        patch.object(pr_shepherd.time, "sleep"),
                        patch.object(
                            pr_shepherd,
                            "_capture_pending_alert_parent_signal",
                            side_effect=signal_before_capture,
                        ) as capture,
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            pr_shepherd.daily_watch(
                                owner="example-org",
                                facts_path=Path("/tmp/repo-facts.json"),
                                min_age=timedelta(hours=2),
                                days=7,
                                alert_command=Path("/bin/sh"),
                                alert_destination_alias="fleet-ops",
                                require_alert_config=operation == "config",
                                receipt_path=receipt,
                                run_id="a" * 64,
                                runtime_sha="b" * 40,
                                runtime_manifest_sha256="c" * 64,
                                destination_target_sha256="d" * 64,
                                gh_executable_sha256="e" * 64,
                                hermes_runtime_sha="f" * 40,
                                hermes_executable_sha256="1" * 64,
                                hermes_runtime_manifest_sha256="2" * 64,
                            )
                finally:
                    signal.signal(signal.SIGINT, previous)

                records = [
                    json.loads(line)
                    for line in receipt.read_text(encoding="utf-8").splitlines()
                ]
                self.assertTrue(emitted[0])
                self.assertEqual(capture.call_count, 2)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(process.communications, 2)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["failure_codes"], ["alert_cancelled"])
                if operation == "config":
                    load_cache.assert_not_called()
                    stalled.assert_not_called()
                    orphans.assert_not_called()
                else:
                    load_cache.assert_called_once()
                    stalled.assert_called_once()
                    orphans.assert_not_called()

    def test_transient_handler_restore_failure_does_not_leak_mask_or_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "receipts" / "watch.jsonl"
            process = _PersistentAlertGroupProcess()
            original_signal = pr_shepherd.signal.signal
            original_append = pr_shepherd.append_watch_receipt
            prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
            previous = signal.getsignal(signal.SIGTERM)
            failed = [False]

            def caller_handler(_signum: int, _frame: object) -> None:
                return None

            def transient_restore_failure(signum: int, handler: object) -> object:
                if (
                    signum == signal.SIGTERM
                    and handler is caller_handler
                    and process.communications == 2
                    and not failed[0]
                ):
                    failed[0] = True
                    raise OSError("synthetic one-time restore failure")
                return original_signal(signum, handler)

            original_signal(signal.SIGTERM, caller_handler)
            try:
                with (
                    patch.object(
                        pr_shepherd.subprocess,
                        "Popen",
                        return_value=process,
                    ) as popen,
                    patch.object(pr_shepherd.os, "killpg"),
                    patch.object(pr_shepherd.time, "sleep"),
                    patch.object(pr_shepherd, "ALERT_GROUP_VERIFY_SECONDS", 0),
                    patch.object(
                        pr_shepherd.signal,
                        "signal",
                        side_effect=transient_restore_failure,
                    ),
                    patch.object(
                        pr_shepherd,
                        "append_watch_receipt",
                        wraps=original_append,
                    ) as append,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    result = pr_shepherd.daily_watch(
                        owner="example-org",
                        facts_path=Path("/tmp/repo-facts.json"),
                        min_age=timedelta(hours=2),
                        days=7,
                        alert_command=Path("/bin/sh"),
                        alert_destination_alias="fleet-ops",
                        require_alert_config=True,
                        receipt_path=receipt,
                        run_id="a" * 64,
                        runtime_sha="b" * 40,
                        runtime_manifest_sha256="c" * 64,
                        destination_target_sha256="d" * 64,
                        gh_executable_sha256="e" * 64,
                        hermes_runtime_sha="f" * 40,
                        hermes_executable_sha256="1" * 64,
                        hermes_runtime_manifest_sha256="2" * 64,
                    )
            finally:
                original_signal(signal.SIGTERM, previous)

            final_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
            records = [
                json.loads(line)
                for line in receipt.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result, 3)
        self.assertTrue(failed[0])
        self.assertEqual(final_mask, prior_mask)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(process.communications, 2)
        self.assertEqual(append.call_count, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["failure_codes"], ["alert_cleanup_unproven"])

    def test_post_transfer_signal_uses_caller_handler_without_callback(self) -> None:
        original_restore = pr_shepherd._restore_alert_signal_handlers

        for kind in (
            "default_int",
            "returning_callable",
            "raising_callable",
            "reraising_callable",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / "receipts" / "watch.jsonl"
                process = _SuccessfulAlertProcess()
                signum = signal.SIGINT if kind == "default_int" else signal.SIGTERM
                previous = signal.getsignal(signum)
                received: list[int] = []
                emitted = [False]

                if kind == "default_int":
                    handler = signal.default_int_handler
                    expected_exception = KeyboardInterrupt
                elif kind == "returning_callable":
                    def handler(received_signum: int, _frame: object) -> None:
                        received.append(received_signum)

                    expected_exception = InterruptedError
                elif kind == "raising_callable":
                    def handler(received_signum: int, _frame: object) -> None:
                        received.append(received_signum)
                        raise InterruptedError("custom caller cancellation")

                    expected_exception = InterruptedError
                else:
                    def handler(received_signum: int, _frame: object) -> None:
                        received.append(received_signum)
                        if len(received) == 1:
                            signal.raise_signal(received_signum)
                        raise InterruptedError("re-raised caller cancellation")

                    expected_exception = InterruptedError

                def killpg(_pid: int, probe: int) -> None:
                    if probe == 0:
                        raise ProcessLookupError

                def signal_before_restore(handlers: dict[int, object]) -> None:
                    if not emitted[0]:
                        emitted[0] = True
                        signal.raise_signal(signum)
                    original_restore(handlers)

                signal.signal(signum, handler)
                try:
                    with (
                        patch.object(
                            pr_shepherd,
                            "load_cache",
                            return_value={"repos": {"SampleApp": {}}},
                        ) as load_cache,
                        patch.object(
                            pr_shepherd.subprocess,
                            "Popen",
                            return_value=process,
                        ) as popen,
                        patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                        patch.object(
                            pr_shepherd,
                            "_restore_alert_signal_handlers",
                            side_effect=signal_before_restore,
                        ) as restore,
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        with self.assertRaises(expected_exception):
                            pr_shepherd.daily_watch(
                                owner="example-org",
                                facts_path=Path("/tmp/repo-facts.json"),
                                min_age=timedelta(hours=2),
                                days=7,
                                alert_command=Path("/bin/sh"),
                                alert_destination_alias="fleet-ops",
                                require_alert_config=True,
                                receipt_path=receipt,
                                run_id="a" * 64,
                                runtime_sha="b" * 40,
                                runtime_manifest_sha256="c" * 64,
                                destination_target_sha256="d" * 64,
                                gh_executable_sha256="e" * 64,
                                hermes_runtime_sha="f" * 40,
                                hermes_executable_sha256="1" * 64,
                                hermes_runtime_manifest_sha256="2" * 64,
                            )
                finally:
                    signal.signal(signum, previous)

                self.assertTrue(emitted[0])
                self.assertGreaterEqual(restore.call_count, 1)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(process.communications, 1)
                self.assertFalse(receipt.exists())
                load_cache.assert_not_called()
                if kind in ("returning_callable", "raising_callable"):
                    self.assertEqual(received, [signal.SIGTERM])
                elif kind == "reraising_callable":
                    self.assertEqual(received, [signal.SIGTERM, signal.SIGTERM])

    def test_late_handoff_preserves_sig_ign_without_cancellation_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "receipts" / "watch.jsonl"
            process = _SuccessfulAlertProcess()
            original_restore = pr_shepherd._restore_alert_signal_handlers
            previous = signal.getsignal(signal.SIGTERM)
            emitted = [False]

            def killpg(_pid: int, probe: int) -> None:
                if probe == 0:
                    raise ProcessLookupError

            def signal_before_restore(handlers: dict[int, object]) -> None:
                if not emitted[0]:
                    emitted[0] = True
                    signal.raise_signal(signal.SIGTERM)
                original_restore(handlers)

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                with (
                    patch.object(
                        pr_shepherd,
                        "load_cache",
                        return_value={"repos": {"SampleApp": {}}},
                    ),
                    patch.object(pr_shepherd, "stalled_auto_merges", return_value=[]),
                    patch.object(pr_shepherd, "orphaned_findings", return_value=[]),
                    patch.object(
                        pr_shepherd.subprocess,
                        "Popen",
                        return_value=process,
                    ),
                    patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                    patch.object(
                        pr_shepherd,
                        "_restore_alert_signal_handlers",
                        side_effect=signal_before_restore,
                    ),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    result = pr_shepherd.daily_watch(
                        owner="example-org",
                        facts_path=Path("/tmp/repo-facts.json"),
                        min_age=timedelta(hours=2),
                        days=7,
                        alert_command=Path("/bin/sh"),
                        alert_destination_alias="fleet-ops",
                        require_alert_config=True,
                        receipt_path=receipt,
                        run_id="a" * 64,
                        runtime_sha="b" * 40,
                        runtime_manifest_sha256="c" * 64,
                        destination_target_sha256="d" * 64,
                        gh_executable_sha256="e" * 64,
                        hermes_runtime_sha="f" * 40,
                        hermes_executable_sha256="1" * 64,
                        hermes_runtime_manifest_sha256="2" * 64,
                    )
            finally:
                signal.signal(signal.SIGTERM, previous)

            records = [
                json.loads(line)
                for line in receipt.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result, 0)
        self.assertTrue(emitted[0])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "success")
        self.assertEqual(records[0]["failure_codes"], [])

    def test_late_handoff_default_sigterm_terminates_once_without_recursion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "callback.txt"
            code = "\n".join(
                (
                    "import os, signal",
                    "from pathlib import Path",
                    "from git_janitor import pr_shepherd as p",
                    f"marker = Path({str(marker)!r})",
                    "old = signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])",
                    "os.kill(os.getpid(), signal.SIGTERM)",
                    "def callback(code):",
                    "    with marker.open('a', encoding='utf-8') as handle:",
                    "        handle.write(code + '\\n')",
                    "    signal.raise_signal(signal.SIGTERM)",
                    "p._finish_alert_signal_handoff(",
                    "    previous_handlers={signal.SIGTERM: signal.SIG_DFL},",
                    "    blocked_mask=old,",
                    "    callback=callback,",
                    "    callback_already_attempted=False,",
                    ")",
                    "raise SystemExit(99)",
                )
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = "src"
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )

            callbacks = marker.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        self.assertEqual(callbacks, ["alert_cancelled"])

    def test_handoff_callback_same_signal_preserves_prior_handler(self) -> None:
        for prior_raises in (False, True):
            with self.subTest(prior_raises=prior_raises):
                callbacks: list[str] = []
                received: list[int] = []
                previous = signal.getsignal(signal.SIGTERM)
                prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])

                def caller_handler(signum: int, _frame: object) -> None:
                    received.append(signum)
                    if prior_raises:
                        raise InterruptedError("caller cancellation handler")

                def callback(failure_code: str) -> None:
                    callbacks.append(failure_code)
                    signal.raise_signal(signal.SIGTERM)

                signal.signal(signal.SIGTERM, caller_handler)
                blocked_mask = signal.pthread_sigmask(
                    signal.SIG_BLOCK,
                    [signal.SIGTERM],
                )
                signal.raise_signal(signal.SIGTERM)
                try:
                    with patch.object(pr_shepherd.os, "kill") as kill:
                        with self.assertRaises(InterruptedError):
                            pr_shepherd._finish_alert_signal_handoff(
                                previous_handlers={
                                    signal.SIGTERM: caller_handler,
                                },
                                blocked_mask=blocked_mask,
                                callback=callback,
                                callback_already_attempted=False,
                            )
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
                    signal.signal(signal.SIGTERM, previous)

                self.assertEqual(callbacks, ["alert_cancelled"])
                self.assertEqual(received, [signal.SIGTERM, signal.SIGTERM])
                kill.assert_not_called()

    def test_late_handoff_callback_baseexception_cannot_preempt_signal(self) -> None:
        callbacks: list[str] = []
        received: list[int] = []
        previous = signal.getsignal(signal.SIGTERM)
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)
            raise InterruptedError("caller cancellation handler")

        def callback(failure_code: str) -> None:
            callbacks.append(failure_code)
            raise SystemExit("synthetic callback cancellation")

        signal.signal(signal.SIGTERM, caller_handler)
        blocked_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            [signal.SIGTERM],
        )
        signal.raise_signal(signal.SIGTERM)
        try:
            with self.assertRaises(InterruptedError) as replayed:
                pr_shepherd._finish_alert_signal_handoff(
                    previous_handlers={
                        signal.SIGTERM: caller_handler,
                    },
                    blocked_mask=blocked_mask,
                    callback=callback,
                    callback_already_attempted=False,
                )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
            signal.signal(signal.SIGTERM, previous)

        self.assertEqual(callbacks, ["alert_cancelled"])
        self.assertEqual(received, [signal.SIGTERM])
        self.assertIsInstance(replayed.exception.__cause__, InterruptedError)

    def test_callback_failure_diagnostic_cannot_swallow_signal_replay(self) -> None:
        class SignalStderr(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.emitted = False

            def write(self, value: str) -> int:
                if not self.emitted:
                    self.emitted = True
                    signal.raise_signal(signal.SIGTERM)
                return super().write(value)

        previous = signal.getsignal(signal.SIGTERM)

        def replay_handler(_signum: int, _frame: object) -> None:
            raise pr_shepherd._AlertSignalReplayed("synthetic caller replay")

        def failed_callback(_failure_code: str) -> None:
            raise RuntimeError("synthetic callback failure")

        signal.signal(signal.SIGTERM, replay_handler)
        try:
            for helper in (
                pr_shepherd._attempt_alert_terminal_callback,
                pr_shepherd._attempt_alert_handoff_callback,
            ):
                with self.subTest(helper=helper.__name__):
                    stderr = SignalStderr()
                    with (
                        redirect_stderr(stderr),
                        self.assertRaises(InterruptedError),
                    ):
                        helper(failed_callback, "alert_cleanup_unproven")
                    self.assertTrue(stderr.emitted)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_failed_handoff_install_replays_pending_parent_signal(self) -> None:
        process = _SuccessfulAlertProcess()
        callbacks: list[str] = []
        received: list[int] = []
        signal_calls = [0]
        original_signal = signal.signal
        previous = signal.getsignal(signal.SIGTERM)
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)
            raise InterruptedError("caller cancellation handler")

        def fail_first_handoff_install(signum: int, handler: object) -> object:
            signal_calls[0] += 1
            if signal_calls[0] == 4:
                signal.raise_signal(signal.SIGTERM)
                raise OSError("synthetic handoff install failure")
            return original_signal(signum, handler)

        def killpg(_pid: int, probe: int) -> None:
            if probe == 0:
                raise ProcessLookupError

        original_signal(signal.SIGTERM, caller_handler)
        try:
            with (
                patch.object(
                    pr_shepherd.signal,
                    "signal",
                    side_effect=fail_first_handoff_install,
                ),
                patch.object(
                    pr_shepherd.subprocess,
                    "Popen",
                    return_value=process,
                ),
                patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
            ):
                with self.assertRaises(InterruptedError) as replayed:
                    pr_shepherd._run_alert_process(
                        ["/bin/sh", "--check-config"],
                        input_text=None,
                        timeout_seconds=1,
                        cancelled_cleanup_callback=callbacks.append,
                    )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
            original_signal(signal.SIGTERM, previous)

        self.assertEqual(callbacks, ["alert_cancelled"])
        self.assertEqual(received, [signal.SIGTERM])
        self.assertIsInstance(replayed.exception.__cause__, InterruptedError)
        self.assertEqual(process.communications, 1)

    def test_failed_handoff_install_and_outer_restore_replay_signal(self) -> None:
        process = _SuccessfulAlertProcess()
        callbacks: list[str] = []
        received: list[int] = []
        signal_calls = [0]
        restore_calls = [0]
        original_signal = signal.signal
        original_restore = pr_shepherd._restore_alert_signal_handlers
        previous = signal.getsignal(signal.SIGTERM)
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)
            raise InterruptedError("caller cancellation handler")

        def fail_first_handoff_install(signum: int, handler: object) -> object:
            signal_calls[0] += 1
            if signal_calls[0] == 4:
                raise OSError("synthetic handoff install failure")
            return original_signal(signum, handler)

        def fail_outer_restore_then_succeed(handlers: dict[int, object]) -> None:
            restore_calls[0] += 1
            if restore_calls[0] == 1:
                signal.raise_signal(signal.SIGTERM)
                raise OSError("synthetic outer restore failure")
            original_restore(handlers)

        def killpg(_pid: int, probe: int) -> None:
            if probe == 0:
                raise ProcessLookupError

        original_signal(signal.SIGTERM, caller_handler)
        try:
            with (
                patch.object(
                    pr_shepherd.signal,
                    "signal",
                    side_effect=fail_first_handoff_install,
                ),
                patch.object(
                    pr_shepherd,
                    "_restore_alert_signal_handlers",
                    side_effect=fail_outer_restore_then_succeed,
                ),
                patch.object(
                    pr_shepherd.subprocess,
                    "Popen",
                    return_value=process,
                ),
                patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
            ):
                with self.assertRaises(InterruptedError) as replayed:
                    pr_shepherd._run_alert_process(
                        ["/bin/sh", "--check-config"],
                        input_text=None,
                        timeout_seconds=1,
                        cancelled_cleanup_callback=callbacks.append,
                    )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
            original_signal(signal.SIGTERM, previous)

        self.assertEqual(callbacks, ["alert_cleanup_unproven"])
        self.assertEqual(received, [signal.SIGTERM])
        self.assertIsInstance(replayed.exception.__cause__, InterruptedError)
        self.assertEqual(restore_calls[0], 2)
        self.assertEqual(process.communications, 1)

    def test_partial_handoff_persistent_restore_delegates_later_signal(self) -> None:
        process = _SuccessfulAlertProcess()
        callbacks: list[str] = []
        received: list[int] = []
        signal_calls = [0]
        restore_calls = [0]
        original_signal = signal.signal
        original_restore = pr_shepherd._restore_alert_signal_handlers
        watched_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        original_handlers = {
            signum: signal.getsignal(signum) for signum in watched_signals
        }

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)
            raise InterruptedError("caller cancellation handler")

        def fail_first_handoff_install(signum: int, handler: object) -> object:
            signal_calls[0] += 1
            if signal_calls[0] == 4:
                raise OSError("synthetic handoff install failure")
            return original_signal(signum, handler)

        def always_fail_restore(_handlers: dict[int, object]) -> None:
            restore_calls[0] += 1
            raise OSError(f"restore failure {restore_calls[0]}")

        def killpg(_pid: int, probe: int) -> None:
            if probe == 0:
                raise ProcessLookupError

        original_signal(signal.SIGTERM, caller_handler)
        try:
            with (
                patch.object(
                    pr_shepherd.signal,
                    "signal",
                    side_effect=fail_first_handoff_install,
                ),
                patch.object(
                    pr_shepherd,
                    "_restore_alert_signal_handlers",
                    side_effect=always_fail_restore,
                ),
                patch.object(
                    pr_shepherd.subprocess,
                    "Popen",
                    return_value=process,
                ),
                patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(pr_shepherd._AlertTerminalCleanupFailure):
                    pr_shepherd._run_alert_process(
                        ["/bin/sh", "--check-config"],
                        input_text=None,
                        timeout_seconds=1,
                        cancelled_cleanup_callback=callbacks.append,
                    )
            with self.assertRaises(InterruptedError) as replayed:
                signal.raise_signal(signal.SIGTERM)
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
            try:
                original_restore(dict(original_handlers))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)

        self.assertEqual(callbacks, ["alert_cleanup_unproven"])
        self.assertEqual(received, [signal.SIGTERM])
        self.assertIsInstance(replayed.exception.__cause__, InterruptedError)
        self.assertEqual(restore_calls[0], 1)
        self.assertEqual(process.communications, 1)

    def test_handoff_callback_cross_signal_delegates_both_priors_once(self) -> None:
        callbacks: list[str] = []
        received: list[int] = []
        watched = (signal.SIGINT, signal.SIGTERM)
        previous = {signum: signal.getsignal(signum) for signum in watched}
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)

        def callback(failure_code: str) -> None:
            callbacks.append(failure_code)
            signal.raise_signal(signal.SIGINT)

        for signum in watched:
            signal.signal(signum, caller_handler)
        blocked_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched)
        signal.raise_signal(signal.SIGTERM)
        try:
            with patch.object(pr_shepherd.os, "kill") as kill:
                with self.assertRaises(InterruptedError):
                    pr_shepherd._finish_alert_signal_handoff(
                        previous_handlers={
                            signum: caller_handler for signum in watched
                        },
                        blocked_mask=blocked_mask,
                        callback=callback,
                        callback_already_attempted=False,
                    )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
            for signum, handler in previous.items():
                signal.signal(signum, handler)

        self.assertEqual(callbacks, ["alert_cancelled"])
        self.assertEqual(received, [signal.SIGINT, signal.SIGTERM])
        kill.assert_not_called()

    def test_two_restore_failures_cannot_preempt_captured_parent_replay(self) -> None:
        class CancelledReapedGroup:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                if self.communications == 1:
                    signal.raise_signal(signal.SIGTERM)
                    raise AssertionError("parent signal was not intercepted")
                self.returncode = -signal.SIGTERM
                return "", ""

        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "receipts" / "watch.jsonl"
            process = CancelledReapedGroup()
            original_restore = pr_shepherd._restore_alert_signal_handlers
            prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
            previous = signal.getsignal(signal.SIGTERM)
            restore_calls = [0]
            received: list[int] = []

            def caller_handler(signum: int, _frame: object) -> None:
                received.append(signum)
                raise InterruptedError("caller cancellation handler")

            def fail_twice_then_restore(handlers: dict[int, object]) -> None:
                restore_calls[0] += 1
                if restore_calls[0] <= 2:
                    raise OSError(f"restore failure {restore_calls[0]}")
                original_restore(handlers)

            def killpg(_pid: int, probe: int) -> None:
                if probe == 0:
                    raise ProcessLookupError

            signal.signal(signal.SIGTERM, caller_handler)
            try:
                with (
                    patch.object(
                        pr_shepherd,
                        "load_cache",
                        return_value={"repos": {"SampleApp": {}}},
                    ) as load_cache,
                    patch.object(
                        pr_shepherd.subprocess,
                        "Popen",
                        return_value=process,
                    ),
                    patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                    patch.object(
                        pr_shepherd,
                        "_restore_alert_signal_handlers",
                        side_effect=fail_twice_then_restore,
                    ),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(InterruptedError) as replayed:
                        pr_shepherd.daily_watch(
                            owner="example-org",
                            facts_path=Path("/tmp/repo-facts.json"),
                            min_age=timedelta(hours=2),
                            days=7,
                            alert_command=Path("/bin/sh"),
                            alert_destination_alias="fleet-ops",
                            require_alert_config=True,
                            receipt_path=receipt,
                            run_id="a" * 64,
                            runtime_sha="b" * 40,
                            runtime_manifest_sha256="c" * 64,
                            destination_target_sha256="d" * 64,
                            gh_executable_sha256="e" * 64,
                            hermes_runtime_sha="f" * 40,
                            hermes_executable_sha256="1" * 64,
                            hermes_runtime_manifest_sha256="2" * 64,
                        )
            finally:
                signal.signal(signal.SIGTERM, previous)

            final_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
            records = [
                json.loads(line)
                for line in receipt.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(received, [signal.SIGTERM])
        self.assertIsInstance(replayed.exception.__cause__, InterruptedError)
        self.assertEqual(restore_calls[0], 3)
        self.assertEqual(final_mask, prior_mask)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["failure_codes"], ["alert_cancelled"])
        load_cache.assert_not_called()

    def test_persistent_restore_cannot_preempt_runtimeerror_signal(self) -> None:
        class CancelledReapedGroup:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                if self.communications == 1:
                    signal.raise_signal(signal.SIGTERM)
                    raise AssertionError("parent signal was not intercepted")
                self.returncode = -signal.SIGTERM
                return "", ""

        process = CancelledReapedGroup()
        callbacks: list[str] = []
        received: list[int] = []
        restore_calls = [0]
        original_restore = pr_shepherd._restore_alert_signal_handlers
        watched_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        original_handlers = {
            signum: signal.getsignal(signum) for signum in watched_signals
        }

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)
            raise RuntimeError("caller cancellation handler")

        def always_fail_restore(_handlers: dict[int, object]) -> None:
            restore_calls[0] += 1
            raise OSError(f"restore failure {restore_calls[0]}")

        def killpg(_pid: int, probe: int) -> None:
            if probe == 0:
                raise ProcessLookupError

        signal.signal(signal.SIGTERM, caller_handler)
        try:
            with (
                patch.object(
                    pr_shepherd.subprocess,
                    "Popen",
                    return_value=process,
                ),
                patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                patch.object(
                    pr_shepherd,
                    "_restore_alert_signal_handlers",
                    side_effect=always_fail_restore,
                ),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(InterruptedError) as replayed:
                    pr_shepherd._run_alert_process(
                        ["/bin/sh", "--check-config"],
                        input_text=None,
                        timeout_seconds=1,
                        cancelled_cleanup_callback=callbacks.append,
                    )
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
            try:
                original_restore(dict(original_handlers))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)

        self.assertEqual(callbacks, ["alert_cancelled"])
        self.assertEqual(received, [signal.SIGTERM])
        self.assertIsInstance(replayed.exception.__cause__, RuntimeError)
        self.assertEqual(restore_calls[0], 4)
        self.assertEqual(process.communications, 2)

    def test_handoff_restore_baseexception_propagates_without_duplicate_receipt(
        self,
    ) -> None:
        original_restore = pr_shepherd._restore_alert_signal_handlers

        for exception in (
            KeyboardInterrupt("synthetic restore interrupt"),
            SystemExit("synthetic restore exit"),
        ):
            with (
                self.subTest(exception=type(exception).__name__),
                tempfile.TemporaryDirectory() as tmp,
            ):
                receipt = Path(tmp) / "receipts" / "watch.jsonl"
                process = _PersistentAlertGroupProcess()
                prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
                prior_handlers = {
                    signum: signal.getsignal(signum)
                    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
                }
                restore_calls = [0]

                def fail_once_then_restore(handlers: dict[int, object]) -> None:
                    restore_calls[0] += 1
                    if restore_calls[0] == 1:
                        raise exception
                    original_restore(handlers)

                try:
                    with (
                        patch.object(
                            pr_shepherd,
                            "load_cache",
                            return_value={"repos": {"SampleApp": {}}},
                        ) as load_cache,
                        patch.object(
                            pr_shepherd.subprocess,
                            "Popen",
                            return_value=process,
                        ),
                        patch.object(pr_shepherd.os, "killpg"),
                        patch.object(pr_shepherd.time, "sleep"),
                        patch.object(pr_shepherd, "ALERT_GROUP_VERIFY_SECONDS", 0),
                        patch.object(
                            pr_shepherd,
                            "_restore_alert_signal_handlers",
                            side_effect=fail_once_then_restore,
                        ),
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        with self.assertRaises(type(exception)):
                            pr_shepherd.daily_watch(
                                owner="example-org",
                                facts_path=Path("/tmp/repo-facts.json"),
                                min_age=timedelta(hours=2),
                                days=7,
                                alert_command=Path("/bin/sh"),
                                alert_destination_alias="fleet-ops",
                                require_alert_config=True,
                                receipt_path=receipt,
                                run_id="a" * 64,
                                runtime_sha="b" * 40,
                                runtime_manifest_sha256="c" * 64,
                                destination_target_sha256="d" * 64,
                                gh_executable_sha256="e" * 64,
                                hermes_runtime_sha="f" * 40,
                                hermes_executable_sha256="1" * 64,
                                hermes_runtime_manifest_sha256="2" * 64,
                            )
                finally:
                    for signum, handler in prior_handlers.items():
                        signal.signal(signum, handler)

                final_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
                records = [
                    json.loads(line)
                    for line in receipt.read_text(encoding="utf-8").splitlines()
                ]
                final_handlers = {
                    signum: signal.getsignal(signum)
                    for signum in prior_handlers
                }

                self.assertEqual(restore_calls[0], 2)
                self.assertEqual(final_mask, prior_mask)
                self.assertEqual(final_handlers, prior_handlers)
                self.assertEqual(len(records), 1)
                self.assertEqual(
                    records[0]["failure_codes"],
                    ["alert_cleanup_unproven"],
                )
                load_cache.assert_not_called()

    def test_alert_cleanup_failure_commits_receipt_then_replays_parent_signal(
        self,
    ) -> None:
        class CancelledPersistentGroup:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                if self.communications == 1:
                    signal.raise_signal(signal.SIGTERM)
                    raise AssertionError("parent signal was not intercepted")
                self.returncode = -signal.SIGKILL
                return "", ""

        for operation in ("delivery", "config"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / "receipts" / "watch.jsonl"
                process = CancelledPersistentGroup()
                stalls = (
                    [{"repo": "example-org/SampleApp", "number": 1, "title": "blocked"}]
                    if operation == "delivery"
                    else []
                )
                received: list[int] = []
                previous = signal.getsignal(signal.SIGTERM)

                def caller_handler(signum: int, _frame: object) -> None:
                    received.append(signum)
                    raise InterruptedError("caller cancellation handler")

                signal.signal(signal.SIGTERM, caller_handler)
                try:
                    with (
                        patch.object(
                            pr_shepherd,
                            "load_cache",
                            return_value={"repos": {"SampleApp": {}}},
                        ) as load_cache,
                        patch.object(
                            pr_shepherd,
                            "stalled_auto_merges",
                            return_value=stalls,
                        ) as stalled,
                        patch.object(
                            pr_shepherd,
                            "orphaned_findings",
                            return_value=[],
                        ) as orphans,
                        patch.object(
                            pr_shepherd.subprocess,
                            "Popen",
                            return_value=process,
                        ) as popen,
                        patch.object(pr_shepherd.os, "killpg"),
                        patch.object(pr_shepherd.time, "sleep"),
                        patch.object(
                            pr_shepherd,
                            "ALERT_GROUP_VERIFY_SECONDS",
                            0,
                        ),
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        with self.assertRaises(InterruptedError) as replayed:
                            pr_shepherd.daily_watch(
                                owner="example-org",
                                facts_path=Path("/tmp/repo-facts.json"),
                                min_age=timedelta(hours=2),
                                days=7,
                                alert_command=Path("/bin/sh"),
                                alert_destination_alias="fleet-ops",
                                require_alert_config=operation == "config",
                                receipt_path=receipt,
                                run_id="a" * 64,
                                runtime_sha="b" * 40,
                                runtime_manifest_sha256="c" * 64,
                                destination_target_sha256="d" * 64,
                                gh_executable_sha256="e" * 64,
                                hermes_runtime_sha="f" * 40,
                                hermes_executable_sha256="1" * 64,
                                hermes_runtime_manifest_sha256="2" * 64,
                            )
                finally:
                    signal.signal(signal.SIGTERM, previous)

                records = [
                    json.loads(line)
                    for line in receipt.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(received, [signal.SIGTERM])
                self.assertIsInstance(replayed.exception.__cause__, InterruptedError)
                self.assertEqual(process.communications, 2)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "failure")
                self.assertEqual(records[0]["outcome"], "broken")
                self.assertEqual(records[0]["alert_delivery"], "failed")
                self.assertEqual(
                    records[0]["failure_codes"],
                    ["alert_cleanup_unproven"],
                )
                if operation == "config":
                    load_cache.assert_not_called()
                    stalled.assert_not_called()
                    orphans.assert_not_called()
                else:
                    load_cache.assert_called_once()
                    stalled.assert_called_once()
                    orphans.assert_not_called()

    def test_clean_alert_cancellation_commits_receipt_before_parent_replay(
        self,
    ) -> None:
        class CancelledReapedGroup:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                if self.communications == 1:
                    signal.raise_signal(signal.SIGTERM)
                    raise AssertionError("parent signal was not intercepted")
                self.returncode = -signal.SIGTERM
                return "", ""

        for operation in ("delivery", "config"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                receipt = Path(tmp) / "receipts" / "watch.jsonl"
                process = CancelledReapedGroup()
                stalls = (
                    [{"repo": "example-org/SampleApp", "number": 1, "title": "blocked"}]
                    if operation == "delivery"
                    else []
                )
                received: list[int] = []
                receipt_counts_at_replay: list[int] = []
                previous = signal.getsignal(signal.SIGTERM)

                def caller_handler(signum: int, _frame: object) -> None:
                    received.append(signum)
                    receipt_counts_at_replay.append(
                        len(receipt.read_text(encoding="utf-8").splitlines())
                    )
                    raise InterruptedError("caller cancellation handler")

                def killpg(_pid: int, signum: int) -> None:
                    if signum == 0:
                        raise ProcessLookupError

                signal.signal(signal.SIGTERM, caller_handler)
                try:
                    with (
                        patch.object(
                            pr_shepherd,
                            "load_cache",
                            return_value={"repos": {"SampleApp": {}}},
                        ) as load_cache,
                        patch.object(
                            pr_shepherd,
                            "stalled_auto_merges",
                            return_value=stalls,
                        ) as stalled,
                        patch.object(
                            pr_shepherd,
                            "orphaned_findings",
                            return_value=[],
                        ) as orphans,
                        patch.object(
                            pr_shepherd.subprocess,
                            "Popen",
                            return_value=process,
                        ) as popen,
                        patch.object(pr_shepherd.os, "killpg", side_effect=killpg),
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        with self.assertRaises(InterruptedError) as replayed:
                            pr_shepherd.daily_watch(
                                owner="example-org",
                                facts_path=Path("/tmp/repo-facts.json"),
                                min_age=timedelta(hours=2),
                                days=7,
                                alert_command=Path("/bin/sh"),
                                alert_destination_alias="fleet-ops",
                                require_alert_config=operation == "config",
                                receipt_path=receipt,
                                run_id="a" * 64,
                                runtime_sha="b" * 40,
                                runtime_manifest_sha256="c" * 64,
                                destination_target_sha256="d" * 64,
                                gh_executable_sha256="e" * 64,
                                hermes_runtime_sha="f" * 40,
                                hermes_executable_sha256="1" * 64,
                                hermes_runtime_manifest_sha256="2" * 64,
                            )
                finally:
                    signal.signal(signal.SIGTERM, previous)

                records = [
                    json.loads(line)
                    for line in receipt.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(received, [signal.SIGTERM])
                self.assertEqual(receipt_counts_at_replay, [1])
                self.assertIsInstance(replayed.exception.__cause__, InterruptedError)
                self.assertEqual(process.communications, 2)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "failure")
                self.assertEqual(records[0]["outcome"], "broken")
                self.assertEqual(records[0]["alert_delivery"], "failed")
                self.assertEqual(records[0]["failure_codes"], ["alert_cancelled"])
                if operation == "config":
                    load_cache.assert_not_called()
                    stalled.assert_not_called()
                    orphans.assert_not_called()
                else:
                    load_cache.assert_called_once()
                    stalled.assert_called_once()
                    orphans.assert_not_called()

    def test_daily_cancellation_broken_stderr_still_receipts_and_replays(self) -> None:
        class BrokenStderr(io.StringIO):
            def write(self, _value: str) -> int:
                raise OSError("synthetic closed stderr")

        class CancelledPersistentGroup:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                if self.communications == 1:
                    signal.raise_signal(signal.SIGTERM)
                    raise AssertionError("parent signal was not intercepted")
                self.returncode = -signal.SIGKILL
                return "", ""

        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "receipts" / "watch.jsonl"
            process = CancelledPersistentGroup()
            received: list[int] = []
            previous = signal.getsignal(signal.SIGTERM)

            def caller_handler(signum: int, _frame: object) -> None:
                received.append(signum)
                raise subprocess.TimeoutExpired(["caller-handler"], 1)

            signal.signal(signal.SIGTERM, caller_handler)
            try:
                with (
                    patch.object(
                        pr_shepherd,
                        "load_cache",
                        return_value={"repos": {"SampleApp": {}}},
                    ) as load_cache,
                    patch.object(
                        pr_shepherd.subprocess,
                        "Popen",
                        return_value=process,
                    ) as popen,
                    patch.object(pr_shepherd.os, "killpg"),
                    patch.object(pr_shepherd.time, "sleep"),
                    patch.object(
                        pr_shepherd,
                        "ALERT_GROUP_VERIFY_SECONDS",
                        0,
                    ),
                    redirect_stderr(BrokenStderr()),
                ):
                    with self.assertRaises(InterruptedError) as replayed:
                        pr_shepherd.daily_watch(
                            owner="example-org",
                            facts_path=Path("/tmp/repo-facts.json"),
                            min_age=timedelta(hours=2),
                            days=7,
                            alert_command=Path("/bin/sh"),
                            alert_destination_alias="fleet-ops",
                            require_alert_config=True,
                            receipt_path=receipt,
                            run_id="a" * 64,
                            runtime_sha="b" * 40,
                            runtime_manifest_sha256="c" * 64,
                            destination_target_sha256="d" * 64,
                            gh_executable_sha256="e" * 64,
                            hermes_runtime_sha="f" * 40,
                            hermes_executable_sha256="1" * 64,
                            hermes_runtime_manifest_sha256="2" * 64,
                        )
            finally:
                signal.signal(signal.SIGTERM, previous)

            records = [
                json.loads(line)
                for line in receipt.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(received, [signal.SIGTERM])
        self.assertIsInstance(
            replayed.exception.__cause__,
            subprocess.TimeoutExpired,
        )
        self.assertEqual(process.communications, 2)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "failure")
        self.assertEqual(records[0]["failure_codes"], ["alert_cleanup_unproven"])
        load_cache.assert_not_called()

    def test_alert_cancellation_replays_signal_when_receipt_commit_fails(self) -> None:
        received: list[int] = []
        previous = signal.getsignal(signal.SIGTERM)

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)

        def cancel_after_callback(
            _command: list[str],
            *,
            input_text: str | None,
            timeout_seconds: float,
            cancelled_cleanup_callback: object,
        ) -> subprocess.CompletedProcess[str]:
            self.assertIsNone(input_text)
            self.assertGreater(timeout_seconds, 0)
            self.assertTrue(callable(cancelled_cleanup_callback))
            try:
                cancelled_cleanup_callback("alert_cleanup_unproven")
            finally:
                pr_shepherd._resume_alert_parent_signal(
                    pr_shepherd._AlertParentSignal(signal.SIGTERM, None)
                )
            raise AssertionError("parent signal replay returned unexpectedly")

        signal.signal(signal.SIGTERM, caller_handler)
        stderr = io.StringIO()
        try:
            with (
                patch.object(
                    pr_shepherd,
                    "_run_alert_process",
                    side_effect=cancel_after_callback,
                ),
                patch.object(
                    pr_shepherd,
                    "append_watch_receipt",
                    side_effect=OSError("synthetic receipt failure"),
                ) as append,
                redirect_stderr(stderr),
            ):
                with self.assertRaises(InterruptedError):
                    pr_shepherd.daily_watch(
                        owner="example-org",
                        facts_path=Path("/tmp/repo-facts.json"),
                        min_age=timedelta(hours=2),
                        days=7,
                        alert_command=Path("/bin/sh"),
                        alert_destination_alias="fleet-ops",
                        require_alert_config=True,
                        receipt_path=Path("/tmp/watch.jsonl"),
                        run_id="a" * 64,
                        runtime_sha="b" * 40,
                        runtime_manifest_sha256="c" * 64,
                        destination_target_sha256="d" * 64,
                        gh_executable_sha256="e" * 64,
                        hermes_runtime_sha="f" * 40,
                        hermes_executable_sha256="1" * 64,
                        hermes_runtime_manifest_sha256="2" * 64,
                    )
        finally:
            signal.signal(signal.SIGTERM, previous)

        self.assertEqual(received, [signal.SIGTERM])
        append.assert_called_once()
        self.assertIn("receipt write failed (OSError)", stderr.getvalue())

    def test_public_alert_surfaces_replay_cancelled_cleanup_signal(self) -> None:
        class BrokenStderr(io.StringIO):
            def write(self, _value: str) -> int:
                raise OSError("synthetic closed stderr")

        class CancelledPersistentGroup:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                if self.communications == 1:
                    signal.raise_signal(signal.SIGTERM)
                    raise AssertionError("parent signal was not intercepted")
                self.returncode = -signal.SIGKILL
                return "", ""

        for operation in ("delivery", "config", "send"):
            with self.subTest(operation=operation):
                process = CancelledPersistentGroup()
                received: list[int] = []
                previous = signal.getsignal(signal.SIGTERM)

                def caller_handler(signum: int, _frame: object) -> None:
                    received.append(signum)
                    raise subprocess.TimeoutExpired(["caller-handler"], 1)

                signal.signal(signal.SIGTERM, caller_handler)
                try:
                    with (
                        patch.object(
                            pr_shepherd.subprocess,
                            "Popen",
                            return_value=process,
                        ) as popen,
                        patch.object(pr_shepherd.os, "killpg"),
                        patch.object(pr_shepherd.time, "sleep"),
                        patch.object(
                            pr_shepherd,
                            "ALERT_GROUP_VERIFY_SECONDS",
                            0,
                        ),
                        redirect_stderr(BrokenStderr()),
                    ):
                        with self.assertRaises(InterruptedError) as replayed:
                            if operation == "delivery":
                                pr_shepherd.deliver_alert(
                                    alert_command=Path("/bin/sh"),
                                    subject="subject",
                                    body="body",
                                )
                            elif operation == "config":
                                pr_shepherd.validate_alert_configuration(
                                    alert_command=Path("/bin/sh"),
                                    destination_alias="fleet-ops",
                                )
                            else:
                                pr_shepherd.send_alert(
                                    alert_command=Path("/bin/sh"),
                                    subject="subject",
                                    body="body",
                                )
                finally:
                    signal.signal(signal.SIGTERM, previous)

                self.assertEqual(received, [signal.SIGTERM])
                self.assertIsInstance(
                    replayed.exception.__cause__,
                    subprocess.TimeoutExpired,
                )
                self.assertEqual(process.communications, 2)
                self.assertEqual(popen.call_count, 1)

    def test_alert_group_probe_retries_eintr_and_waits_for_esrch(self) -> None:
        probe_results: list[BaseException | None] = [
            InterruptedError(),
            None,
            ProcessLookupError(),
        ]

        def probe_group(_pgid: int, signum: int) -> None:
            self.assertEqual(signum, 0)
            result = probe_results.pop(0)
            if result is not None:
                raise result

        with (
            patch.object(pr_shepherd.os, "killpg", side_effect=probe_group) as killpg,
            patch.object(pr_shepherd.time, "sleep") as sleep,
        ):
            pr_shepherd._wait_for_alert_process_group_exit(424242)

        self.assertEqual(killpg.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_alert_group_probe_errors_and_persistent_eintr_fail_closed(self) -> None:
        with patch.object(
            pr_shepherd.os,
            "killpg",
            side_effect=PermissionError("synthetic permission failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be proven gone"):
                pr_shepherd._wait_for_alert_process_group_exit(424242)

        with (
            patch.object(
                pr_shepherd.os,
                "killpg",
                side_effect=InterruptedError(),
            ),
            patch.object(pr_shepherd, "ALERT_GROUP_VERIFY_SECONDS", 0),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be proven gone"):
                pr_shepherd._wait_for_alert_process_group_exit(424242)

    def test_alert_cleanup_reaps_before_group_probe_and_never_resignals(self) -> None:
        events: list[int | str] = []
        probes = 0

        class ReapedProcess:
            pid = 424242

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                events.append("reap")
                return "", ""

        def kill_group(_pgid: int, signum: int) -> None:
            nonlocal probes
            events.append(signum)
            if signum == 0:
                probes += 1
                if probes == 2:
                    raise ProcessLookupError

        with (
            patch.object(pr_shepherd.os, "killpg", side_effect=kill_group),
            patch.object(pr_shepherd.time, "sleep"),
        ):
            pr_shepherd._cleanup_alert_process(ReapedProcess())

        self.assertEqual(events, [signal.SIGTERM, signal.SIGKILL, "reap", 0, 0])

    def test_alert_cleanup_signal_error_still_reaps_and_probes_then_fails(self) -> None:
        events: list[int | str] = []

        class ReapedProcess:
            pid = 424242

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                events.append("reap")
                return "", ""

        def kill_group(_pgid: int, signum: int) -> None:
            events.append(signum)
            if signum == signal.SIGTERM:
                raise PermissionError("synthetic permission failure")
            if signum == 0:
                raise ProcessLookupError

        with patch.object(pr_shepherd.os, "killpg", side_effect=kill_group):
            with self.assertRaisesRegex(RuntimeError, "signalling could not be completed"):
                pr_shepherd._cleanup_alert_process(ReapedProcess())

        self.assertEqual(events, [signal.SIGTERM, "reap", 0])

    def test_alert_child_does_not_inherit_supervisor_signal_mask(self) -> None:
        code = (
            "import signal\n"
            "watched = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}\n"
            "blocked = signal.pthread_sigmask(signal.SIG_BLOCK, [])\n"
            "raise SystemExit(1 if blocked & watched else 0)\n"
        )

        result = pr_shepherd._run_alert_process(
            [sys.executable, "-c", code],
            input_text=None,
            timeout_seconds=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_alert_signal_during_spawn_is_replayed_after_group_cleanup(self) -> None:
        class SpawnedProcess:
            pid = 424242
            returncode = None

            def __init__(self) -> None:
                self.communications = 0

            def communicate(self, **_kwargs: object) -> tuple[str, str]:
                self.communications += 1
                self.returncode = -signal.SIGKILL
                return "", ""

        spawned = SpawnedProcess()
        received: list[int] = []
        previous = signal.getsignal(signal.SIGTERM)

        def caller_handler(signum: int, _frame: object) -> None:
            received.append(signum)

        def spawn_with_signal(*_args: object, **_kwargs: object) -> SpawnedProcess:
            signal.raise_signal(signal.SIGTERM)
            return spawned

        def kill_group(_pgid: int, signum: int) -> None:
            if signum == 0:
                raise ProcessLookupError

        signal.signal(signal.SIGTERM, caller_handler)
        try:
            with (
                patch.object(
                    pr_shepherd.subprocess,
                    "Popen",
                    side_effect=spawn_with_signal,
                ),
                patch.object(
                    pr_shepherd.os,
                    "killpg",
                    side_effect=kill_group,
                ) as killpg,
                patch.object(pr_shepherd.time, "sleep"),
            ):
                with self.assertRaises(InterruptedError):
                    pr_shepherd._run_alert_process(
                        ["alert"],
                        input_text=None,
                        timeout_seconds=5,
                    )

            self.assertEqual(received, [signal.SIGTERM])
            self.assertEqual(spawned.communications, 1)
            self.assertEqual(
                [call.args[1] for call in killpg.call_args_list],
                [signal.SIGTERM, signal.SIGKILL, 0],
            )
            self.assertIs(signal.getsignal(signal.SIGTERM), caller_handler)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_send_alert_forwards_alias_and_surfaces_failure(self) -> None:
        result = subprocess.CompletedProcess(["fleet-send"], 1, stdout="", stderr="failed")
        with patch.object(pr_shepherd, "_run_alert_process", return_value=result) as run:
            with redirect_stderr(io.StringIO()):
                delivered = pr_shepherd.send_alert(
                    alert_command=Path("/bin/sh"),
                    destination_alias="fleet-ops",
                    subject="subject",
                    body="body",
                )

        self.assertFalse(delivered)
        self.assertIn("--destination-alias", run.call_args.args[0])

    def test_daily_watch_delivers_summary_body_but_prints_full_json(self) -> None:
        delivered: dict[str, str] = {}

        def capture_alert(
            *,
            alert_command: Path,
            subject: str,
            body: str,
            destination_alias: str | None = None,
            cancelled_cleanup_callback: object = None,
        ) -> bool:
            self.assertTrue(callable(cancelled_cleanup_callback))
            delivered["subject"] = subject
            delivered["body"] = body
            return True

        out = io.StringIO()
        with (
            patch.object(pr_shepherd, "load_cache", return_value={"repos": {"SampleApp": {}}}),
            patch.object(
                pr_shepherd,
                "stalled_auto_merges",
                return_value=[
                    {
                        "repo": "example-org/SampleApp",
                        "number": 338,
                        "title": "CI pending",
                        "check_status": "pending",
                    }
                ],
            ),
            patch.object(
                pr_shepherd,
                "orphaned_findings",
                return_value=[
                    {
                        "repo": "example-org/SampleTool",
                        "number": 192,
                        "priority": "P2",
                        "title": "Fix finding",
                    }
                ],
            ),
            patch.object(pr_shepherd, "_deliver_alert", side_effect=capture_alert),
            redirect_stdout(out),
        ):
            code = pr_shepherd.daily_watch(
                owner="example-org",
                facts_path=Path("/tmp/repo-facts.json"),
                min_age=timedelta(hours=2),
                days=7,
                alert_command=Path("/tmp/fleet-send"),
            )

        self.assertEqual(code, 2)
        self.assertEqual(
            delivered["subject"],
            "pr-shepherd watch: 1 stalled auto-merge PR(s), tests-only delivery",
        )
        self.assertIn("example-org/SampleApp#338", delivered["body"])
        self.assertNotIn("example-org/SampleTool#192", delivered["body"])
        self.assertNotIn('"orphans"', delivered["body"])
        self.assertEqual(json.loads(out.getvalue())["orphans"], [])


class InferPrTests(unittest.TestCase):
    def _runner(self, *, branch: str = "fix/topic", branch_returncode: int = 0):
        commands: list[list[str]] = []

        def runner(
            args: list[str], _cwd: Path | None, _timeout: int
        ) -> pr_shepherd.CommandResult:
            commands.append(args)
            if args[:3] == ["git", "branch", "--show-current"]:
                return pr_shepherd.CommandResult(
                    args, branch_returncode, f"{branch}\n", ""
                )
            if args[:3] == ["gh", "pr", "view"]:
                return pr_shepherd.CommandResult(
                    args, 0, json.dumps({"number": 494}), ""
                )
            raise AssertionError(f"unexpected command: {args}")

        return runner, commands

    def test_explicit_pr_skips_inference(self) -> None:
        runner, commands = self._runner()
        self.assertEqual(
            pr_shepherd.infer_pr(7, repo="owner/repo", runner=runner), 7
        )
        self.assertEqual(commands, [])

    def test_passes_branch_selector_with_repo_flag(self) -> None:
        runner, commands = self._runner(branch="fix/topic")
        self.assertEqual(
            pr_shepherd.infer_pr(None, repo="owner/repo", runner=runner), 494
        )
        self.assertIn(
            ["gh", "pr", "view", "fix/topic", "--repo", "owner/repo", "--json", "number"],
            commands,
        )

    def test_detached_head_fails_with_guidance(self) -> None:
        runner, _commands = self._runner(branch="")
        with self.assertRaises(SystemExit) as caught:
            pr_shepherd.infer_pr(None, repo="owner/repo", runner=runner)
        self.assertIn("pass --pr", str(caught.exception))


class SweepEvidenceClockSkewTests(unittest.TestCase):
    """`now` is pinned before the sweep's network calls, so live GitHub
    timestamps legitimately land after it."""

    def _now(self) -> datetime:
        return datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)

    def test_timestamp_just_after_now_is_accepted(self) -> None:
        now = self._now()
        raw = (now + timedelta(seconds=90)).isoformat().replace("+00:00", "Z")

        parsed = pr_shepherd._evidence_timestamp(raw, "startedAt", now=now)

        self.assertEqual(parsed, now + timedelta(seconds=90))

    def test_timestamp_far_in_the_future_is_still_rejected(self) -> None:
        now = self._now()
        raw = (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")

        with self.assertRaises(pr_shepherd._SweepEvidenceError):
            pr_shepherd._evidence_timestamp(raw, "startedAt", now=now)

    def test_naive_timestamp_is_still_rejected(self) -> None:
        with self.assertRaises(pr_shepherd._SweepEvidenceError):
            pr_shepherd._evidence_timestamp("2026-07-27T12:00:00", "startedAt", now=self._now())


if __name__ == "__main__":
    unittest.main()
