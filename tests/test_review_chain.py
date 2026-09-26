from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path

from git_janitor.review_chain import (
    DEFAULT_LUNA_MODEL,
    REVIEW_CONTEXT_LINES,
    REVIEW_RECEIPT_SCHEMA,
    ReviewCoverage,
    ReviewFinding,
    ReviewReport,
    ReviewRequest,
    ReviewResult,
    ReviewStatus,
    format_review_comment,
)


HEAD_SHA = "1" * 40
BASE_SHA = "2" * 40
DEFAULT_DIFF = "diff --git a/src/example.py b/src/example.py\n+raise ValueError\n"


def _coverage(
    *,
    diff: str = DEFAULT_DIFF,
    files: tuple[str, ...] = ("src/example.py",),
    status: str = "complete",
    detail: str = "",
) -> dict[str, object]:
    return {
        "status": status,
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        "files_covered": list(files),
        "context_lines": REVIEW_CONTEXT_LINES,
        "input_omitted": False,
        "input_truncated": False,
        "context_omitted": False,
        "context_truncated": False,
        "detail": detail,
    }


def _request(*, hazard: str | None = None, base_ref: str = "main") -> ReviewRequest:
    return ReviewRequest(
        repo_path=Path("/private/tmp/example-repo"),
        repository="example-org/example",
        pr_number=42,
        base_ref=base_ref,
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
        hazard=hazard,
    )


def _report(*, priority: str = "P1", path: str = "src/example.py", line: object = 17):
    return {
        "summary": "One actionable regression.",
        "coverage": _coverage(),
        "findings": [
            {
                "priority": priority,
                "path": path,
                "line": line,
                "title": "Incorrect empty-input handling",
                "body": "The new branch raises before the documented fallback can run.",
                "execution_path": "A shipped request enters the changed branch.",
                "impact": "The documented fallback cannot run.",
                "regression_test": "Exercise an empty shipped request and assert fallback.",
            }
        ],
    }


class LunaRecordingRunner:
    def __init__(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        changed_files: str = "src/example.py\n",
        diff: str = DEFAULT_DIFF,
    ) -> None:
        self.result = result
        self.changed_files = changed_files
        self.diff = diff
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.prompt: str | None = None
        self.schema: object | None = None
        self.schema_mode: int | None = None
        self.review_cwd: str | None = None

    def __call__(self, command, **kwargs):
        command = tuple(command)
        self.calls.append((command, kwargs))
        if command[:3] == ("git", "rev-parse", "--verify"):
            output = BASE_SHA if command[3].startswith("origin/") else HEAD_SHA
            return subprocess.CompletedProcess(command, 0, stdout=output + "\n", stderr="")
        if command[:3] == ("git", "diff", "--no-ext-diff"):
            output = self.changed_files if "--name-only" in command else self.diff
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        schema_path = Path(command[command.index("--output-schema") + 1])
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.schema_mode = schema_path.stat().st_mode & 0o777
        self.prompt = str(kwargs["input"])
        self.review_cwd = str(kwargs["cwd"])
        return self.result


class ReceiptTests(unittest.TestCase):
    def _coverage(self) -> ReviewCoverage:
        return ReviewCoverage(
            status="complete",
            diff_sha256=hashlib.sha256(DEFAULT_DIFF.encode()).hexdigest(),
            files_covered=("src/example.py",),
            context_lines=REVIEW_CONTEXT_LINES,
            input_omitted=False,
            input_truncated=False,
            context_omitted=False,
            context_truncated=False,
            detail="",
        )

    def _result(self, findings: tuple[ReviewFinding, ...] = ()) -> ReviewResult:
        return ReviewResult(
            status=ReviewStatus.VALID_REVIEW,
            provider="luna",
            model=DEFAULT_LUNA_MODEL,
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            base_ref="main",
            report=ReviewReport(
                summary="Review complete.",
                findings=findings,
                coverage=self._coverage(),
            ),
            elapsed_ms=1234,
            attempts=1,
        )

    def test_receipt_proves_luna_model_max_and_report_hash(self) -> None:
        result = self._result()
        comment = format_review_comment(result)
        marker = json.loads(comment.splitlines()[0].removeprefix(
            "<!-- git-janitor-review-receipt "
        ).removesuffix(" -->"))
        report_payload = {
            "summary": "Review complete.",
            "coverage": _coverage(),
            "findings": [],
        }
        canonical = json.dumps(
            report_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        self.assertEqual(marker["provider"], "luna")
        self.assertEqual(marker["model"], DEFAULT_LUNA_MODEL)
        self.assertEqual(marker["reasoning"], "max")
        self.assertEqual(marker["base"], BASE_SHA)
        self.assertEqual(marker["base_ref"], "main")
        self.assertEqual(marker["schema"], REVIEW_RECEIPT_SCHEMA)
        self.assertEqual(marker["files_covered"], ["src/example.py"])
        self.assertEqual(marker["elapsed_ms"], 1234)
        self.assertEqual(marker["report_sha256"], hashlib.sha256(canonical.encode()).hexdigest())
        self.assertIn("Luna Max code review", comment)

    def test_receipt_is_deterministic_and_only_p1_blocks(self) -> None:
        p2 = ReviewFinding(
            "P2",
            "src/example.py",
            17,
            "Regression",
            "Concrete failure.",
            "A shipped request reaches it.",
            "The request fails.",
            "Assert the shipped request succeeds.",
        )
        first = format_review_comment(self._result((p2,)))
        second = format_review_comment(self._result((p2,)))
        self.assertEqual(first, second)
        self.assertIn('"blocking":false', first.splitlines()[0])

        p1 = ReviewFinding(
            "P1",
            "src/example.py",
            17,
            "Regression",
            "Concrete failure.",
            "A shipped request reaches it.",
            "The request fails.",
            "Assert the shipped request succeeds.",
        )
        self.assertIn('"blocking":true', format_review_comment(self._result((p1,))).splitlines()[0])

    def test_legacy_receipts_can_still_be_rendered_as_evidence(self) -> None:
        for provider, model in (("grok", "grok-4.5"), ("gemini", "gemini-3.6-flash-high")):
            with self.subTest(provider=provider):
                legacy = ReviewResult(
                    status=ReviewStatus.VALID_REVIEW,
                    provider=provider,
                    model=model,
                    head_sha=HEAD_SHA,
                    report=ReviewReport(summary="Historical receipt.", findings=()),
                )
                comment = format_review_comment(legacy)
                self.assertIn(f'"provider":"{provider}"', comment.splitlines()[0])
                self.assertIn('"reasoning":"legacy"', comment.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
