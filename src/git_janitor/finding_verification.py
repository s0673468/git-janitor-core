"""Deterministic verification of provider-review finding locations.

The review schema already requires a finding's path to be a changed file and its
line to be a positive integer. Neither check catches a review that has drifted
off the actual code: a line past end-of-file, or a body citing a symbol that
does not exist anywhere in the tree.

That is not hypothetical. Across the 21 review rounds on Health#476 the later
ones cited ``_reconcileSelectedHistory``, ``_opQueue``, ``_isRicherOrEqual``,
``_queuePending`` and ``_historyCache`` — all with zero occurrences — and
``food_log_repository.dart:119`` in a 55-line file. Every one passed schema
validation and blocked the merge, and each had to be dismissed by hand.

A finding whose cited location cannot be resolved at the reviewed head or exact
base is recorded as ``unverifiable``: it stays visible in the receipt but does
not become a blocking claim. A deleted-file finding remains actionable only
when its line and cited private symbols can be checked against the exact base.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - types only, avoids a runtime import cycle
    from .review_chain import ReviewFinding, ReviewReport

__all__ = [
    "FindingVerdict",
    "VerifiedFinding",
    "blocking_findings",
    "unverifiable_findings",
    "verify_findings",
]

BLOCKING_PRIORITIES = frozenset({"P1", "P2"})

# Only leading-underscore identifiers are treated as citations of real code.
# Backticks in a review body also carry prose, filenames, column names, shell
# commands and public API names, any of which could legitimately be absent from
# the diff; a private member cited by name is the one case where absence is
# strong evidence the reviewer is describing code that is not there.
_PRIVATE_SYMBOL_RE = re.compile(r"`(_[A-Za-z][A-Za-z0-9_]{2,})`")

# Directories that never contain reviewable source, skipped when searching for a
# cited symbol so a large checkout does not make verification slow.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".dart_tool",
        "node_modules",
        "build",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        "Pods",
        "DerivedData",
    }
)

_MAX_SEARCH_BYTES = 2_000_000


class FindingVerdict(str, Enum):
    """Whether a finding's cited location could be resolved at the reviewed head."""

    ACTIONABLE = "actionable"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class VerifiedFinding:
    finding: "ReviewFinding"
    state: FindingVerdict
    reason: str = ""


def verify_findings(
    report: "ReviewReport",
    *,
    root: Path,
    base_text_reader: Callable[[str], str | None] | None = None,
) -> tuple[VerifiedFinding, ...]:
    """Classify every finding in ``report`` against the checkout at ``root``."""
    if not report.findings:
        return ()
    if not root.is_dir():
        return tuple(
            VerifiedFinding(
                finding=finding,
                state=FindingVerdict.UNVERIFIABLE,
                reason="the reviewed-head checkout could not be inspected",
            )
            for finding in report.findings
        )

    symbol_cache: dict[str, bool] = {}
    verdicts: list[VerifiedFinding] = []
    for finding in report.findings:
        reason = _unverifiable_reason(
            finding,
            root=root,
            symbol_cache=symbol_cache,
            base_text_reader=base_text_reader,
        )
        verdicts.append(
            VerifiedFinding(
                finding=finding,
                state=(
                    FindingVerdict.ACTIONABLE
                    if reason is None
                    else FindingVerdict.UNVERIFIABLE
                ),
                reason=reason or "",
            )
        )
    return tuple(verdicts)


def blocking_findings(
    verdicts: tuple[VerifiedFinding, ...],
) -> tuple[VerifiedFinding, ...]:
    """Actionable P1/P2 findings — the ones that may hold a merge."""
    return tuple(
        verdict
        for verdict in verdicts
        if verdict.state is FindingVerdict.ACTIONABLE
        and verdict.finding.priority in BLOCKING_PRIORITIES
    )


def unverifiable_findings(
    verdicts: tuple[VerifiedFinding, ...],
) -> tuple[VerifiedFinding, ...]:
    return tuple(
        verdict for verdict in verdicts if verdict.state is FindingVerdict.UNVERIFIABLE
    )


def _unverifiable_reason(
    finding: "ReviewFinding",
    *,
    root: Path,
    symbol_cache: dict[str, bool],
    base_text_reader: Callable[[str], str | None] | None,
) -> str | None:
    target = _safe_join(root, finding.path)
    if target is None:
        return f"{finding.path} is not a safe location in the reviewed tree"
    if target.is_dir():
        return f"{finding.path} resolves to a directory, not an exact file location"
    elif not target.exists():
        base_text = base_text_reader(finding.path) if base_text_reader is not None else None
        if base_text is None:
            return (
                f"{finding.path}:{finding.line} is absent at the reviewed head and "
                "could not be verified against the exact base"
            )
        base_line_count = len(base_text.splitlines()) if base_text else 0
        if finding.line > base_line_count:
            return (
                f"{finding.path}:{finding.line} is past end of file "
                f"({base_line_count} lines at the exact base)"
            )
        for symbol in _cited_private_symbols(finding):
            if symbol not in base_text:
                return (
                    f"cites `{symbol}`, which has no occurrence in the deleted "
                    "file at the exact base"
                )
        return None
    else:
        line_count = _line_count(target)
        if line_count is None:
            return f"{finding.path}:{finding.line} could not be read as source text"
        if finding.line > line_count:
            return (
                f"{finding.path}:{finding.line} is past end of file "
                f"({line_count} lines at the reviewed head)"
            )

    for symbol in _cited_private_symbols(finding):
        present = symbol_cache.get(symbol)
        if present is None:
            present = _symbol_present(symbol, root=root)
            symbol_cache[symbol] = present
        if not present:
            return f"cites `{symbol}`, which has no occurrence at the reviewed head"
    return None


def _cited_private_symbols(finding: "ReviewFinding") -> tuple[str, ...]:
    evidence = "\n".join(
        (
            finding.body,
            finding.execution_path,
            finding.impact,
            finding.regression_test,
        )
    )
    return tuple(dict.fromkeys(_PRIVATE_SYMBOL_RE.findall(evidence)))


def _safe_join(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _line_count(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Unreadable or binary: no positive evidence either way.
        return None
    if not text:
        return 0
    return len(text.splitlines())


def _symbol_present(symbol: str, *, root: Path) -> bool:
    """True when ``symbol`` occurs, or when the search could not be exhaustive.

    Returning False is read by the caller as positive proof of absence, so any
    candidate file that could not be searched — too large, unreadable, not
    UTF-8 — must force True instead. A private symbol living only in a large or
    generated source file would otherwise be "proved" absent and silently
    unblock a real finding.
    """
    skipped = False
    for path in _source_files(root):
        try:
            if path.stat().st_size > _MAX_SEARCH_BYTES:
                skipped = True
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped = True
            continue
        if symbol in text:
            return True
    return skipped


def _source_files(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    stack.append(entry)
                continue
            if entry.is_file():
                yield entry
