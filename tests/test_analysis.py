"""Report analysis: samples, signal changes, episodes, summaries, chart data."""

from __future__ import annotations

import unittest

from helpers import make_alert, make_process, make_snapshot

from src.analysis import (
    alert_episodes,
    chart_payload,
    load_samples,
    signal_changes,
    summarize,
)
from src.events import LeakResource, LeakSeverity
from src.report import snapshot_to_dict

MB = 1024 * 1024


def _snap(stamp: float, severity: LeakSeverity | None = None, rss_mb: int = 10,
          resource: LeakResource = LeakResource.MEMORY, **kwargs) -> dict:
    alerts = (make_alert(resource, severity),) if severity else ()
    processes = kwargs.pop("processes", (make_process(100, rss=rss_mb * MB),))
    return snapshot_to_dict(
        make_snapshot(processes=processes, alerts=alerts, timestamp=stamp, **kwargs)
    )


def _report(*snapshots: dict, **extra) -> dict:
    return {"snapshots": list(snapshots), **extra}


class LoadSamplesTests(unittest.TestCase):
    def test_skips_malformed_entries_and_sorts(self) -> None:
        report = _report(
            _snap(3.0), "junk", {"timestamp": "soon"}, _snap(1.0), {"timestamp": float("nan")},
            _snap(2.0),
        )
        self.assertEqual([sample.timestamp for sample in load_samples(report)], [1.0, 2.0, 3.0])

    def test_fresh_exports_and_json_round_trips_agree(self) -> None:
        import json

        report = _report(_snap(1.0), _snap(2.0, LeakSeverity.HIGH))
        fresh = load_samples(report)
        loaded = load_samples(json.loads(json.dumps(report)))
        self.assertEqual(fresh, loaded)
        self.assertTrue(all(sample.measured for sample in fresh))

    def test_tolerates_missing_or_wrong_containers(self) -> None:
        self.assertEqual(load_samples({}), [])
        self.assertEqual(load_samples({"snapshots": {"a": 1}}), [])
        sample = load_samples(_report({"timestamp": 5, "totals": [], "alerts": "x"}))[0]
        self.assertEqual(dict(sample.totals), {})
        self.assertEqual(dict(sample.alerts), {})
        self.assertFalse(sample.measured)

    def test_ignores_unknown_resources_and_clear_alerts(self) -> None:
        snapshot = _snap(1.0)
        snapshot["alerts"] = [
            {"resource": "gpu", "severity": "high"},
            {"resource": "memory", "severity": "none"},
            {"resource": "handles", "severity": "LOW"},
        ]
        self.assertEqual(list(load_samples(_report(snapshot))[0].alerts), ["handles"])


class SignalTests(unittest.TestCase):
    def test_changes_mark_raises_and_escalations_only(self) -> None:
        samples = load_samples(_report(
            _snap(1.0),
            _snap(2.0, LeakSeverity.LOW),
            _snap(3.0, LeakSeverity.HIGH),
            _snap(4.0, LeakSeverity.MEDIUM),
            _snap(5.0),
            _snap(6.0, LeakSeverity.MEDIUM),
        ))
        changes = [(c.timestamp, c.severity, c.kind) for c in signal_changes(samples)]
        self.assertEqual(
            changes,
            [(2.0, "low", "raised"), (3.0, "high", "escalated"), (6.0, "medium", "raised")],
        )

    def test_paused_and_failed_samples_do_not_split_signals(self) -> None:
        samples = load_samples(_report(
            _snap(1.0, LeakSeverity.MEDIUM),
            _snap(2.0, paused=True),
            _snap(3.0, metadata={"sampling_error": "boom"}),
            _snap(4.0, LeakSeverity.MEDIUM),
        ))
        self.assertEqual(len(signal_changes(samples)), 1)
        episodes = alert_episodes(samples)
        self.assertEqual(len(episodes), 1)
        self.assertEqual((episodes[0].start, episodes[0].end), (1.0, 4.0))

    def test_episodes_keep_the_strongest_severity(self) -> None:
        samples = load_samples(_report(
            _snap(1.0, LeakSeverity.LOW),
            _snap(2.0, LeakSeverity.CRITICAL),
            _snap(3.0, LeakSeverity.MEDIUM),
            _snap(4.0),
            _snap(5.0, LeakSeverity.LOW, resource=LeakResource.THREADS),
        ))
        episodes = alert_episodes(samples)
        self.assertEqual(
            [(e.resource, e.severity, e.start, e.end) for e in episodes],
            [("memory", "critical", 1.0, 3.0), ("threads", "low", 5.0, 5.0)],
        )
        self.assertEqual(episodes[0].duration, 2.0)


