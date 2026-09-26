from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from git_janitor.autonomy import AutomationPolicy
from git_janitor.config import ScannerConfig
from git_janitor.execute import ActionExecutor, FORBIDDEN_COMMAND_ARGS, execute_decisions
from git_janitor.ledger import AuditLedger
from git_janitor.models import AutomationDecision, CommandResult


FIXTURES = Path(__file__).parent / "fixtures" / "agent_ops"
REPO_PATH = Path("/tmp/repo")


class ActionExecutorFixtureTests(unittest.TestCase):
    def test_execution_cases_match_fixture_expectations(self) -> None:
        for case in _load_cases("execution.json"):
            with self.subTest(case=case["name"]):
                with tempfile.TemporaryDirectory() as tmpdir:
                    fake = _runner_for_scenario(case["scenario"])
                    ledger_path = Path(tmpdir) / "audit.jsonl"
                    executor = _executor(
                        case["policy"],
                        ledger_path,
                        mode="apply",
                        runner=fake,
                    )

                    result = executor.execute(_decision(case["decision"]))

                    self.assertEqual(result.status, case["expected"]["status"])
                    self.assertEqual(result.command, case["expected"]["command"])
                    ledger_lines = _ledger_lines(ledger_path)
                    self.assertEqual(len(ledger_lines), 1)
                    self.assertEqual(ledger_lines[0]["status"], case["expected"]["status"])
                    self.assertEqual(ledger_lines[0]["command"], case["expected"]["command"])
                    self.assertEqual(ledger_lines[0]["category"], case["decision"]["category"])
                    if result.status in {"drifted", "skipped"}:
                        self.assertEqual(fake.mutating_calls(), [])
                    elif result.status == "applied":
                        self.assertIn(case["expected"]["command"], fake.mutating_calls())

    def test_dry_run_records_would_apply_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = _runner_for_scenario("delete_branch_success")
            ledger_path = Path(tmpdir) / "audit.jsonl"
            executor = _executor(
                {
                    "auto_delete_merged_branches": True,
                    "apply_categories": ["delete-merged-branch"],
                },
                ledger_path,
                mode="dry-run",
                runner=fake,
            )

            result = executor.execute(
                _decision(
                    {
                        "disposition": "auto-act",
                        "category": "delete-merged-branch",
                        "title": "repo:old-docs can be deleted",
                        "reason": "The branch is merged to the default ref and has no unique commits.",
                        "recommended_action": "Delete exactly old-docs with git branch -d.",
                        "evidence": ["default_ref=origin/main", "unique_commit_count=0"],
                    }
                )
            )

            self.assertEqual(result.status, "would-apply")
            self.assertEqual(result.command, ["git", "branch", "-d", "old-docs"])
            self.assertEqual(fake.mutating_calls(), [])
            ledger_entry = _ledger_lines(ledger_path)[0]
            self.assertEqual(ledger_entry["mode"], "dry-run")
            self.assertEqual(ledger_entry["status"], "would-apply")
            self.assertIsNone(ledger_entry["exit_code"])

    def test_apply_failure_does_not_abort_independent_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = _runner_for_scenario("delete_branch_failure_then_fast_forward_success")
            ledger_path = Path(tmpdir) / "audit.jsonl"
            policy = AutomationPolicy(
                auto_delete_merged_branches=True,
                auto_fast_forward_default_branch=True,
                apply_categories=frozenset(
                    {"delete-merged-branch", "fast-forward-default-branch"}
                ),
            )

            results = execute_decisions(
                [
                    _decision(
                        {
                            "disposition": "auto-act",
                            "category": "delete-merged-branch",
                            "title": "repo:old-docs can be deleted",
                            "reason": "The branch is merged.",
                            "recommended_action": "Delete exactly old-docs with git branch -d.",
                            "evidence": ["default_ref=origin/main", "unique_commit_count=0"],
                        }
                    ),
                    _decision(
                        {
                            "disposition": "auto-act",
                            "category": "fast-forward-default-branch",
                            "title": "repo: default branch can be fast-forwarded",
                            "reason": "The repo is clean and only behind upstream.",
                            "recommended_action": "Fast-forward with git pull --ff-only.",
                            "evidence": ["branch=main", "upstream=origin/main", "behind=2"],
                        }
                    ),
                ],
                policy=policy,
                ledger=AuditLedger(ledger_path, clock=_fixed_clock),
                mode="apply",
                runner=fake,
                repo_paths={"repo": REPO_PATH},
                config=_config(),
            )

            self.assertEqual([result.status for result in results], ["failed", "applied"])
            self.assertEqual(
                fake.mutating_calls(),
                [
                    ["git", "branch", "-d", "old-docs"],
                    ["git", "pull", "--ff-only"],
                ],
            )
            ledger_lines = _ledger_lines(ledger_path)
            self.assertEqual([entry["status"] for entry in ledger_lines], ["failed", "applied"])
            self.assertEqual(ledger_lines[0]["exit_code"], 1)
            self.assertIn("refusing to delete", ledger_lines[0]["detail"])

    def test_supported_fixture_commands_do_not_use_forbidden_flags(self) -> None:
        for case in _load_cases("execution.json"):
            command = case["expected"]["command"]
            if not command:
                continue
            with self.subTest(case=case["name"]):
                self.assertFalse(set(command) & FORBIDDEN_COMMAND_ARGS)
                self.assertNotEqual(command[:3], ["git", "branch", "-D"])

    def test_auto_act_pr_with_risk_evidence_is_skipped_before_gh_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = _runner_for_scenario("no_commands")
            ledger_path = Path(tmpdir) / "audit.jsonl"
            executor = _executor(
                {
                    "auto_merge_green_prs": True,
                    "apply_categories": ["merge-green-pr"],
                },
                ledger_path,
                mode="apply",
                runner=fake,
            )

            result = executor.execute(
                _decision(
                    {
                        "disposition": "auto-act",
                        "category": "merge-green-pr",
                        "title": "owner/repo#47: workflow change can be squash-merged",
                        "reason": "Synthetic unsafe decision.",
                        "recommended_action": "Squash-merge the ready PR.",
                        "evidence": ["risk=.github workflow/config change"],
                    }
                )
            )

            self.assertEqual(result.status, "skipped")
            self.assertEqual(fake.calls, [])
            ledger_entry = _ledger_lines(ledger_path)[0]
            self.assertIn("high-risk", ledger_entry["detail"])

    def test_unsupported_runner_category_is_skipped_even_when_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = _runner_for_scenario("no_commands")
            ledger_path = Path(tmpdir) / "audit.jsonl"
            executor = _executor(
                {
                    "auto_repair_runner_tools": True,
                    "apply_categories": ["runner-tool-missing"],
                },
                ledger_path,
                mode="apply",
                runner=fake,
            )

            result = executor.execute(
                _decision(
                    {
                        "disposition": "auto-act",
                        "category": "runner-tool-missing",
                        "title": "Self-hosted runner is missing gtar",
                        "reason": "Runner repair is infrastructure.",
                        "recommended_action": "Install gtar only with explicit approval.",
                        "evidence": ["gtar: command not found"],
                    }
                )
            )

            self.assertEqual(result.status, "skipped")
            self.assertEqual(fake.calls, [])
            ledger_entry = _ledger_lines(ledger_path)[0]
            self.assertIn("unsupported", ledger_entry["detail"])


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
        key = _key(command, cwd)
        self.responses[key].append(
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
        return [call for call in self.calls if _is_mutating(call)]


def _runner_for_scenario(name: str) -> FakeRunner:
    fake = FakeRunner()
    if name == "no_commands":
        return fake
    if name == "fast_forward_success":
        _add_fast_forward(fake, status=_status_behind(), before="aaa111", after="bbb222")
        return fake
    if name == "fast_forward_dirty":
        _add_fast_forward_verify(
            fake,
            status=f"{_status_behind()}\n M src/app.py",
            before="aaa111",
        )
        return fake
    if name == "fast_forward_ahead":
        _add_fast_forward_verify(
            fake,
            status="## main...origin/main [ahead 1, behind 2]",
            before="aaa111",
        )
        return fake
    if name == "fast_forward_changed_upstream":
        _add_fast_forward_verify(
            fake,
            status="## main...origin/other [behind 2]",
            before="aaa111",
        )
        return fake
    if name == "delete_branch_success":
        _add_delete_branch(fake, mutation_returncode=0, after_exists=False)
        return fake
    if name == "delete_branch_current":
        _add_delete_branch_verify(fake, current_branch="old-docs")
        return fake
    if name == "delete_default_branch":
        _add_delete_branch_verify(fake, branch_name="main", current_branch="feature")
        return fake
    if name == "merge_pr_success":
        _add_pr_merge(fake, number=42, before=_pr_view(number=42), after=_pr_view(number=42, state="MERGED"))
        return fake
    if name == "mark_ready_success":
        _add_pr_ready(fake, number=43, before=_pr_view(number=43, is_draft=True), after=_pr_view(number=43))
        return fake
    if name == "merge_pr_high_risk":
        _add_pr_view(fake, 44, _pr_view(number=44, files=[".github/workflows/test.yml"]))
        return fake
    if name == "mark_ready_not_draft":
        _add_pr_view(fake, 45, _pr_view(number=45, is_draft=False))
        return fake
    if name == "delete_branch_failure_then_fast_forward_success":
        _add_delete_branch(fake, mutation_returncode=1, mutation_stderr="refusing to delete")
        _add_fast_forward(fake, status=_status_behind(), before="aaa111", after="bbb222")
        return fake
    raise AssertionError(f"unknown scenario {name}")


def _add_fast_forward(
    fake: FakeRunner,
    *,
    status: str,
    before: str,
    after: str,
) -> None:
    _add_fast_forward_verify(fake, status=status, before=before)
    fake.add(["git", "pull", "--ff-only"], stdout="updated")
    fake.add(["git", "rev-parse", "HEAD"], stdout=after)


def _add_fast_forward_verify(fake: FakeRunner, *, status: str, before: str) -> None:
    fake.add(["git", "rev-parse", "HEAD"], stdout=before)
    fake.add(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="main")
    fake.add(
        ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
        stdout=status,
    )


def _add_delete_branch(
    fake: FakeRunner,
    *,
    mutation_returncode: int,
    mutation_stderr: str = "",
    after_exists: bool = True,
    branch_name: str = "old-docs",
) -> None:
    _add_delete_branch_verify(fake, branch_name=branch_name, current_branch="main")
    fake.add(
        ["git", "branch", "-d", branch_name],
        returncode=mutation_returncode,
        stderr=mutation_stderr,
    )
    fake.add(
        ["git", "rev-parse", "--verify", "--quiet", branch_name],
        returncode=0 if after_exists else 1,
        stdout="oldsha" if after_exists else "",
    )


def _add_delete_branch_verify(
    fake: FakeRunner,
    *,
    branch_name: str = "old-docs",
    current_branch: str,
) -> None:
    fake.add(["git", "rev-parse", branch_name], stdout="oldsha")
    fake.add(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout=current_branch)
    fake.add(
        ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
        stdout=_status_clean(),
    )
    fake.add(["git", "merge-base", "--is-ancestor", branch_name, "origin/main"])
    fake.add(["git", "cherry", "-v", "origin/main", branch_name], stdout="")


def _add_pr_merge(
    fake: FakeRunner,
    *,
    number: int,
    before: dict,
    after: dict,
) -> None:
    _add_pr_view(fake, number, before)
    fake.add(
        ["gh", "pr", "merge", str(number), "--squash", "--repo", "owner/repo"],
        cwd=None,
        stdout="Merged pull request",
    )
    _add_pr_view(fake, number, after)


def _add_pr_ready(
    fake: FakeRunner,
    *,
    number: int,
    before: dict,
    after: dict,
) -> None:
    _add_pr_view(fake, number, before)
    fake.add(
        ["gh", "pr", "ready", str(number), "--repo", "owner/repo"],
        cwd=None,
        stdout="Pull request is ready",
    )
    _add_pr_view(fake, number, after)


def _add_pr_view(fake: FakeRunner, number: int, payload: dict) -> None:
    fake.add(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            "owner/repo",
            "--json",
            "title,url,headRefName,baseRefName,isDraft,mergeStateStatus,"
            "reviewDecision,statusCheckRollup,state,files,additions,deletions",
        ],
        cwd=None,
        stdout=json.dumps(payload),
    )


