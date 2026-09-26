"""Detect a provider review that is generating findings instead of converging.

Every review is bounded by a timeout; nothing bounded the number of rounds. On
Health#476 that produced 21 receipts, all blocking, 60 findings, 2-4 per round
with no downward trend — while each round's fixes were real and the code kept
improving. The flat finding rate is the signal: a reviewer that never clears a
PR is no longer measuring it.

The rule is deliberately about *convergence*, not volume. A PR is allowed as
many rounds as it needs so long as each one costs less than the last; it is
stopped when several consecutive rounds fail to improve. Stopping is not a
verdict on the code — it is a handoff to a human, who can merge on an
evidence-backed dismissal, split the PR, or ask for a different reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "STALL_ROUND_THRESHOLD",
    "ReviewRound",
    "describe_stall",
    "detect_stall",
]

#: Consecutive non-improving blocking rounds tolerated before escalation.
STALL_ROUND_THRESHOLD = 3


@dataclass(frozen=True)
class ReviewRound:
    """One posted review receipt."""

    head: str
    blocking: bool
    finding_count: int


def detect_stall(history: tuple[ReviewRound, ...]) -> bool:
    """True when the trailing blocking rounds show no convergence.

    The test is "no new low": the last :data:`STALL_ROUND_THRESHOLD` rounds
    contain no finding count strictly below the best already achieved earlier in
    the run. A strict monotonic test is too brittle for real data — the observed
    Health#476 series (4, 3, 4, 4, 2, 3, 3, 3, 3, 2, 3, 2, 3, 3, 2, 2, 2, 4, 3,
    3, 2) wobbles constantly and even ends on a decline, yet never improves on
    the 2 it reached at round five. Tolerating wobble while requiring genuine
    progress fires on that series at round eight instead of never.

    ``history`` is oldest-first. Repeat receipts for the same head collapse to
    one round: re-reviewing an unchanged head is not another attempt at fixing
    it. A prior baseline is required, so the earliest a stall can be reported is
    ``STALL_ROUND_THRESHOLD + 1`` rounds.
    """
    trailing = _trailing_blocking_run(history)
    if len(trailing) <= STALL_ROUND_THRESHOLD:
        return False
    counts = [entry.finding_count for entry in trailing]
    window = counts[-STALL_ROUND_THRESHOLD:]
    baseline = min(counts[:-STALL_ROUND_THRESHOLD])
    return min(window) >= baseline


def describe_stall(history: tuple[ReviewRound, ...]) -> str:
    """Operator-facing explanation of why reviewing stopped."""
    trailing = _trailing_blocking_run(history)
    series = ", ".join(str(entry.finding_count) for entry in trailing)
    return (
        f"Provider review is not converging: {len(trailing)} blocking review rounds "
        f"with no new low in the last {STALL_ROUND_THRESHOLD} ({series}).\n"
        "Refusing to spend another review. A reviewer that never clears a PR is no "
        "longer measuring it, and each further round costs a full CI cycle.\n"
        "Choose one:\n"
        "  - split the PR so the reviewed core merges and the remainder gets a "
        "fresh diff;\n"
        "  - re-run once the diff has materially shrunk;\n"
        "  - merge on an evidence-backed dismissal with "
        "`pr-shepherd merge --allow-unresolved`."
    )


def _trailing_blocking_run(
    history: tuple[ReviewRound, ...],
) -> tuple[ReviewRound, ...]:
    deduped: list[ReviewRound] = []
    for entry in history:
        if deduped and deduped[-1].head == entry.head:
            deduped[-1] = entry
            continue
        deduped.append(entry)

    trailing: list[ReviewRound] = []
    for entry in reversed(deduped):
        if not entry.blocking:
            break
        trailing.append(entry)
    trailing.reverse()
    return tuple(trailing)
