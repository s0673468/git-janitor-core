from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from git_janitor import pr_shepherd
from git_janitor.models import CommandResult
from git_janitor.review_chain import (
    REVIEW_CONTEXT_LINES,
    ReviewCoverage,
    ReviewFinding,
    ReviewReport,
    ReviewResult,
    ReviewStatus,
    format_review_comment,
)


HEAD_SHA = "1" * 40
BASE_SHA = "2" * 40
MERGE_SHA = "4" * 40
REPO = "example-org/example"
REPO_PATH = Path("/private/tmp/example")
AGENT_OPS_FIXTURES = Path(__file__).parent / "fixtures" / "agent_ops"
REVIEW_DIFF = "diff --git a/src/example.py b/src/example.py\n+line\n"
REVIEW_DIFF_SHA256 = hashlib.sha256(REVIEW_DIFF.encode("utf-8")).hexdigest()


def _review_coverage() -> ReviewCoverage:
    return ReviewCoverage(
        status="complete",
        diff_sha256=REVIEW_DIFF_SHA256,
        files_covered=("src/example.py",),
        context_lines=REVIEW_CONTEXT_LINES,
        input_omitted=False,
        input_truncated=False,
        context_omitted=False,
        context_truncated=False,
        detail="",
    )


def _review_result(
    *, priority: str | None = None, provider: str = "luna"
) -> ReviewResult:
    findings = (
        (
            ReviewFinding(
                priority=priority,
                path="src/example.py",
                line=17,
                title="Regression",
                body="The fallback is skipped.",
                execution_path="A shipped request reaches the changed fallback.",
                impact="The request fails instead of using the fallback.",
                regression_test="Exercise the shipped request and assert the fallback.",
            ),
        )
        if priority
        else ()
    )
    return ReviewResult(
        status=ReviewStatus.VALID_REVIEW,
        provider=provider,
        model={
            "luna": "gpt-5.6-luna",
            "grok": "grok-4.5",
            "gemini": "gemini-3.6-flash-high",
        }[provider],
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA if provider == "luna" else "",
        base_ref="main" if provider == "luna" else "",
        report=ReviewReport(
            summary="Review complete.",
            findings=findings,
            coverage=(
                _review_coverage()
                if provider == "luna"
                else None
            ),
        ),
        elapsed_ms=1234,
        attempts=1,
    )


def _grounded_finding(priority: str, line: int, title: str) -> ReviewFinding:
    return ReviewFinding(
        priority=priority,
        path="src/example.py",
        line=line,
        title=title,
        body="The changed branch fails.",
        execution_path="A shipped request reaches the changed branch.",
        impact="The shipped request fails.",
        regression_test="Exercise the shipped request and assert success.",
    )


