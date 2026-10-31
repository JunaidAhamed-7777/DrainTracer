"""Retained process rankings describe each run without pairing identities or causes."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from helpers import make_alert, make_console, make_snapshot, render_text
from test_report_diff import _run

from src.analysis import PROCESS_SUMMARY_SCAN_LIMIT, process_attribution, summarize
from src.dashboard import DashboardApp
from src.diff_command import run_diff
from src.events import GrowthContributor, ProcessSummary
from src.report import process_summary_to_dict, snapshot_to_dict
from src.report_diff import compare_reports, load_report


def lifetime(pid=100, created=10.0, before=1, after=9, **changes):
    value = process_summary_to_dict(ProcessSummary(pid, created, "worker", None, 1000, 1010, 3,
        {"private_bytes": before}, {"private_bytes": after}, {"private_bytes": max(before, after)}))
    value.update(changes)
    return value


def alerted(*contributors, severity="high", stamp=1010, paused=False):
    return {"timestamp": stamp, "monitoring_paused": paused, "alerts": [{"resource": "memory",
        "severity": severity, "window_seconds": 10, "growth_from_new_processes": 60,
        "growth_within_existing": 20, "contributors": list(contributors)}]}


class ProcessAttributionTests(unittest.TestCase):
    def test_lifetime_reuses_first_to_last_instead_of_untrusted_growth_field(self):
        report = {"processes": {"live": [lifetime(after=9, growth={"private_bytes": 99999}),
                                           lifetime(200, after=20), lifetime(300, before=10, after=1)]}}
        rows = process_attribution(report, "memory")["lifetime"]["rows"]
        self.assertEqual([(row["pid"], row["delta"]) for row in rows], [(200, 19), (100, 8)])
        self.assertTrue(process_attribution(report, "memory")["lifetime"]["startup_included"])

    def test_new_capacity_can_be_in_alert_when_lifetime_growth_is_zero(self):
        report = {"processes": {"live": [lifetime(before=12, after=12)]}, "snapshots": [alerted(
            {"pid": 100, "create_time": 10, "name": "worker", "delta": 60, "new_process": True, "exited": False})]}
        result = process_attribution(report, "memory")
        self.assertEqual(result["lifetime"]["rows"], [])
        self.assertTrue(result["alert"]["rows"][0]["new_process"])
        self.assertEqual(result["alert"]["growth_from_new_processes"], 60)

    def test_strongest_then_latest_alert_wins_and_paused_repeats_do_not_replace_it(self):
        def contribution(pid):
            return {"pid": pid, "create_time": 1, "delta": pid}
        report = {"snapshots": [alerted(contribution(1), stamp=1), alerted(contribution(2), stamp=2),
            alerted(contribution(3), severity="low", stamp=3),
            alerted(contribution(4), severity="critical", stamp=4, paused=True)]}
        result = process_attribution(report, "memory")["alert"]
        self.assertEqual((result["severity"], result["timestamp"], result["rows"][0]["pid"]), ("high", 2, 2))

    def test_duplicate_identities_are_omitted_and_same_pid_distinct_creations_are_separate(self):
        report = {"processes": {"live": [lifetime(), lifetime(after=90), lifetime(created=11),
                                           lifetime(200, created=None)]}}
        result = process_attribution(report, "memory")["lifetime"]
        self.assertEqual(result["duplicate_identity_rows_omitted"], 2)
        self.assertEqual([(row["pid"], row["create_time"]) for row in result["rows"]], [(100, 11), (200, None)])
        self.assertEqual(result["unknown_identity_rows"], 1)
        self.assertFalse(result["rows"][1]["identity_known"])

    def test_scan_top_and_bad_numeric_bounds_are_explicit(self):
        values = [lifetime(pid=index + 1) for index in range(PROCESS_SUMMARY_SCAN_LIMIT + 7)]
        result = process_attribution({"processes": {"live": values}}, "memory")["lifetime"]
        self.assertEqual((result["summaries_examined"], result["summaries_omitted"], len(result["rows"])),
                         (PROCESS_SUMMARY_SCAN_LIMIT, 7, 5))
        malformed = [lifetime(after=float("inf")), lifetime(samples=True), lifetime(first={"private_bytes": True}),
                     lifetime(create_time="unknown"), lifetime(last_seen=999)]
        result = process_attribution({"processes": {"live": malformed}}, "memory")["lifetime"]
        self.assertEqual((result["invalid_summaries"], result["rows"]), (5, []))
        for limit in (True, 0, 21):
            with self.assertRaises(ValueError):
                process_attribution({}, "memory", limit=limit)

    def test_missing_attribution_is_unavailable_and_summary_is_detached(self):
        result = process_attribution({}, "memory")
        self.assertFalse(result["lifetime"]["available"])
        self.assertFalse(result["alert"]["available"])
        report = {"processes": {"live": [lifetime()]}, "snapshots": [snapshot_to_dict(make_snapshot())]}
        before = copy.deepcopy(report)
        summary = summarize(report)
        self.assertEqual(report, before)
        report["processes"]["live"][0]["name"] = "changed"
        self.assertEqual(summary.process_attribution["memory"]["lifetime"]["rows"][0]["name"], "worker")

    def test_comparison_keeps_independent_sides_and_rankings_do_not_change_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("a.json", "b.json")]
            for path, delta in zip(paths, (8, 20)):
                path.write_text(json.dumps(_run(processes={"live": [lifetime(after=delta + 1)]})), encoding="utf-8")
            comparison = compare_reports(*(load_report(path) for path in paths))
            value = comparison.to_dict()
            self.assertEqual(value["verdict"], "unchanged")
            before = value["before"]["process_attribution"]["memory"]["lifetime"]["rows"][0]
            after = value["after"]["process_attribution"]["memory"]["lifetime"]["rows"][0]
            self.assertEqual((before["pid"], after["pid"], before["delta"], after["delta"]), (100, 100, 8, 20))
            before["delta"] = 999
            self.assertEqual(comparison.to_dict()["before"]["process_attribution"]["memory"]["lifetime"]["rows"][0]["delta"], 8)

    def test_narrow_legacy_and_plain_output_keep_process_labels_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(_run(processes={"live": [lifetime(name="[red]漢字🙂[/red]")]})), encoding="utf-8")
            for width in (50, 80):
                for flags in ([], ["--plain"]):
                    console = make_console(width=width, encoding="cp437")
                    self.assertEqual(run_diff([str(path), str(path), *flags], console), 0)
                    output = console.file.getvalue()
                    output.encode("cp437")
                    self.assertIn("[red]", output)
                    self.assertIn("ranked independently", " ".join(output.split()))
                    self.assertTrue(all(len(line) <= width for line in output.splitlines()))

    def test_evidence_positive_components_are_not_presented_as_net_window_arithmetic(self):
        alert = replace(make_alert(), baseline_value=100 * 1024**2, current_value=110 * 1024**2,
            growth_from_new_processes=20 * 1024**2, growth_within_existing=30 * 1024**2,
            contributors=(GrowthContributor(100, 1, "worker", 30 * 1024**2, 0.6),))
        app = DashboardApp("worker.exe", console=make_console())
        output = " ".join(render_text(app, app._alert_detail(alert)).split())
        self.assertIn("Positive growth: new processes 20.0 MB", output)
        self.assertIn("existing processes 30.0 MB", output)
        self.assertIn("Exits and decreases can offset", output)
        self.assertEqual(alert.current_value - alert.baseline_value, 10 * 1024**2)


if __name__ == "__main__":
    unittest.main()