def _pr_view(
    *,
    number: int,
    state: str = "OPEN",
    is_draft: bool = False,
    files: list[str] | None = None,
) -> dict:
    return {
        "title": "Docs cleanup",
        "url": f"https://github.com/owner/repo/pull/{number}",
        "headRefName": "docs-cleanup",
        "baseRefName": "main",
        "isDraft": is_draft,
        "mergeStateStatus": "CLEAN",
        "reviewDecision": None,
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        "state": state,
        "files": [{"path": path} for path in (files or ["README.md"])],
        "additions": 3,
        "deletions": 1,
    }


def _executor(
    policy: dict,
    ledger_path: Path,
    *,
    mode: str,
    runner: FakeRunner,
) -> ActionExecutor:
    return ActionExecutor(
        policy=AutomationPolicy(**policy),
        ledger=AuditLedger(ledger_path, clock=_fixed_clock),
        mode=mode,
        runner=runner,
        repo_paths={"repo": REPO_PATH},
        config=_config(),
    )


def _config() -> ScannerConfig:
    return ScannerConfig(high_risk_patterns=["auth", "secret", "sync", "schema", "workflow"])


def _decision(raw: dict) -> AutomationDecision:
    return AutomationDecision(
        disposition=raw["disposition"],
        category=raw["category"],
        title=raw["title"],
        reason=raw["reason"],
        recommended_action=raw["recommended_action"],
        evidence=tuple(raw.get("evidence", [])),
    )


def _load_cases(name: str) -> list[dict]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)["cases"]


def _ledger_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _fixed_clock() -> datetime:
    return datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)


def _status_behind() -> str:
    return "## main...origin/main [behind 2]"


def _status_clean() -> str:
    return "## main...origin/main"


def _key(args: Sequence[str], cwd: Path | None) -> tuple[str | None, tuple[str, ...]]:
    return (str(cwd) if cwd else None, tuple(args))


def _is_mutating(args: list[str]) -> bool:
    return (
        args[:3] == ["git", "branch", "-d"]
        or args == ["git", "pull", "--ff-only"]
        or args[:3] == ["gh", "pr", "merge"]
        or args[:3] == ["gh", "pr", "ready"]
    )


if __name__ == "__main__":
    unittest.main()
