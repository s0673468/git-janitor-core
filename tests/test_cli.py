from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from git_janitor import cli
from git_janitor.models import BranchState, CommandResult, PullRequestState, RepoState


REPO_PATH = Path("/tmp/repo")


class CliExecutionTests(unittest.TestCase):
    def test_apply_and_dry_run_are_mutually_exclusive(self) -> None:
        parser = cli.build_parser()

        with mock.patch("sys.stderr", StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--apply", "--dry-run"])

    def test_default_run_does_not_execute_auto_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _write_config(
                tmpdir,
                ledger_path=Path(tmpdir) / "audit.jsonl",
                apply_categories=["merge-green-pr"],
                auto_merge=True,
            )
            fake = FakeRunner()
            stdout = StringIO()

            with _patched_scan(prs=[_green_pr()], stdout=stdout):
                result = cli.main(
                    ["--config", str(config_path), "--no-fetch"],
                    runner=fake,
                    clock=_fixed_clock,
                )

            self.assertEqual(result, 0)
            self.assertEqual(fake.calls, [])
            self.assertFalse((Path(tmpdir) / "audit.jsonl").exists())
            self.assertNotIn("## Execution Results", stdout.getvalue())

    def test_dry_run_uses_cli_allowlist_as_narrowing_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "audit.jsonl"
            config_path = _write_config(
                tmpdir,
                ledger_path=ledger_path,
                apply_categories=["delete-merged-branch", "merge-green-pr"],
                auto_delete=True,
                auto_merge=True,
            )
            fake = FakeRunner()
            _add_branch_preflight(fake)
            stdout = StringIO()

            with _patched_scan(repo=_repo_with_merged_branch(), prs=[_green_pr()], stdout=stdout):
                result = cli.main(
                    [
                        "--config",
                        str(config_path),
                        "--no-fetch",
                        "--dry-run",
                        "--apply-categories",
                        "delete-merged-branch",
                    ],
                    runner=fake,
                    clock=_fixed_clock,
                )

            self.assertEqual(result, 0)
            self.assertEqual(fake.mutating_calls(), [])
            ledger_entries = [
                json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([entry["status"] for entry in ledger_entries], ["skipped", "would-apply"])
            self.assertEqual(ledger_entries[0]["category"], "merge-green-pr")
            self.assertEqual(ledger_entries[1]["category"], "delete-merged-branch")
            self.assertIn("## Execution Results", stdout.getvalue())
            self.assertIn("Status: `would-apply`", stdout.getvalue())

    def test_duplicate_repo_basenames_are_non_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "audit.jsonl"
            config_path = _write_config(
                tmpdir,
                ledger_path=ledger_path,
                apply_categories=["delete-merged-branch"],
                auto_delete=True,
            )
            first = _repo_with_merged_branch(path="/tmp/one/repo")
            second = _repo_with_merged_branch(path="/tmp/two/repo")
            fake = FakeRunner()
            stdout = StringIO()

            with _patched_scan(repos=[first, second], stdout=stdout):
                result = cli.main(
                    [
                        "--config",
                        str(config_path),
                        "--no-fetch",
                        "--apply",
                    ],
                    runner=fake,
                    clock=_fixed_clock,
                )

            self.assertEqual(result, 0)
            self.assertEqual(fake.calls, [])
            ledger_entries = [
                json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([entry["status"] for entry in ledger_entries], ["skipped", "skipped"])
            self.assertTrue(
                all("no unique repo path" in entry["detail"] for entry in ledger_entries)
            )


class FakeRunner:
    def __init__(self) -> None:
        self.responses: dict[tuple[str | None, tuple[str, ...]], list[CommandResult]] = defaultdict(
            list
        )
        self.calls: list[list[str]] = []

    def add(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = REPO_PATH,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        command = list(args)
        self.responses[_key(command, cwd)].append(
            CommandResult(args=command, returncode=returncode, stdout=stdout, stderr=stderr)
        )

    def __call__(
        self,
        args: list[str],
        cwd: Path | None = None,
        timeout: int = 45,
    ) -> CommandResult:
        del timeout
        self.calls.append(list(args))
        key = _key(args, cwd)
        if not self.responses[key]:
            raise AssertionError(f"unexpected command {args!r} cwd={cwd}")
        return self.responses[key].pop(0)

    def mutating_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[:3] == ["git", "branch", "-d"]]


@contextmanager
def _patched_scan(
    *,
    repo: RepoState | None = None,
    repos: list[RepoState] | None = None,
    prs: list[PullRequestState] | None = None,
    stdout: StringIO,
):
    repos = repos or [repo or _repo()]
    with mock.patch(
        "git_janitor.cli.discover_repos",
        return_value=[Path(item.path) for item in repos],
    ), mock.patch(
        "git_janitor.cli.scan_repo",
        side_effect=repos,
    ), mock.patch("git_janitor.cli.gh_available", return_value=True), mock.patch(
        "git_janitor.cli.list_pull_requests",
        return_value=(prs or [], []),
    ), mock.patch("sys.stdout", stdout):
        yield


def _write_config(
    tmpdir: str,
    *,
    ledger_path: Path,
    apply_categories: list[str],
    auto_delete: bool = False,
    auto_merge: bool = False,
) -> Path:
    path = Path(tmpdir) / "config.toml"
    quoted_categories = ", ".join(f'"{category}"' for category in apply_categories)
    path.write_text(
        f"""
        [scanner]
        repos = ["/tmp/repo"]
        scan_roots = []
        fetch_prune = false
        high_risk_patterns = ["workflow"]

        [actions]
        auto_merge_green_prs = {str(auto_merge).lower()}
        auto_delete_merged_branches = {str(auto_delete).lower()}
        auto_mark_drafts_ready = false
        auto_fast_forward_default_branch = false
        apply_categories = [{quoted_categories}]
        ledger_path = "{ledger_path}"
        """,
        encoding="utf-8",
    )
    return path


def _repo(*, path: str = str(REPO_PATH)) -> RepoState:
    return RepoState(
        path=path,
        name="repo",
        current_branch="main",
        default_branch="main",
        default_ref="origin/main",
        github_repo="owner/repo",
        remote_url="https://github.com/owner/repo.git",
    )


def _repo_with_merged_branch(*, path: str = str(REPO_PATH)) -> RepoState:
    repo = _repo(path=path)
    repo.branches = [
        BranchState(
            name="old-docs",
            upstream="origin/old-docs",
            merged_to_default=True,
            unique_commit_count=0,
            current=False,
        )
    ]
    return repo


def _green_pr() -> PullRequestState:
    return PullRequestState(
        repo="owner/repo",
        number=42,
        title="Docs cleanup",
        url="https://github.com/owner/repo/pull/42",
        head_ref="docs-cleanup",
        base_ref="main",
        is_draft=False,
        merge_state="CLEAN",
        review_decision=None,
        check_status="success",
        changed_files=["README.md"],
    )


def _add_branch_preflight(fake: FakeRunner) -> None:
    fake.add(["git", "rev-parse", "old-docs"], stdout="oldsha")
    fake.add(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="main")
    fake.add(
        ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
        stdout="## main...origin/main",
    )
    fake.add(["git", "merge-base", "--is-ancestor", "old-docs", "origin/main"])
    fake.add(["git", "cherry", "-v", "origin/main", "old-docs"], stdout="")


def _fixed_clock() -> datetime:
    return datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)


def _key(args: Sequence[str], cwd: Path | None) -> tuple[str | None, tuple[str, ...]]:
    return (str(cwd) if cwd else None, tuple(args))


if __name__ == "__main__":
    unittest.main()
