"""Finding-location verification for provider reviews.

A review that cites code which does not exist cannot be acted on, and must not
block a merge. Observed on SampleApp#476: across 21 review rounds the later ones
cited symbols with zero occurrences in the tree (``_reconcileSelectedHistory``,
``_opQueue``, ``_isRicherOrEqual``, ``_queuePending``, ``_historyCache``) and
line numbers past end-of-file (``food_log_repository.dart:119`` in a 55-line
file). The schema validator accepted all of them: it only checks that the path
is a changed file and the line is a positive integer.

These tests pin the extra, deterministic checks made against the reviewed head.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_janitor.finding_verification import (
    FindingVerdict,
    blocking_findings,
    verify_findings,
)
from git_janitor.review_chain import ReviewFinding, ReviewReport


def _finding(
    *,
    priority: str = "P1",
    path: str = "lib/thing.dart",
    line: int = 3,
    title: str = "Something",
    body: str = "A problem.",
) -> ReviewFinding:
    return ReviewFinding(
        priority=priority, path=path, line=line, title=title, body=body
    )


class _Tree:
    """A throwaway checkout for verification."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        target = self.root / "lib" / "thing.dart"
        target.parent.mkdir(parents=True)
        target.write_text(
            "class Thing {\n"
            "  void _existingMethod() {}\n"
            "  int value = 1;\n"
            "}\n",
            encoding="utf-8",
        )
        return self.root

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


class TestLineWithinFile(unittest.TestCase):
    def test_line_inside_the_file_is_actionable(self):
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(summary="s", findings=(_finding(line=2),)), root=root
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.ACTIONABLE)

    def test_line_past_end_of_file_is_unverifiable(self):
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(summary="s", findings=(_finding(line=119),)), root=root
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.UNVERIFIABLE)
        self.assertIn("4 lines", verdicts[0].reason)

    def test_a_missing_file_without_exact_base_evidence_is_unverifiable(self):
        """Raised in review on PR #64.

        Absence at the head is not proof of drift: the finding may be about a
        file this PR deletes, and "you removed the auth check" is exactly the
        P1 that must keep blocking.
        """
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(
                    summary="s", findings=(_finding(path="lib/absent.dart"),)
                ),
                root=root,
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.UNVERIFIABLE)

    def test_a_deletion_finding_naming_the_removed_symbol_still_blocks(self):
        """Raised in review on PR #64.

        A deletion finding names the member it removed, and that symbol is gone
        from the tree by definition — so falling through to the symbol check
        marked exactly this class unverifiable and cleared the merge gate.
        """
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(
                    summary="s",
                    findings=(
                        _finding(
                            priority="P1",
                            path="lib/auth.dart",
                            line=12,
                            body="This deletion removes `_validateSession`.",
                        ),
                    ),
                ),
                root=root,
                base_text_reader=lambda path: (
                    "\n".join(["// context"] * 11 + ["void _validateSession() {}"]) + "\n"
                    if path == "lib/auth.dart"
                    else None
                ),
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.ACTIONABLE)
        self.assertEqual(len(blocking_findings(verdicts)), 1)

    def test_a_deleted_file_finding_still_blocks(self):
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(
                    summary="s",
                    findings=(
                        _finding(
                            priority="P1",
                            path="lib/removed_auth.dart",
                            line=42,
                            body="This deletion drops the auth check.",
                        ),
                    ),
                ),
                root=root,
                base_text_reader=lambda path: (
                    "\n".join(["// auth"] * 42) + "\n"
                    if path == "lib/removed_auth.dart"
                    else None
                ),
            )
        self.assertEqual(len(blocking_findings(verdicts)), 1)


class TestCitedSymbols(unittest.TestCase):
    def test_a_cited_private_symbol_that_exists_is_actionable(self):
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(
                    summary="s",
                    findings=(_finding(body="`_existingMethod` returns early."),),
                ),
                root=root,
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.ACTIONABLE)

    def test_a_cited_private_symbol_that_does_not_exist_is_unverifiable(self):
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(
                    summary="s",
                    findings=(_finding(body="`_reconcileSelectedHistory` races."),),
                ),
                root=root,
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.UNVERIFIABLE)
        self.assertIn("_reconcileSelectedHistory", verdicts[0].reason)

    def test_private_symbols_in_grounding_evidence_are_verified(self):
        with _Tree() as root:
            finding = _finding(body="The branch returns early.")
            finding = ReviewFinding(
                priority=finding.priority,
                path=finding.path,
                line=finding.line,
                title=finding.title,
                body=finding.body,
                execution_path="A request calls `_ghostEntryPoint`.",
                impact="The request fails.",
                regression_test="Assert the request succeeds.",
            )
            verdicts = verify_findings(
                ReviewReport(summary="s", findings=(finding,)), root=root
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.UNVERIFIABLE)
        self.assertIn("_ghostEntryPoint", verdicts[0].reason)

    def test_prose_and_public_names_are_not_treated_as_symbols(self):
        """Only leading-underscore identifiers are checked.

        Backticks carry plenty of prose, filenames, columns and commands; a
        false 'unverifiable' would silently drop a real finding, so the rule is
        deliberately narrow.
        """
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(
                    summary="s",
                    findings=(
                        _finding(
                            body=(
                                "`main` calls `saveToday()` and writes "
                                "`updated_at` via `gh pr view`."
                            )
                        ),
                    ),
                ),
                root=root,
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.ACTIONABLE)

    def test_a_symbol_defined_in_another_file_still_counts(self):
        with _Tree() as root:
            (root / "lib" / "other.dart").write_text(
                "void _sharedHelper() {}\n", encoding="utf-8"
            )
            verdicts = verify_findings(
                ReviewReport(
                    summary="s", findings=(_finding(body="`_sharedHelper` is wrong."),)
                ),
                root=root,
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.ACTIONABLE)


