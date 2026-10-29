"""Copyable investigation evidence, privacy, and metric-specific profiler handoff."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from test_privacy import SECRET, private_fixture

from src.investigation import investigation_summary
from src.report_contract import main


class InvestigationTests(unittest.TestCase):
    def test_copyable_summary_keeps_resource_interval_contributor_and_coverage(self) -> None:
        report = private_fixture()
        for sample in report["snapshots"]:
            sample["alerts"][0]["contributors"] = [{"pid": 543219, "create_time": 1700000123.5,
                                                    "name": SECRET, "delta": 10, "share": 1}]
        result = investigation_summary(report, runtime="python")
        text = result["text"]
        self.assertEqual(result["investigation_schema"], 1)
        self.assertIn("metric: private_bytes", text)
        self.assertIn("10 seconds ending +1.0s", text)
        self.assertIn("Leading contributor: process-", text)
        self.assertIn("0 of 2 live-tree samples had missing readings", text)
        self.assertIn("tracemalloc", text)
        self.assertIn("not proof of an allocation source", text)
        self.assertIn("No profiling or tracing was started", text)
        for private in (SECRET, "543219", "1700000123.5", "2023-11-14"):
            self.assertNotIn(private, text)

    def test_metric_and_runtime_change_profiler_guidance(self) -> None:
        report = private_fixture()
        for resource, expected in (("memory", "heap/VirtualAlloc"), ("handles", "handle creation/close"),
                                   ("threads", "process/thread lifetime")):
            with self.subTest(resource=resource):
                text = investigation_summary(report, resource=resource, runtime="native")["text"]
                self.assertIn(expected, text)
                self.assertIn("WPA", text)
                self.assertNotIn("tracemalloc", text)
        text = investigation_summary(report, runtime="dotnet")["text"]
        self.assertIn("dotnet-gcdump", text)
        self.assertIn("Collection triggers a GC", text)
        report["experiment"]["environment"]["os"] = "Linux"
        text = investigation_summary(report, resource="handles")["text"]
        self.assertIn("/proc/PID/fd", text)
        self.assertNotIn("WPR trace", text)

    def test_missing_retained_details_and_unknown_coverage_remain_explicit(self) -> None:
        report = private_fixture()
        report["snapshots"] = []
        text = investigation_summary(report, profile="minimal")["text"]
        self.assertIn("strongest recorded signal: HIGH", text)
        self.assertIn("no retained alert details", text)
        legacy = {"snapshots": [{"timestamp": 1, "processes": [{"pid": 123}], "totals": {}}]}
        text = investigation_summary(legacy)["text"]
        self.assertIn("Coverage: unknown", text)
        self.assertIn("detection window judged: unknown", text)
        self.assertNotIn("CLEAR", text)

    def test_recovery_and_incomplete_cleanup_qualifications_survive_copying(self) -> None:
        report = private_fixture()
        report["recovery"] = {"partial": True, "final_outcome_confirmed": False, "exact_through_cutoff": True}
        report["outcome"]["startup"] = {"state": "failed", "error": SECRET}
        report["outcome"]["cleanup"] = {"state": "incomplete", "remaining_count": 2, "resume_required": True,
                                         "errors": [SECRET]}
        for profile in ("share", "minimal"):
            with self.subTest(profile=profile):
                text = investigation_summary(report, profile=profile)["text"]
                self.assertIn("Recovered prefix: partial checkpoint evidence", text)
                self.assertIn("final target and monitoring outcomes are unconfirmed", text)
                self.assertIn("Startup outcome: failed", text)
                self.assertIn("Cleanup outcome: incomplete; 2 processes remained unresolved", text)
                self.assertIn("still require resume", text)
                self.assertNotIn(SECRET, text)

    def test_fractional_count_slopes_are_not_rounded_to_zero(self) -> None:
        report = private_fixture()
        for sample in report["snapshots"]:
            sample["alerts"][0].update(resource="handles", metric="handles", slope_per_second=0.125)
        text = investigation_summary(report, resource="handles")["text"]
        self.assertIn("0.125 counts per second", text)

    def test_cli_json_and_text_modes_both_transform_before_sharing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "private.json"
            source.write_text(json.dumps(private_fixture()), encoding="utf-8")
            for output_format in ("json", "text"):
                with self.subTest(output_format=output_format):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main(["investigate", str(source), "--runtime", "python",
                                               "--format", output_format]), 0)
                    text = output.getvalue()
                    if output_format == "json":
                        self.assertEqual(json.loads(text)["investigation_schema"], 1)
                    self.assertIn("DrainTracer investigation", text)
                    self.assertNotIn(SECRET, text)
            self.assertEqual(list(Path(temp).iterdir()), [source])


if __name__ == "__main__":
    unittest.main()