class ReviewRunner:
    def __init__(
        self,
        *,
        comments: list[dict[str, object]] | None = None,
        checks_green: bool = True,
        local_head: str = HEAD_SHA,
        post_ok: bool = True,
        comments_ok: bool = True,
        move_head_after_review: bool = False,
        dirty_worktree: bool = False,
        readback_failures_after_post: int = 0,
        move_head_after_post: bool = False,
        rest_comments_not_found: bool = False,
        rest_post_not_found: bool = False,
        graphql_comments_ok: bool = True,
        graphql_post_ok: bool = True,
    ) -> None:
        self.comments = list(comments or [])
        self.checks_green = checks_green
        self.local_head = local_head
        self.post_ok = post_ok
        self.comments_ok = comments_ok
        self.move_head_after_review = move_head_after_review
        self.dirty_worktree = dirty_worktree
        self.readback_failures_after_post = readback_failures_after_post
        self.move_head_after_post = move_head_after_post
        self.rest_comments_not_found = rest_comments_not_found
        self.rest_post_not_found = rest_post_not_found
        self.graphql_comments_ok = graphql_comments_ok
        self.graphql_post_ok = graphql_post_ok
        self.posted = False
        self.pr_view_calls = 0
        self.commands: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        _cwd: Path | None,
        _timeout: int,
    ) -> CommandResult:
        self.commands.append(args)
        if args[:3] == ["gh", "pr", "checks"]:
            if not self.checks_green:
                return CommandResult(args, 1, "", "required checks are not green")
            return CommandResult(
                args,
                0,
                json.dumps([{"bucket": "pass", "state": "SUCCESS"}]),
                "",
            )
        if args[:3] == ["gh", "pr", "view"]:
            self.pr_view_calls += 1
            moved = (
                self.move_head_after_review and self.pr_view_calls > 1
            ) or (self.move_head_after_post and self.posted)
            head = "3" * 40 if moved else HEAD_SHA
            return CommandResult(
                args,
                0,
                json.dumps(
                    {
                        "id": "PR_example42",
                        "headRefOid": head,
                        "baseRefName": "main",
                        "baseRefOid": BASE_SHA,
                    }
                ),
                "",
            )
        if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return CommandResult(args, 0, str(REPO_PATH), "")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return CommandResult(args, 0, self.local_head, "")
        if args[:3] == ["git", "status", "--porcelain=v1"]:
            return CommandResult(
                args,
                0,
                " M src/example.py\n" if self.dirty_worktree else "",
                "",
            )
        if args[:4] == ["git", "diff", "--no-ext-diff", "--name-only"]:
            return CommandResult(args, 0, "src/example.py", "")
        if args[:3] == ["git", "diff", "--no-ext-diff"] and any(
            argument.startswith("--unified=") for argument in args
        ):
            return CommandResult(args, 0, REVIEW_DIFF, "")
        if args[:2] == ["gh", "api"] and "--slurp" in args:
            if self.rest_comments_not_found:
                return CommandResult(args, 1, "", "gh: Not Found (HTTP 404)")
            if self.posted and self.readback_failures_after_post > 0:
                self.readback_failures_after_post -= 1
                return CommandResult(args, 1, "", "transient comment readback failure")
            if not self.comments_ok:
                return CommandResult(args, 1, "", "comment readback failed")
            return CommandResult(args, 0, json.dumps([self.comments]), "")
        if args[:3] == ["gh", "pr", "comment"]:
            if self.rest_post_not_found:
                return CommandResult(args, 1, "", "gh: Not Found (HTTP 404)")
            if not self.post_ok:
                return CommandResult(args, 1, "", "comment post failed")
            body = args[args.index("--body") + 1]
            self.comments.append(
                {
                    "body": body,
                    "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN},
                }
            )
            self.posted = True
            return CommandResult(args, 0, "https://example.invalid/comment", "")
        if args[:3] == ["gh", "api", "graphql"]:
            query = args[args.index("-f") + 1]
            if "addComment" in query:
                if not self.graphql_post_ok:
                    return CommandResult(args, 1, "", "GraphQL comment post failed")
                body = next(
                    value.removeprefix("body=")
                    for value in args
                    if value.startswith("body=")
                )
                self.comments.append(
                    {
                        "body": body,
                        "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN},
                    }
                )
                self.posted = True
                return CommandResult(
                    args,
                    0,
                    json.dumps(
                        {
                            "data": {
                                "addComment": {
                                    "commentEdge": {
                                        "node": {
                                            "body": body,
                                            "author": {
                                                "login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    ),
                    "",
                )
            if not self.graphql_comments_ok:
                return CommandResult(args, 1, "", "GraphQL comment read failed")
            nodes = [
                {
                    "body": comment["body"],
                    "author": comment.get("user"),
                }
                for comment in self.comments
            ]
            return CommandResult(
                args,
                0,
                json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "comments": {
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": nodes,
                                    }
                                }
                            }
                        }
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected command: {args}")


class FixForwardMergeRunner:
    def __init__(
        self,
        *,
        descendant: bool,
        post_ok: bool,
        readback_ok: bool,
        strict_base: bool = True,
        merge_parent: str = BASE_SHA,
    ) -> None:
        self.descendant = descendant
        self.post_ok = post_ok
        self.readback_ok = readback_ok
        self.strict_base = strict_base
        self.merge_parent = merge_parent
        self.commands: list[list[str]] = []
        self.posted_body: str | None = None

    def __call__(
        self,
        args: list[str],
        _cwd: Path | None,
        _timeout: int,
    ) -> CommandResult:
        self.commands.append(args)
        if args[:2] == ["gh", "api"] and "/compare/" in args[2]:
            status = "ahead" if self.descendant else "diverged"
            return CommandResult(args, 0, json.dumps({"status": status}), "")
        if args[:2] == ["gh", "api"] and args[2].endswith(
            "/branches/main/protection"
        ):
            return CommandResult(
                args,
                0,
                json.dumps(
                    {"required_status_checks": {"strict": self.strict_base}}
                ),
                "",
            )
        if args[:3] == ["gh", "pr", "checks"]:
            return CommandResult(
                args,
                0,
                json.dumps([{"bucket": "pass", "state": "SUCCESS"}]),
                "",
            )
        if args[:3] == ["gh", "pr", "merge"]:
            return CommandResult(args, 0, "auto-merge enabled", "")
        if args[:4] == ["gh", "api", "--method", "PUT"]:
            return CommandResult(
                args,
                0,
                json.dumps({"merged": True, "sha": MERGE_SHA}),
                "",
            )
        if args[:2] == ["gh", "api"] and args[2].endswith(
            f"/git/commits/{MERGE_SHA}"
        ):
            return CommandResult(
                args,
                0,
                json.dumps({"parents": [{"sha": self.merge_parent}]}),
                "",
            )
        if args == ["gh", "api", f"repos/{REPO}/pulls/42"]:
            return CommandResult(
                args,
                0,
                json.dumps(
                    {
                        "merged": True,
                        "head": {
                            "ref": "codex/luna-review",
                            "sha": HEAD_SHA,
                            "repo": {"full_name": REPO},
                        },
                    }
                ),
                "",
            )
        if args[:3] == ["gh", "pr", "comment"]:
            self.posted_body = args[args.index("--body") + 1]
            if not self.post_ok:
                return CommandResult(args, 1, "", "comment post failed")
            return CommandResult(args, 0, "https://example.invalid/comment", "")
        if args[:2] == ["gh", "api"] and "--slurp" in args:
            if self.posted_body is None:
                return CommandResult(args, 0, "[[]]", "")
            login = (
                pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN
                if self.readback_ok
                else "untrusted-contributor"
            )
            return CommandResult(
                args,
                0,
                json.dumps(
                    [[{"body": self.posted_body, "user": {"login": login}}]]
                ),
                "",
            )
        raise AssertionError(f"unexpected command: {args}")


def _reviewed_merge_commands(commands: list[list[str]]) -> list[list[str]]:
    return [
        command
        for command in commands
        if command[:4] == ["gh", "api", "--method", "PUT"]
        and "/pulls/" in command[4]
        and command[4].endswith("/merge")
    ]


class _ReviewHarness:
    """Shared harness. Deliberately not a TestCase: subclassing one would
    re-run every parent test under the child suite."""

    def setUp(self) -> None:
        self._lock_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._lock_temp.cleanup)
        self.lock_dir = Path(self._lock_temp.name)
        self._repo_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._repo_temp.cleanup)
        global REPO_PATH
        previous_repo_path = REPO_PATH
        REPO_PATH = Path(self._repo_temp.name)
        self.addCleanup(lambda: globals().__setitem__("REPO_PATH", previous_repo_path))
        source = REPO_PATH / "src" / "example.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "\n".join(["def shipped_request():", "    return 'fallback'", *(["# context"] * 18)])
            + "\n",
            encoding="utf-8",
        )

    def run_review(
        self,
        result: ReviewResult,
        *,
        runner: ReviewRunner | None = None,
        high_risk: bool = True,
        dirty_after_review: bool = False,
        force_review: bool = False,
        new_scope: str | None = None,
        hazard: str | None = None,
        allow_pending_ci: bool = False,
    ) -> tuple[int, ReviewRunner, list[object], list[object], str, str]:
        command_runner = runner or ReviewRunner()
        adapter_calls: list[object] = []

        def adapter(request, **_kwargs):
            adapter_calls.append(request)
            if dirty_after_review:
                command_runner.dirty_worktree = True
            return result

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = pr_shepherd.run_provider_review(
                repo=REPO,
                pr=42,
                high_risk=high_risk,
                allow_pending_ci=allow_pending_ci,
                timeout_seconds=600,
                repo_path=REPO_PATH,
                lock_dir=self.lock_dir,
                runner=command_runner,
                luna_adapter=adapter,
                readback_poll_interval=0,
                force_review=force_review,
                new_scope=new_scope,
                hazard=hazard,
            )
        return (
            code,
            command_runner,
            adapter_calls,
            [],
            stdout.getvalue(),
            stderr.getvalue(),
        )




class RetiredReviewRouteTests(unittest.TestCase):
    def test_cli_exposes_no_direct_codex_wait(self) -> None:
        parser = pr_shepherd.build_parser()
        commands = parser._subparsers._group_actions[0].choices
        self.assertNotIn("wait", commands)

    def test_legacy_codex_request_entry_points_fail_closed(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            direct = pr_shepherd.run_direct_codex_wait(
                repo=REPO,
                pr=42,
                timeout_seconds=600,
                poll_interval=0,
                nudge=True,
                high_risk=True,
            )
            wait = pr_shepherd.wait_for_review(
                repo=REPO,
                pr=42,
                timeout_seconds=600,
                poll_interval=0,
                nudge=True,
                high_risk=True,
            )
            request = pr_shepherd.request_review_once(repo=REPO, pr=42)

        self.assertEqual(direct, 2)
        self.assertEqual(wait, 2)
        self.assertEqual(request, "error")
        self.assertIn("disabled", stderr.getvalue())


class ReceiptParsingTests(unittest.TestCase):
    def test_receipt_base_ref_validation_accepts_legal_git_names(self) -> None:
        for base_ref in (
            "release/v1+hotfix",
            "foo@bar",
            "-maintenance",
            "foo=bar",
            "foo,bar",
            "foo%bar",
            "lançamento/produção",
        ):
            with self.subTest(base_ref=base_ref):
                self.assertTrue(pr_shepherd._valid_receipt_base_ref(base_ref))

    def test_old_receipt_without_priority_counts_trusts_stored_blocking(self) -> None:
        fixture = json.loads(
            (AGENT_OPS_FIXTURES / "pr_shepherd_one_pass_review.json").read_text(
                encoding="utf-8"
            )
        )
        marker = fixture["old_receipt"]
        body = (
            pr_shepherd.GROK_REVIEW_RECEIPT_PREFIX
            + json.dumps(marker, sort_keys=True, separators=(",", ":"))
            + " -->"
        )

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            runner=ReviewRunner(
                comments=[
                    {
                        "body": body,
                        "user": {
                            "login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN
                        },
                    }
                ]
            ),
        )

        self.assertIsNotNone(receipt)
        self.assertTrue(receipt["blocking"])
        self.assertNotIn("p1", receipt)

    def test_trusted_exact_head_gemini_receipt_is_accepted(self) -> None:
        body = format_review_comment(_review_result(provider="gemini"))

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            runner=ReviewRunner(
                comments=[
                    {
                        "body": body,
                        "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN},
                    }
                ]
            ),
        )

        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["provider"], "gemini")
        self.assertFalse(receipt["blocking"])

    def test_earlier_luna_receipt_remains_readable_but_cannot_authorize(self) -> None:
        comment = format_review_comment(_review_result())
        marker = pr_shepherd._receipt_marker_payload(comment.splitlines()[0])
        for key in (
            "schema",
            "diff_sha256",
            "files_covered",
            "coverage_status",
            "context_lines",
            "input_omitted",
            "input_truncated",
            "context_omitted",
            "context_truncated",
            "elapsed_ms",
            "attempts",
        ):
            marker.pop(key)
        historical = (
            pr_shepherd.GROK_REVIEW_RECEIPT_PREFIX
            + json.dumps(marker, sort_keys=True, separators=(",", ":"))
            + " -->\nHistorical Luna receipt."
        )
        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            required_base_sha=BASE_SHA,
            required_base_ref="main",
            require_current_if_reviewed=True,
            latest_only=True,
            runner=ReviewRunner(
                comments=[
                    {
                        "body": historical,
                        "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN},
                    }
                ]
            ),
        )
        self.assertTrue(receipt["_stale"])
        self.assertTrue(receipt["_requires_fresh_luna"])

    def test_luna_receipt_must_match_the_actual_exact_diff(self) -> None:
        comment = format_review_comment(_review_result())
        mismatched = comment.replace(REVIEW_DIFF_SHA256, "0" * 64, 1)
        runner = ReviewRunner(
            comments=[
                {
                    "body": mismatched,
                    "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN},
                }
            ]
        )

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            required_base_sha=BASE_SHA,
            required_base_ref="main",
            require_current_if_reviewed=True,
            latest_only=True,
            validate_diff=True,
            repo_path=REPO_PATH,
            runner=runner,
        )

        self.assertTrue(receipt["_stale"])
        self.assertTrue(receipt["_requires_fresh_luna"])

    def test_luna_receipt_file_order_must_match_the_actual_diff(self) -> None:
        marker = pr_shepherd._receipt_marker_payload(
            format_review_comment(_review_result()).splitlines()[0]
        )
        marker["files_covered"] = ["src/second.py", "src/example.py"]

        self.assertFalse(
            pr_shepherd._receipt_authorizes_luna(
                marker,
                base_sha=BASE_SHA,
                base_ref="main",
                expected_diff_sha256=REVIEW_DIFF_SHA256,
                expected_files_covered=("src/example.py", "src/second.py"),
            )
        )

    def test_descendant_carry_forward_validates_the_reviewed_head_diff(self) -> None:
        reviewed_head = "9" * 40
        reviewed_diff = "diff --git a/src/example.py b/src/example.py\n+reviewed\n"
        reviewed = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model="gpt-5.6-luna",
            head_sha=reviewed_head,
            base_sha=BASE_SHA,
            base_ref="main",
            report=ReviewReport(
                summary="Review complete.",
                findings=(),
                coverage=ReviewCoverage(
                    status="complete",
                    diff_sha256=hashlib.sha256(reviewed_diff.encode("utf-8")).hexdigest(),
                    files_covered=("src/example.py",),
                    context_lines=REVIEW_CONTEXT_LINES,
                    input_omitted=False,
                    input_truncated=False,
                    context_omitted=False,
                    context_truncated=False,
                    detail="",
                ),
            ),
            elapsed_ms=1234,
            attempts=1,
        )
        comments = ReviewRunner(
            comments=[
                {
                    "body": format_review_comment(reviewed),
                    "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN},
                }
            ]
        )

        def reviewed_head_runner(args, cwd, timeout):
            if args[:4] == ["git", "diff", "--no-ext-diff", "--name-only"]:
                self.assertEqual(args[-1], f"{BASE_SHA}...{reviewed_head}")
                return CommandResult(args, 0, "src/example.py\n", "")
            if args[:3] == ["git", "diff", "--no-ext-diff"]:
                self.assertEqual(args[-1], f"{BASE_SHA}...{reviewed_head}")
                return CommandResult(args, 0, reviewed_diff, "")
            return comments(args, cwd, timeout)

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            required_base_sha=BASE_SHA,
            required_base_ref="main",
            sticky_prior_blocking=True,
            require_current_if_reviewed=True,
            latest_only=True,
            validate_diff=True,
            repo_path=REPO_PATH,
            runner=reviewed_head_runner,
        )

        self.assertTrue(receipt["_stale"])
        self.assertNotIn("_requires_fresh_luna", receipt)

    def test_luna_gate_requires_fresh_review_for_non_authorizing_receipts(self) -> None:
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}
        wrong_model = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model="gpt-5.6-luna-preview",
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            base_ref="main",
            report=ReviewReport(
                summary="Review complete.", findings=(), coverage=_review_coverage()
            ),
        )
        wrong_base = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model="gpt-5.6-luna",
            head_sha=HEAD_SHA,
            base_sha="3" * 40,
            base_ref="main",
            report=ReviewReport(
                summary="Review complete.", findings=(), coverage=_review_coverage()
            ),
        )
        wrong_base_ref = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model="gpt-5.6-luna",
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            base_ref="release",
            report=ReviewReport(
                summary="Review complete.", findings=(), coverage=_review_coverage()
            ),
        )
        comments = [
            {
                "body": format_review_comment(_review_result(provider="gemini")),
                "user": trusted,
            },
            {"body": format_review_comment(wrong_model), "user": trusted},
            {"body": format_review_comment(wrong_base), "user": trusted},
            {"body": format_review_comment(wrong_base_ref), "user": trusted},
        ]

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            required_base_sha=BASE_SHA,
            required_base_ref="main",
            require_current_if_reviewed=True,
            runner=ReviewRunner(comments=comments),
        )

        self.assertIsNotNone(receipt)
        self.assertTrue(receipt["_stale"])
        self.assertTrue(receipt["_requires_fresh_luna"])

    def test_newer_non_authorizing_receipt_invalidates_older_luna_receipt(self) -> None:
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}
        wrong_model = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model="gpt-5.6-luna-preview",
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            base_ref="main",
            report=ReviewReport(
                summary="Review complete.", findings=(), coverage=_review_coverage()
            ),
        )

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            required_base_sha=BASE_SHA,
            required_base_ref="main",
            require_current_if_reviewed=True,
            latest_only=True,
            runner=ReviewRunner(
                comments=[
                    {
                        "body": format_review_comment(_review_result()),
                        "user": trusted,
                    },
                    {"body": format_review_comment(wrong_model), "user": trusted},
                ]
            ),
        )

        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["model"], "gpt-5.6-luna-preview")
        self.assertTrue(receipt["_stale"])
        self.assertTrue(receipt["_requires_fresh_luna"])

    def test_luna_history_ignores_receipts_for_an_old_base(self) -> None:
        old_base = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model="gpt-5.6-luna",
            head_sha=HEAD_SHA,
            base_sha="3" * 40,
            base_ref="main",
            report=ReviewReport(
                summary="Review complete.", findings=(), coverage=_review_coverage()
            ),
        )
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}

        history = pr_shepherd.review_round_history(
            repo=REPO,
            pr=42,
            luna_only=True,
            required_base_sha=BASE_SHA,
            required_base_ref="main",
            runner=ReviewRunner(
                comments=[{"body": format_review_comment(old_base), "user": trusted}]
            ),
        )

        self.assertEqual(history, ())

    def test_luna_history_ignores_receipts_for_an_old_base_branch(self) -> None:
        old_base_ref = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model="gpt-5.6-luna",
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            base_ref="release",
            report=ReviewReport(
                summary="Review complete.", findings=(), coverage=_review_coverage()
            ),
        )
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}

        history = pr_shepherd.review_round_history(
            repo=REPO,
            pr=42,
            luna_only=True,
            required_base_sha=BASE_SHA,
            required_base_ref="main",
            runner=ReviewRunner(
                comments=[
                    {"body": format_review_comment(old_base_ref), "user": trusted}
                ]
            ),
        )

        self.assertEqual(history, ())

    def test_newer_invalid_receipt_reopens_luna_review_history(self) -> None:
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}
        wrong_model = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model="gpt-5.6-luna-preview",
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            base_ref="main",
            report=ReviewReport(
                summary="Review complete.", findings=(), coverage=_review_coverage()
            ),
        )

        history = pr_shepherd.review_round_history(
            repo=REPO,
            pr=42,
            luna_only=True,
            required_base_sha=BASE_SHA,
            required_base_ref="main",
            runner=ReviewRunner(
                comments=[
                    {"body": format_review_comment(_review_result()), "user": trusted},
                    {"body": format_review_comment(wrong_model), "user": trusted},
                ]
            ),
        )

        self.assertEqual(history, ())

    def test_malformed_receipt_fails_closed(self) -> None:
        body = '<!-- git-janitor-review-receipt {"provider":"grok"} -->'

        with self.assertRaisesRegex(RuntimeError, "malformed provider review receipt"):
            pr_shepherd.grok_review_receipt_state(
                repo=REPO,
                pr=42,
                head_sha=HEAD_SHA,
                runner=ReviewRunner(
                    comments=[
                        {
                            "body": body,
                            "user": {
                                "login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN
                            },
                        }
                    ]
                ),
            )

    def test_untrusted_receipt_author_is_ignored(self) -> None:
        body = format_review_comment(_review_result(priority="P1"))

        self.assertIsNone(
            pr_shepherd.grok_review_receipt_state(
                repo=REPO,
                pr=42,
                head_sha=HEAD_SHA,
                runner=ReviewRunner(
                    comments=[
                        {
                            "body": body,
                            "user": {"login": "untrusted-contributor"},
                        }
                    ]
                ),
            )
        )

    def test_blocking_receipt_is_sticky_for_the_exact_head(self) -> None:
        blocking = format_review_comment(_review_result(priority="P1"))
        clean = format_review_comment(_review_result())
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            runner=ReviewRunner(
                comments=[
                    {"body": blocking, "user": trusted},
                    {"body": clean, "user": trusted},
                ]
            ),
        )

        self.assertIsNotNone(receipt)
        self.assertTrue(receipt["blocking"])

    def test_latest_only_returns_the_latest_exact_head_receipt(self) -> None:
        blocking = format_review_comment(_review_result(priority="P1"))
        clean = format_review_comment(_review_result())
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            latest_only=True,
            runner=ReviewRunner(
                comments=[
                    {"body": blocking, "user": trusted},
                    {"body": clean, "user": trusted},
                ]
            ),
        )

        self.assertIsNotNone(receipt)
        self.assertFalse(receipt["blocking"])

    def test_prior_blocking_receipt_stays_sticky_for_merge_until_current_review(self) -> None:
        prior = format_review_comment(
            ReviewResult(
                status=ReviewStatus.VALID_REVIEW,
                provider="grok",
                model="grok-4.5",
                head_sha="9" * 40,
                report=ReviewReport(
                    summary="Blocking.",
                    findings=(
                        ReviewFinding(
                            priority="P1",
                            path="src/example.py",
                            line=17,
                            title="Regression",
                            body="Still unresolved.",
                        ),
                    ),
                ),
            )
        )
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            sticky_prior_blocking=True,
            runner=ReviewRunner(comments=[{"body": prior, "user": trusted}]),
        )

        self.assertIsNotNone(receipt)
        self.assertTrue(receipt["blocking"])
        self.assertEqual(receipt["head"], "9" * 40)

    def test_current_clean_receipt_clears_prior_blocking_receipt_for_merge(self) -> None:
        prior_result = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="grok",
            model="grok-4.5",
            head_sha="9" * 40,
            report=ReviewReport(
                summary="Blocking.",
                findings=(
                    ReviewFinding(
                        priority="P1",
                        path="src/example.py",
                        line=17,
                        title="Regression",
                        body="Still unresolved.",
                    ),
                ),
            ),
        )
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            sticky_prior_blocking=True,
            runner=ReviewRunner(
                comments=[
                    {"body": format_review_comment(prior_result), "user": trusted},
                    {
                        "body": format_review_comment(_review_result()),
                        "user": trusted,
                    },
                ]
            ),
        )

        self.assertIsNotNone(receipt)
        self.assertFalse(receipt["blocking"])
        self.assertEqual(receipt["head"], HEAD_SHA)

    def test_prior_clean_receipt_requires_a_current_head_review_before_merge(self) -> None:
        prior = format_review_comment(
            ReviewResult(
                status=ReviewStatus.VALID_REVIEW,
                provider="grok",
                model="grok-4.5",
                head_sha="9" * 40,
                report=ReviewReport(summary="Clean.", findings=()),
            )
        )
        trusted = {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}

        receipt = pr_shepherd.grok_review_receipt_state(
            repo=REPO,
            pr=42,
            head_sha=HEAD_SHA,
            require_current_if_reviewed=True,
            runner=ReviewRunner(comments=[{"body": prior, "user": trusted}]),
        )

        self.assertIsNotNone(receipt)
        self.assertTrue(receipt["_stale"])
        self.assertFalse(receipt["blocking"])

    def test_no_receipt_is_distinct_from_readback_failure(self) -> None:
        self.assertIsNone(
            pr_shepherd.grok_review_receipt_state(
                repo=REPO,
                pr=42,
                head_sha=HEAD_SHA,
                runner=ReviewRunner(),
            )
        )
        with self.assertRaisesRegex(RuntimeError, "review receipt"):
            pr_shepherd.grok_review_receipt_state(
                repo=REPO,
                pr=42,
                head_sha=HEAD_SHA,
                runner=ReviewRunner(comments_ok=False),
            )


