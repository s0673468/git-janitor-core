from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from git_janitor.ledger import AuditLedger, LedgerEntry


class AuditLedgerTests(unittest.TestCase):
    def test_record_creates_parent_dirs_and_writes_jsonl_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "audit.jsonl"
            ledger = AuditLedger(path, clock=_fixed_clock)

            ledger.record(_entry())

            self.assertTrue(path.exists())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["timestamp"], "2026-06-29T12:00:00+00:00")
            self.assertEqual(payload["repo"], "/tmp/repo")
            self.assertEqual(payload["category"], "delete-merged-branch")
            self.assertEqual(payload["disposition"], "auto-act")
            self.assertEqual(payload["mode"], "dry-run")
            self.assertEqual(payload["command"], ["git", "branch", "-d", "old-docs"])
            self.assertEqual(payload["before"], {"branch": "old-docs", "sha": "abc123"})
            self.assertEqual(payload["after"], {"branch": "old-docs", "exists": True})
            self.assertIsNone(payload["exit_code"])
            self.assertEqual(payload["status"], "would-apply")
            self.assertEqual(payload["rollback_hint"], "Recreate old-docs at abc123.")

    def test_record_appends_without_rewriting_existing_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "audit.jsonl"
            existing = '{"existing": true}'
            path.write_text(existing + "\n", encoding="utf-8")
            ledger = AuditLedger(path, clock=_fixed_clock)

            ledger.record(_entry())
            ledger.record(
                _entry(
                    category="merge-green-pr",
                    command=["gh", "pr", "merge", "42", "--squash", "--repo", "owner/repo"],
                    status="failed",
                    exit_code=1,
                    detail="gh pr merge failed",
                )
            )

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], existing)
            self.assertEqual(len(lines), 3)
            decoded = [json.loads(line) for line in lines]
            self.assertEqual(decoded[1]["status"], "would-apply")
            self.assertEqual(decoded[2]["status"], "failed")
            self.assertEqual(decoded[2]["exit_code"], 1)
            self.assertEqual(decoded[2]["detail"], "gh pr merge failed")


def _fixed_clock() -> datetime:
    return datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)


def _entry(
    *,
    category: str = "delete-merged-branch",
    command: list[str] | None = None,
    status: str = "would-apply",
    exit_code: int | None = None,
    detail: str = "dry run",
) -> LedgerEntry:
    return LedgerEntry(
        repo="/tmp/repo",
        category=category,
        disposition="auto-act",
        mode="dry-run",
        command=command or ["git", "branch", "-d", "old-docs"],
        before={"branch": "old-docs", "sha": "abc123"},
        after={"branch": "old-docs", "exists": True},
        exit_code=exit_code,
        status=status,
        rollback_hint="Recreate old-docs at abc123.",
        detail=detail,
    )


if __name__ == "__main__":
    unittest.main()
