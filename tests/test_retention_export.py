"""The monitor's own dense exports must round-trip through its bounded public reader."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import make_alert, make_console, make_process, make_snapshot

from src.headless import HeadlessRunner
from src.report import prepare_report_export
from src.report_contract import read_report


class RetentionExportTests(unittest.TestCase):
    def test_dense_export_bounds_both_formats_without_losing_exact_peak_or_signal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runner = HeadlessRunner("retention-shape.exe", console=make_console(), report_dir=folder)
            ordinary = tuple(make_process(100 + index) for index in range(31))
            spike = (make_process(100, private=800 * 1024**2), *ordinary[1:])
            for index in range(1440):
                runner._accept_snapshot(make_snapshot(timestamp=1000 + index * 0.5,
                    processes=spike if index == 701 else ordinary,
                    alerts=(make_alert(),) if index == 701 else ()))
            original = runner.build_report()
            with mock.patch("src.session.write_html_report", return_value=Path(folder) / "report.html") as html:
                path = runner.export_log()
            saved = read_report(path)
            bounded = html.call_args.args[0]
            self.assertEqual(saved, json.loads(json.dumps(bounded)))
            self.assertLess(len(saved["snapshots"]), 1440)
            self.assertGreater(saved["snapshot_stride"], 1)
            self.assertEqual(saved["snapshots"][0]["timestamp"], 1000)
            self.assertEqual(saved["snapshots"][-1]["timestamp"], 1000 + 1439 * 0.5)
            self.assertEqual(saved["session_facts"]["signals"], original["session_facts"]["signals"])
            self.assertEqual(saved["session_facts"]["peaks"], original["session_facts"]["peaks"])
            self.assertEqual(saved["sample_count"], 1440)
            self.assertEqual(saved["session_facts"]["chart"]["seen"], 1440)
            self.assertEqual(saved["session_facts"]["chart"]["stored"], len(saved["snapshots"]))
            self.assertEqual(len(runner._snapshot_history), 1440)
            self.assertIn("chart thinned", runner.chart_thinning_text())

    def test_non_chart_oversize_fails_before_writing_or_omitting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "reports"
            runner = HeadlessRunner("worker.exe", console=make_console(), report_dir=destination)
            with mock.patch("src.report_contract.MAX_REPORT_BYTES", 64), \
                    self.assertRaisesRegex(ValueError, "non-chart evidence cannot be omitted"):
                runner.export_log()
            self.assertFalse(destination.exists())

    def test_finite_normalization_still_preserves_the_original_dictionary(self) -> None:
        original = {"report_id": "test", "snapshots": [], "optional": float("nan")}
        bounded = prepare_report_export(original)
        self.assertIsNone(bounded["optional"])
        self.assertIsNot(original, bounded)


if __name__ == "__main__":
    unittest.main()