class ReviewOutcomeMeasurementTests(unittest.TestCase):
    def _review_comment(self) -> str:
        result = ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model="gpt-5.6-luna",
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            base_ref="main",
            report=ReviewReport(
                summary="Two findings.",
                findings=(
                    _grounded_finding("P1", 1, "Critical"),
                    _grounded_finding("P2", 2, "Important"),
                ),
                coverage=_review_coverage(),
            ),
            elapsed_ms=54_321,
            attempts=1,
        )
        return format_review_comment(result)

    def test_disposition_receipt_binds_report_and_records_escape(self) -> None:
        runner = ReviewRunner(
            comments=[
                {
                    "body": self._review_comment(),
                    "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN},
                }
            ]
        )

        code = pr_shepherd.record_review_outcome(
            repo=REPO,
            pr=42,
            confirmed=(1,),
            dismissed=(2,),
            unverifiable=(),
            post_merge_escape_refs=(f"{REPO}#99",),
            runner=runner,
        )

        self.assertEqual(code, 0)
        metrics = pr_shepherd.review_metrics(repo=REPO, pr=42, runner=runner)
        self.assertEqual(metrics["elapsed_ms"], 54_321)
        disposition = metrics["disposition"]
        self.assertEqual(disposition["confirmed"], [1])
        self.assertEqual(disposition["dismissed"], [2])
        self.assertEqual(disposition["post_merge_escape_refs"], [f"{REPO}#99"])

    def test_escape_update_carries_forward_dispositions(self) -> None:
        runner = ReviewRunner(
            comments=[
                {
                    "body": self._review_comment(),
                    "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN},
                }
            ]
        )
        self.assertEqual(
            pr_shepherd.record_review_outcome(
                repo=REPO,
                pr=42,
                confirmed=(1,),
                dismissed=(),
                unverifiable=(2,),
                runner=runner,
            ),
            0,
        )
        self.assertEqual(
            pr_shepherd.record_review_outcome(
                repo=REPO,
                pr=42,
                confirmed=None,
                dismissed=None,
                unverifiable=None,
                post_merge_escape_refs=(f"{REPO}#101",),
                runner=runner,
            ),
            0,
        )
        metrics = pr_shepherd.review_metrics(repo=REPO, pr=42, runner=runner)
        disposition = metrics["disposition"]
        self.assertEqual(disposition["confirmed"], [1])
        self.assertEqual(disposition["unverifiable"], [2])
        self.assertEqual(disposition["post_merge_escape_refs"], [f"{REPO}#101"])

    def test_partial_disposition_is_rejected(self) -> None:
        runner = ReviewRunner(
            comments=[
                {
                    "body": self._review_comment(),
                    "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN},
                }
            ]
        )
        code = pr_shepherd.record_review_outcome(
            repo=REPO,
            pr=42,
            confirmed=(1,),
            dismissed=(),
            unverifiable=(),
            runner=runner,
        )
        self.assertEqual(code, 2)
        self.assertFalse(runner.posted)






