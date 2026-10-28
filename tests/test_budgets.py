"""Absolute resource budgets: parsing and checks against run peaks."""

from __future__ import annotations

import unittest

from src.budgets import check_budgets, parse_budget

MB = 1024 * 1024


class BudgetTests(unittest.TestCase):
    def test_parsing_uses_sizes_for_private_bytes_and_counts_otherwise(self) -> None:
        self.assertEqual(parse_budget("private_bytes", "500MB"), 500 * MB)
        self.assertEqual(parse_budget("private_bytes", "1.5MB"), 1.5 * MB)
        self.assertEqual(parse_budget("handles", "2000"), 2000)
        for metric, value in (("handles", "0"), ("threads", "-3"), ("private_bytes", "lots")):
            with self.assertRaises(ValueError):
                parse_budget(metric, value)

    def test_count_budgets_require_positive_whole_numbers(self) -> None:
        for metric in ("handles", "threads"):
            for value in (1, 2.0, "3", "4.0"):
                with self.subTest(metric=metric, value=value):
                    self.assertEqual(parse_budget(metric, value), float(value))
            for value in (1.5, "2.25", ".5"):
                with self.subTest(metric=metric, value=value):
                    with self.assertRaisesRegex(ValueError, "whole number"):
                        parse_budget(metric, value)
            for value in (0, -1, -1.5, True, float("inf"), float("nan")):
                with self.subTest(metric=metric, value=value):
                    with self.assertRaises(ValueError):
                        parse_budget(metric, value)

    def test_stable_but_excessive_peaks_exceed_their_budget(self) -> None:
        checks = check_budgets(
            {"private_bytes": 500 * MB, "handles": 2000, "threads": 50},
            {"private_bytes": 612 * MB, "handles": 1500, "threads": 0},
        )
        self.assertEqual([check.metric for check in checks], ["private_bytes", "handles", "threads"])
        private, handles, threads = checks
        self.assertTrue(private.exceeded)
        self.assertEqual(private.describe(), "private bytes peak 612.0 MB over the 500.0 MB budget")
        self.assertFalse(handles.exceeded)
        self.assertIn("within the 2,000 budget", handles.describe())
        # Zero means nothing was measured, not that the budget was met.
        self.assertFalse(threads.measured)
        self.assertIn("not checked", threads.describe())
        self.assertEqual(private.to_dict()["exceeded"], True)

    def test_unset_budgets_are_skipped(self) -> None:
        self.assertEqual(check_budgets({}, {"handles": 10}), ())


if __name__ == "__main__":
    unittest.main()