class TestBlockingSet(unittest.TestCase):
    def test_only_actionable_p1_p2_block(self):
        with _Tree() as root:
            report = ReviewReport(
                summary="s",
                findings=(
                    _finding(priority="P1", body="`_ghostSymbol` explodes."),
                    _finding(priority="P3", line=2, body="Tidy this."),
                ),
            )
            verdicts = verify_findings(report, root=root)
        self.assertEqual(blocking_findings(verdicts), ())

    def test_one_actionable_p2_still_blocks(self):
        with _Tree() as root:
            report = ReviewReport(
                summary="s",
                findings=(
                    _finding(priority="P1", body="`_ghostSymbol` explodes."),
                    _finding(priority="P2", line=2, body="Real problem here."),
                ),
            )
            verdicts = verify_findings(report, root=root)
        blocking = blocking_findings(verdicts)
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0].finding.priority, "P2")

    def test_an_empty_report_blocks_nothing(self):
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(summary="s", findings=()), root=root
            )
        self.assertEqual(verdicts, ())
        self.assertEqual(blocking_findings(verdicts), ())


class TestVerificationIsFailSafe(unittest.TestCase):
    def test_an_unreadable_root_leaves_findings_unverifiable(self):
        """Verification must never invent a dismissal.

        If the tree cannot be inspected the finding stays blocking; only a
        positive proof of non-existence downgrades it.
        """
        verdicts = verify_findings(
            ReviewReport(summary="s", findings=(_finding(),)),
            root=Path("/nonexistent/checkout"),
        )
        self.assertEqual(verdicts[0].state, FindingVerdict.UNVERIFIABLE)
        self.assertEqual(len(blocking_findings(verdicts)), 0)

    def test_an_absolute_path_is_unverifiable(self):
        """Raised in review on PR #64.

        The schema rejects absolute and directory paths before verification is
        ever reached (a finding path must be a safe relative POSIX path that is
        also a changed file), so this case is unreachable in production. If one
        did arrive, it must stay actionable rather than be silently dropped —
        the fail-safe direction.
        """
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(
                    summary="s",
                    findings=(_finding(path="/etc/passwd", line=99999),),
                ),
                root=root,
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.UNVERIFIABLE)
        self.assertEqual(len(blocking_findings(verdicts)), 0)

    def test_a_directory_path_is_unverifiable(self):
        with _Tree() as root:
            verdicts = verify_findings(
                ReviewReport(summary="s", findings=(_finding(path="lib"),)), root=root
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.UNVERIFIABLE)

    def test_an_unsearchable_file_prevents_proving_a_symbol_absent(self):
        """Raised in review on PR #64.

        Returning False from the symbol search is read as positive proof of
        absence. A candidate file that could not be searched — too large,
        unreadable, not UTF-8 — must force the finding to stay actionable
        instead.
        """
        with _Tree() as root:
            blob = root / "lib" / "generated.dart"
            blob.write_bytes(b"\xff\xfe" * 8)
            verdicts = verify_findings(
                ReviewReport(
                    summary="s",
                    findings=(_finding(body="`_maybeInGeneratedCode` is wrong."),),
                ),
                root=root,
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.ACTIONABLE)

    def test_unreadable_locations_cannot_become_blocking_findings(self):
        """The module's central invariant, raised in review on PR #64.

        Verification only ever *removes* blocking status, so any read failure
        must leave a finding actionable. This makes every filesystem read raise
        and asserts nothing is dismissed. (The review described this against a
        git-based implementation — `run_git`, `GitCommandError`,
        `_path_exists_at_head`, `cat-file` — none of which exist here; this
        module never shells out. The hazard is pinned anyway.)
        """
        with _Tree() as root:
            report = ReviewReport(
                summary="s",
                findings=(
                    _finding(priority="P1", line=99999),
                    _finding(priority="P2", body="`_ghostSymbol` explodes."),
                ),
            )
            # Only the read path: patching Path.stat as well breaks is_dir()
            # itself on some Python versions, which tests the mock, not the
            # module.
            with patch.object(Path, "read_text", side_effect=OSError("disk gone")):
                verdicts = verify_findings(report, root=root)

        self.assertTrue(
            all(v.state is FindingVerdict.UNVERIFIABLE for v in verdicts),
            "a read failure must never authorize a blocking location claim",
        )
        self.assertEqual(len(blocking_findings(verdicts)), 0)

    def test_a_binary_file_does_not_crash_verification(self):
        with _Tree() as root:
            (root / "lib" / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
            verdicts = verify_findings(
                ReviewReport(
                    summary="s", findings=(_finding(path="lib/blob.bin", line=1),)
                ),
                root=root,
            )
        self.assertEqual(verdicts[0].state, FindingVerdict.UNVERIFIABLE)


if __name__ == "__main__":
    unittest.main()