def _receipt_comment(
    *, head: str, blocking: bool, findings: int, unverifiable: int = 0
) -> dict[str, object]:
    marker = {
        "provider": "luna",
        "model": "gpt-5.6-luna",
        "reasoning": "max",
        "base": BASE_SHA,
        "base_ref": "main",
        "schema": "luna-review/v2",
        "diff_sha256": "a" * 64,
        "files_covered": ["src/example.py"],
        "coverage_status": "complete",
        "context_lines": REVIEW_CONTEXT_LINES,
        "input_omitted": False,
        "input_truncated": False,
        "context_omitted": False,
        "context_truncated": False,
        "elapsed_ms": 1234,
        "attempts": 1,
        "head": head,
        "report_sha256": "0" * 64,
        "outcome": "findings" if findings else "clean",
        "blocking": blocking,
        "findings": findings,
        "unverifiable": unverifiable,
    }
    body = "\n".join(
        [
            pr_shepherd.GROK_REVIEW_RECEIPT_PREFIX
            + json.dumps(marker, sort_keys=True, separators=(",", ":"))
            + " -->",
            "### Luna Max code review — `gpt-5.6-luna`",
            "",
            "Summary.",
            "",
        ]
        + [f"{i}. **P2 — Finding {i}**" for i in range(1, findings + 1)]
    )
    return {"body": body, "user": {"login": pr_shepherd.GROK_REVIEW_RECEIPT_LOGIN}}








if __name__ == "__main__":
    unittest.main()
