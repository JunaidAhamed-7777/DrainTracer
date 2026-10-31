"""Retained process rankings describe each run without pairing identities or causes."""

from __future__ import annotations

import copy
import unittest

from helpers import make_snapshot

from src.analysis import PROCESS_SUMMARY_SCAN_LIMIT, process_attribution, summarize
from src.events import ProcessSummary
from src.report import process_summary_to_dict, snapshot_to_dict


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


if __name__ == "__main__":
    unittest.main()
