from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from git_janitor.closeout_facts import collect_repo_facts, main
from git_janitor.models import CommandResult


FIXTURE = Path(__file__).parent / "fixtures" / "agent_ops" / "session_closeout_facts.json"


class CloseoutFactsTests(unittest.TestCase):
    def test_regression_fixtures(self) -> None:
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        for case in cases:
            with self.subTest(case=case["name"]):
                fake = _runner_for_case(case)

                facts = collect_repo_facts(
                    Path(case["repo_path"]),
                    runner=fake,
                    clock=_fixed_clock,
                )

                self.assertEqual(facts["complete"], case["expected"]["complete"])
                self.assertEqual(
                    facts["authoritative_measurement_ref"],
                    case["expected"]["authoritative_measurement_ref"],
                )
                self.assertEqual(facts["warnings"], case["expected"]["warnings"])
                self.assertEqual(facts["github_archived"], case["github_archived"])
                self.assertEqual(
                    facts["checkout"]["behind_remote_default"],
                    case["behind"],
                )
                self.assertNotIn("lifecycle", facts)
                self.assertNotIn("status", facts)

    def test_fetch_failure_is_incomplete_and_has_no_authoritative_ref(self) -> None:
        case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][0]
        fake = _runner_for_case(case, fetch_returncode=1)

        facts = collect_repo_facts(
            Path(case["repo_path"]),
            runner=fake,
            clock=_fixed_clock,
        )

        self.assertFalse(facts["complete"])
        self.assertIsNone(facts["authoritative_measurement_ref"])
        self.assertIn("fetch-failed", facts["errors"])

    def test_origin_head_mismatch_is_visible_but_github_default_remains_authoritative(
        self,
    ) -> None:
        case = dict(json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][1])
        case["origin_head_ref"] = "origin/trunk"
        fake = _runner_for_case(case)

        facts = collect_repo_facts(
            Path(case["repo_path"]),
            runner=fake,
            clock=_fixed_clock,
        )

        self.assertTrue(facts["complete"])
        self.assertEqual(facts["authoritative_measurement_ref"], "origin/main")
        self.assertIn("origin-head-mismatch", facts["warnings"])

    def test_detached_linked_worktree_is_compared_with_remote_default(self) -> None:
        case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][1]
        fake = _runner_for_case(case, linked_worktree=True)

        facts = collect_repo_facts(
            Path(case["repo_path"]),
            runner=fake,
            clock=_fixed_clock,
        )

        self.assertTrue(facts["complete"])
        self.assertEqual(len(facts["worktrees"]), 1)
        self.assertTrue(facts["worktrees"][0]["detached"])
        self.assertEqual(facts["worktrees"][0]["behind_remote_default"], 1)

    def test_open_pr_inventory_accepts_multiple_paginated_pages(self) -> None:
        case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][1]
        pull_requests = [
            [_pull_request(7, "codex/one", "1" * 40)],
            [_pull_request(8, "codex/two", "2" * 40)],
        ]
        fake = _runner_for_case(case, open_pr_stdout=json.dumps(pull_requests))

        facts = collect_repo_facts(
            Path(case["repo_path"]),
            runner=fake,
            clock=_fixed_clock,
        )

        self.assertTrue(facts["complete"])
        self.assertEqual([pr["number"] for pr in facts["open_prs"]], [7, 8])

    def test_empty_open_pr_response_is_incomplete_instead_of_assumed_empty(self) -> None:
        case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][1]
        fake = _runner_for_case(case, open_pr_stdout="")

        facts = collect_repo_facts(
            Path(case["repo_path"]),
            runner=fake,
            clock=_fixed_clock,
        )

        self.assertFalse(facts["complete"])
        self.assertIn("open-pr-inventory-malformed", facts["errors"])

    def test_cli_emits_json_writes_output_and_returns_three_when_incomplete(self) -> None:
        case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][0]
        fake = _runner_for_case(case, fetch_returncode=1)
        stdout = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "facts.json"
            with mock.patch("sys.stdout", stdout):
                status = main(
                    [
                        "--repo",
                        case["repo_path"],
                        "--output",
                        str(output),
                    ],
                    runner=fake,
                    clock=_fixed_clock,
                )
            written = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(status, 3)
        self.assertEqual(json.loads(stdout.getvalue()), written)
        self.assertFalse(written["complete"])
        self.assertEqual(written["schema_version"], 1)


