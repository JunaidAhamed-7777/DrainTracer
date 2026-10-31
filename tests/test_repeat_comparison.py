"""Observed variability, every-run comparability, bounded groups, and CLI reuse."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from helpers import make_console
from test_report_diff import _experiment, _run

from src.diff_command import EXIT_ERROR, EXIT_NOT_COMPARABLE, EXIT_OVER_BUDGET, run_diff
from src.repeat_comparison import compare_repeated_reports
from src.report_diff import DiffError, compare_reports, load_report


class RepeatedComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)

    def reports(self, prefix, steps, **extra):
        reports = []
        for index, step in enumerate(steps):
            path = self.folder / f"{prefix}{index}.json"
            path.write_text(json.dumps(_run(step, report_id=path.stem, **extra)), encoding="utf-8")
            reports.append(load_report(path))
        return reports

    def test_noisy_favorable_pair_does_not_become_repeatable_improvement(self) -> None:
        old = self.reports("a", [1, 8])
        new = self.reports("b", [2, 3])
        self.assertEqual(compare_reports(old[1], new[0]).verdict, "improved")
        grouped = compare_repeated_reports(old, new)
        self.assertEqual(grouped.verdict, "inconclusive")
        self.assertEqual(grouped.resources[0].verdict, "overlap")
        self.assertEqual(grouped.resources[0].drift_before.count, 2)

    def test_separated_ranges_improve_and_larger_new_ranges_regress(self) -> None:
        old, new = self.reports("a", [4, 5]), self.reports("b", [0.5, 1])
        result = compare_repeated_reports(old, new)
        self.assertEqual(result.verdict, "improved")
        self.assertLess(result.resources[0].drift_after.maximum, result.resources[0].drift_before.minimum)
        self.assertEqual(compare_repeated_reports(new, old).verdict, "regressed")
        self.assertIn("not confidence intervals", result.to_dict()["rule"])

    def test_every_pair_including_nonreference_runs_preserves_caveats(self) -> None:
        old = []
        for index, operations in enumerate((100, 109, 91)):
            old += self.reports(f"a{index}", [1], experiment=_experiment(operations=operations))
        new = self.reports("b", [1, 1], experiment=_experiment(operations=100))
        result = compare_repeated_reports(old, new)
        self.assertTrue(result.blocked)
        self.assertTrue(any(item["before"] == "A2" and item["after"] == "A3"
                            and "operation" in item["message"] for item in result.caveats))

    def test_incompatible_member_is_not_hidden_by_group_averages(self) -> None:
        old, new = self.reports("a", [4, 5]), self.reports("b", [0, 1])
        data = dict(new[1].data)
        data["target_path"] = "other.exe"
        new[1].path.write_text(json.dumps(data), encoding="utf-8")
        new[1] = load_report(new[1].path)
        result = compare_repeated_reports(old, new)
        self.assertEqual(result.verdict, "not comparable")
        self.assertTrue(any(item["after"] == "B2" for item in result.caveats))

    def test_requires_multiple_distinct_bounded_runs(self) -> None:
        old, new = self.reports("a", [4, 5]), self.reports("b", [0, 1])
        for before, after in ((old[:1], new), (old * 11, new), ([old[0], old[0]], new)):
            with self.subTest(before=len(before), after=len(after)), self.assertRaises(DiffError):
                compare_repeated_reports(before, after)

    def test_reused_evidence_across_sides_cannot_appear_independent(self) -> None:
        old, new = self.reports("a", [4, 5]), self.reports("b", [0, 1])
        with self.assertRaisesRegex(DiffError, "independent runs on both sides"):
            compare_repeated_reports(old, [old[0], new[0]])
        copied = dict(new[0].data)
        copied["report_id"] = old[0].data["report_id"]
        with self.assertRaisesRegex(DiffError, "report ID"):
            compare_repeated_reports(old, [replace(new[0], data=copied), new[1]])

    def test_missing_drift_remains_unknown_and_blocks_the_group(self) -> None:
        old, new = self.reports("a", [4, 5]), self.reports("b", [0, 1])
        resources = dict(new[0].summary.resources)
        resources["memory"] = replace(resources["memory"], drift_per_second=None)
        new[0] = replace(new[0], summary=replace(new[0].summary, resources=resources))
        result = compare_repeated_reports(old, new)
        self.assertEqual(result.verdict, "insufficient coverage")
        self.assertEqual(result.resources[0].verdict, "unknown")
        self.assertEqual(result.resources[0].drift_after.count, 1)

    def test_cli_caps_combined_bytes_before_parsing_any_report(self) -> None:
        old, new = self.reports("a", [4, 5]), self.reports("b", [0, 1])
        console = make_console()
        with mock.patch("src.diff_command.MAX_GROUP_BYTES", 1), \
                mock.patch("src.diff_command.load_report") as read:
            code = run_diff([str(old[0].path), str(new[0].path), "--before-run", str(old[1].path),
                             "--after-run", str(new[1].path)], console)
        self.assertEqual(code, EXIT_ERROR)
        read.assert_not_called()
        self.assertIn("128 MiB", console.file.getvalue())

    def test_cli_json_exposes_spread_and_checks_largest_after_peak(self) -> None:
        old, new = self.reports("a", [4, 5]), self.reports("b", [0, 1])
        console = make_console(80, 24)
        code = run_diff([str(old[0].path), str(new[0].path), "--before-run", str(old[1].path),
                         "--after-run", str(new[1].path), "--budget-handles", "150", "--json"], console)
        self.assertEqual(code, EXIT_OVER_BUDGET)
        data = json.loads(console.file.getvalue())
        self.assertEqual(data["mode"], "repeated")
        self.assertEqual(data["resources"]["memory"]["drift_before"]["count"], 2)
        self.assertTrue(data["over_budget"])

    def test_cli_rejects_incompatible_repetitions_and_prints_the_rule(self) -> None:
        old = self.reports("a", [4, 5])
        new = self.reports("b", [0, 1], target_path="other.exe")
        console = make_console(80, 24)
        code = run_diff([str(old[0].path), str(new[0].path), "--before-run", str(old[1].path),
                         "--after-run", str(new[1].path)], console)
        self.assertEqual(code, EXIT_NOT_COMPARABLE)
        output = console.file.getvalue()
        self.assertIn("observed min/max", output)
        self.assertIn("median", output)
        self.assertLess(output.index("COMPARABILITY"), output.index("MEMORY:"))


if __name__ == "__main__":
    unittest.main()