class SummaryTests(unittest.TestCase):
    def test_measured_samples_drive_growth_peaks_and_drift(self) -> None:
        report = _report(
            _snap(0.0, rss_mb=100),
            _snap(1.0, rss_mb=110),
            _snap(2.0, rss_mb=500, paused=True),
            _snap(3.0, rss_mb=130, severity=LeakSeverity.HIGH),
            _snap(4.0, processes=()),
        )
        report["snapshots"][-1]["processes"] = []
        summary = summarize(report)
        memory = summary.resources["memory"]
        self.assertEqual(memory.first, 100 * MB)
        self.assertEqual(memory.last, 130 * MB)
        self.assertEqual(memory.growth, 30 * MB)
        self.assertEqual(memory.peak, 130 * MB)
        self.assertAlmostEqual(memory.drift_per_second, 10 * MB, delta=0.01 * MB)
        self.assertEqual(memory.worst, "high")
        self.assertEqual(summary.worst, "high")
        self.assertEqual(summary.peaks["rss_bytes"], 130 * MB)
        self.assertEqual(summary.sample_count, 4)
        self.assertEqual(summary.duration, 4.0)

    def test_empty_report(self) -> None:
        summary = summarize({})
        self.assertEqual(summary.worst, "none")
        self.assertEqual(summary.duration, 0.0)
        self.assertIsNone(summary.resources["handles"].growth)
        self.assertIsNone(summary.resources["handles"].drift_per_second)


class HandleTypeTests(unittest.TestCase):
    def _typed(self, stamp: float, **counts: int) -> dict:
        snapshot = _snap(stamp)
        snapshot["handle_types"] = counts
        return snapshot

    def test_summary_reports_type_growth_and_final_counts(self) -> None:
        report = _report(
            self._typed(1.0, file=10, event=5, other=40),
            _snap(2.0),
            self._typed(3.0, file=60, event=5, other=41, socket=2),
        )
        summary = summarize(report)
        self.assertEqual(summary.handle_types, {"file": 60, "event": 5, "other": 41, "socket": 2})
        self.assertEqual(summary.handle_type_growth, {"file": 50, "socket": 2, "other": 1})

    def test_one_classification_has_no_growth(self) -> None:
        summary = summarize(_report(self._typed(1.0, file=3)))
        self.assertEqual(summary.handle_types, {"file": 3})
        self.assertIsNone(summary.handle_type_growth)
        self.assertIsNone(summarize(_report(_snap(1.0))).handle_types)

    def test_bad_counts_are_ignored(self) -> None:
        report = _report(self._typed(1.0, file="many", event=-3, socket=2.0))
        self.assertEqual(load_samples(report)[0].handle_types, {"socket": 2})

    def test_chart_series_use_their_own_time_base(self) -> None:
        report = _report(
            self._typed(10.0, file=1, other=9, event=0),
            _snap(11.0),
            self._typed(12.0, file=5, other=9, event=0),
        )
        series = chart_payload(report)["handle_types"]
        self.assertEqual(series["t"], [0.0, 2.0])
        self.assertEqual(series["categories"], ["file", "other"])  # never-used types dropped
        self.assertEqual(series["series"], {"file": [1, 5], "other": [9, 9]})


class ChartPayloadTests(unittest.TestCase):
    def test_unmeasured_points_are_gaps_and_pauses_merge(self) -> None:
        exited = _snap(5.0)
        exited["process_alive"] = False
        exited["processes"] = []
        report = _report(
            _snap(1.0),
            _snap(2.0, paused=True),
            _snap(3.0, paused=True),
            _snap(4.0, LeakSeverity.HIGH),
            exited,
            event_log=[
                {"timestamp": 4.0, "level": "error", "message": "Memory leak signal HIGH"},
                {"timestamp": "bad"},
            ],
        )
        payload = chart_payload(report)
        self.assertEqual(payload["start"], 1.0)
        self.assertEqual(payload["t"], [0.0, 1.0, 2.0, 3.0, 4.0])
        self.assertEqual(payload["series"]["rss"], [10 * MB, None, None, 10 * MB, None])
        self.assertEqual(payload["paused"], [[1.0, 3.0]])
        self.assertEqual(payload["exits"], [4.0])
        self.assertEqual(payload["markers"][0]["t"], 3.0)
        self.assertEqual(payload["spans"], [
            {"resource": "memory", "severity": "high", "start": 3.0, "end": 3.0},
        ])
        self.assertEqual(payload["events"], [
            {"t": 3.0, "level": "error", "message": "Memory leak signal HIGH"},
        ])

    def test_no_samples(self) -> None:
        payload = chart_payload({"snapshots": []})
        self.assertEqual(payload["t"], [])
        self.assertIsNone(payload["start"])


if __name__ == "__main__":
    unittest.main()