class FakeRunner:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, tuple[str, ...]], list[CommandResult]] = defaultdict(list)
        self.calls: list[tuple[Path | None, list[str]]] = []

    def add(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        command = list(args)
        self.responses[(str(cwd), tuple(command))].append(
            CommandResult(
                args=command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        )

    def __call__(
        self,
        args: list[str],
        cwd: Path | None = None,
        timeout: int = 45,
    ) -> CommandResult:
        del timeout
        self.calls.append((cwd, list(args)))
        key = (str(cwd), tuple(args))
        if not self.responses[key]:
            raise AssertionError(f"unexpected command {args!r} cwd={cwd}")
        return self.responses[key].pop(0)


def _runner_for_case(
    case: dict,
    *,
    fetch_returncode: int = 0,
    linked_worktree: bool = False,
    open_pr_stdout: str = "[[]]",
) -> FakeRunner:
    fake = FakeRunner()
    repo = Path(case["repo_path"])
    remote_default_ref = f"origin/{case['default_branch']}"
    fake.add(
        ["git", "fetch", "--prune", "origin"],
        cwd=repo,
        returncode=fetch_returncode,
        stderr="network unavailable" if fetch_returncode else "",
    )
    fake.add(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        stdout=f"https://github.com/{case['github_repo']}.git",
    )
    fake.add(
        [
            "gh",
            "repo",
            "view",
            case["github_repo"],
            "--json",
            "nameWithOwner,isArchived,defaultBranchRef",
        ],
        cwd=repo,
        stdout=json.dumps(
            {
                "nameWithOwner": case["github_repo"],
                "isArchived": case["github_archived"],
                "defaultBranchRef": {"name": case["default_branch"]},
            }
        ),
    )
    fake.add(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo,
        stdout=case["origin_head_ref"],
    )
    fake.add(
        ["git", "rev-parse", "--verify", remote_default_ref],
        cwd=repo,
        stdout=case["remote_default_head"],
    )
    fake.add(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo,
        stdout=case["current_branch"],
    )
    fake.add(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        stdout=case["current_head"],
    )
    fake.add(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=repo,
    )
    fake.add(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...{remote_default_ref}"],
        cwd=repo,
        stdout=f"{case['ahead']}\t{case['behind']}",
    )
    fake.add(
        ["git", "diff", "--quiet", remote_default_ref, "HEAD"],
        cwd=repo,
        returncode=0 if case["tree_matches"] else 1,
    )
    worktree_output = (
        f"worktree {repo}\n"
        f"HEAD {case['current_head']}\n"
        f"branch refs/heads/{case['current_branch']}\n"
    )
    if linked_worktree:
        linked_path = Path("/tmp/openloop-review")
        linked_head = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        worktree_output += f"\nworktree {linked_path}\nHEAD {linked_head}\ndetached\n"
        fake.add(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=linked_path,
        )
        fake.add(
            [
                "git",
                "rev-list",
                "--left-right",
                "--count",
                f"{linked_head}...{remote_default_ref}",
            ],
            cwd=linked_path,
            stdout="0\t1",
        )
        fake.add(
            ["git", "diff", "--quiet", remote_default_ref, linked_head],
            cwd=linked_path,
            returncode=1,
        )
    fake.add(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        stdout=worktree_output,
    )
    fake.add(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{case['github_repo']}/pulls?state=open&per_page=100",
        ],
        cwd=repo,
        stdout=open_pr_stdout,
    )
    return fake


def _pull_request(number: int, head_ref: str, head_sha: str) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "head": {"ref": head_ref, "sha": head_sha},
        "base": {"ref": "main"},
        "draft": False,
        "updated_at": "2026-07-24T13:00:00Z",
    }


def _fixed_clock() -> datetime:
    return datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main()
