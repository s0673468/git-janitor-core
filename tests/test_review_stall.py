"""Convergence detection for repeated provider reviews.

Each review is bounded by one continuous timeout, but nothing bounded the number
of *rounds*. SampleApp#476 accumulated 21 receipts, every one blocking, with 2-4
findings per round and no downward trend — while the code demonstrably improved
each round. A review process that never returns a clean verdict is not
measuring quality, and that is visible in the receipt series long before a human
notices.

These tests pin the stall rule: consecutive blocking rounds whose finding count
never decreases means stop and escalate, rather than spend another review.
"""

from __future__ import annotations

import unittest

from git_janitor.review_stall import (
    STALL_ROUND_THRESHOLD,
    ReviewRound,
    _trailing_blocking_run,
    describe_stall,
    detect_stall,
)


def _round(count: int, *, blocking: bool = True, head: str = "a" * 40) -> ReviewRound:
    return ReviewRound(head=head, blocking=blocking, finding_count=count)


class TestDetectStall(unittest.TestCase):
    def test_no_history_is_not_a_stall(self):
        self.assertFalse(detect_stall(()))

    def test_a_run_without_a_prior_baseline_is_not_a_stall(self):
        history = tuple(
            _round(3, head=str(i) * 40) for i in range(STALL_ROUND_THRESHOLD)
        )
        self.assertFalse(detect_stall(history))

    def test_flat_blocking_rounds_are_a_stall(self):
        history = tuple(
            _round(3, head=f"{i:040d}") for i in range(STALL_ROUND_THRESHOLD + 1)
        )
        self.assertTrue(detect_stall(history))

    def test_rising_finding_counts_are_a_stall(self):
        history = (
            _round(2, head="1" * 40),
            _round(3, head="2" * 40),
            _round(4, head="3" * 40),
            _round(5, head="4" * 40),
        )
        self.assertTrue(detect_stall(history))

    def test_a_new_low_breaks_the_stall(self):
        """Convergence is the signal, not volume."""
        history = (
            _round(4, head="1" * 40),
            _round(4, head="2" * 40),
            _round(4, head="3" * 40),
            _round(1, head="4" * 40),
        )
        self.assertFalse(detect_stall(history))

    def test_wobble_without_progress_still_stalls(self):
        """Real series are noisy; only a genuine new low counts as progress."""
        history = (
            _round(2, head="1" * 40),
            _round(4, head="2" * 40),
            _round(3, head="3" * 40),
            _round(4, head="4" * 40),
            _round(2, head="5" * 40),
        )
        self.assertTrue(detect_stall(history))

    def test_a_clean_round_breaks_the_stall(self):
        history = (
            _round(3, head="1" * 40),
            _round(3, head="2" * 40),
            _round(3, head="3" * 40),
            _round(3, head="4" * 40),
            _round(0, blocking=False, head="5" * 40),
            _round(3, head="6" * 40),
        )
        self.assertFalse(detect_stall(history))

    def test_only_the_trailing_run_counts(self):
        """An early rough patch must not condemn a PR that later converged."""
        history = (
            _round(5, head="1" * 40),
            _round(5, head="2" * 40),
            _round(5, head="3" * 40),
            _round(2, head="4" * 40),
            _round(1, head="5" * 40),
        )
        self.assertFalse(detect_stall(history))

    def test_repeat_rounds_on_one_head_do_not_count_as_progress(self):
        """Two receipts for the same head are one round, not two."""
        history = (
            _round(3, head="1" * 40),
            _round(3, head="1" * 40),
            _round(3, head="1" * 40),
        )
        self.assertFalse(detect_stall(history))

    def test_a_repeated_head_keeps_the_latest_count(self):
        """Re-reviewing one head must update its count, not keep the first.

        Raised in review on PR #64: keeping the first count would let a head
        first recorded with few findings hide a later, worse re-run on the same
        SHA, understating the trailing run and delaying exit 5.
        """
        history = (
            _round(1, head="1" * 40),
            _round(9, head="1" * 40),
        )
        collapsed = _trailing_blocking_run(history)
        self.assertEqual([entry.finding_count for entry in collapsed], [9])

    def test_a_worse_re_review_on_one_head_can_trigger_the_stall(self):
        history = (
            _round(2, head="1" * 40),
            _round(3, head="2" * 40),
            _round(3, head="3" * 40),
            _round(1, head="4" * 40),
            _round(3, head="4" * 40),
        )
        self.assertTrue(detect_stall(history))

    def test_oscillating_count_series_stalls(self):
        counts = [4, 3, 4, 4, 2, 3, 3, 3, 3, 2, 3, 2, 3, 3, 2, 2, 2, 4, 3, 3, 2]
        history = tuple(
            _round(count, head=f"{index:040d}") for index, count in enumerate(counts)
        )
        self.assertTrue(detect_stall(history))

    def test_it_would_have_fired_early(self):
        """The point of the rule is to escalate at round ~6, not round 21."""
        counts = [4, 3, 4, 4, 2, 3, 3, 3, 3, 2]
        history = tuple(
            _round(count, head=f"{index:040d}") for index, count in enumerate(counts)
        )
        first = next(
            (
                index
                for index in range(1, len(history) + 1)
                if detect_stall(history[:index])
            ),
            None,
        )
        self.assertIsNotNone(first, "the rule must fire on this series")
        # Rounds 1-8 were genuinely productive here; the degradation set in
        # afterwards. Firing at 8 saves thirteen rounds without cutting off a
        # reviewer that is still adding value.
        self.assertLessEqual(first, 8)


class TestDescribeStall(unittest.TestCase):
    def test_the_message_shows_the_series_and_the_way_out(self):
        history = tuple(_round(3, head=f"{index:040d}") for index in range(4))
        message = describe_stall(history)
        self.assertIn("4 blocking review rounds", message)
        self.assertIn("3, 3, 3, 3", message)
        self.assertIn("no new low", message)
        self.assertIn("--allow-unresolved", message)


if __name__ == "__main__":
    unittest.main()
