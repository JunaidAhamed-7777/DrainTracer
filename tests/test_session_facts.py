"""Exact session facts survive the thinning of long chart histories."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from helpers import make_alert, make_console, make_process, make_snapshot

from src.events import (
    EvidenceState,
    LeakResource,
    LeakSeverity,
    MonitorDiagnostics,
    ResourceEvidence,
)
from src.session import MonitorSession
from src.session_facts import SessionFacts

MB = 1024 * 1024
START = 1_000.0


class _SmallHistorySession(MonitorSession):
    VERSION = "test"
    SNAPSHOT_LIMIT = 16


def _judged(state: EvidenceState = EvidenceState.NO_GROWTH):
    return tuple(ResourceEvidence(resource, state) for resource in LeakResource)


def long_session_snapshots(cycles: int = 30, period: int = 10):
    """``cycles`` warning/recovery cycles, one sample each 0.5 s.

    Each cycle signals memory for three samples (LOW, then MEDIUM, then
    LOW) and is clear for the rest. One sample in the middle of the run
    holds a private-bytes spike that is never pinned by a state change.
    """
    total = cycles * period
    spike = total // 2 + 5
    snapshots = []
    for index in range(total):
        stamp = START + index * 0.5
        private = 100 * MB + (500 * MB if index == spike else index * 1024)
        phase = index % period
        alerts = ()
        if phase in (2, 3, 4):
            severity = LeakSeverity.MEDIUM if phase == 3 else LeakSeverity.LOW
            alerts = (make_alert(LeakResource.MEMORY, severity),)
        snapshot = make_snapshot(
            processes=(make_process(100, private=private),),
            alerts=alerts,
            timestamp=stamp,
        )
        snapshots.append(
            replace(
                snapshot,
                evidence=_judged(EvidenceState.GROWTH if alerts else EvidenceState.NO_GROWTH),
                diagnostics=MonitorDiagnostics(
                    500, actual_interval_ms=500.0, collection_ms=4.0 + phase,
                    monitor_cpu_percent=1.0, monitor_rss_bytes=40 * MB,
                ),
            )
        )
    return snapshots, START + spike * 0.5


class SessionFactsTests(unittest.TestCase):
    def test_counts_samples_by_kind_and_skips_gaps(self) -> None:
        facts = SessionFacts()
        facts.observe(replace(make_snapshot(timestamp=10.0), evidence=_judged()))
        facts.observe(replace(make_snapshot(timestamp=10.5), evidence=_judged()))
        facts.observe(make_snapshot(timestamp=11.0, paused=True))
        facts.observe(make_snapshot(timestamp=11.5, metadata={"sampling_error": "boom"}))
        facts.observe(replace(make_snapshot(timestamp=30.0), evidence=_judged()))
        facts.observe(replace(make_snapshot(timestamp=30.5), evidence=_judged()))
        record = facts.to_dict()
        self.assertEqual(
            record["samples"],
            {"taken": 6, "collected": 4, "measured": 4, "paused": 1, "failed": 1},
        )
        # Only the two uninterrupted half-second steps count: the pause and
        # failure break the run, so the 19 s gap is not evaluated time.
        self.assertEqual(record["evaluation"]["evaluated_seconds"], 1.0)
        self.assertEqual(record["evaluation"]["after_warmup_seconds"], 1.0)
        self.assertEqual(record["monitor"]["failed_samples"], 1)

    def test_warmup_and_collecting_time_are_not_evaluated(self) -> None:
        facts = SessionFacts()
        for index in range(4):
            snapshot = replace(make_snapshot(timestamp=index * 1.0), warming_up=index < 2)
            facts.observe(replace(snapshot, evidence=_judged(EvidenceState.COLLECTING)))
        facts.observe(replace(make_snapshot(timestamp=4.0), evidence=_judged()))
        evaluation = facts.to_dict()["evaluation"]
        self.assertEqual(evaluation["after_warmup_seconds"], 2.0)  # from 2.0 to 4.0
        self.assertEqual(evaluation["evaluated_seconds"], 1.0)
        self.assertEqual(evaluation["first_evaluated_at"], 3.0)

    def test_unmeasured_totals_set_no_peak(self) -> None:
        from helpers import make_denied

        facts = SessionFacts()
        facts.observe(make_snapshot(processes=(make_denied(1),), timestamp=1.0))
        self.assertIsNone(facts.peak("private_bytes"))
        facts.observe(make_snapshot(processes=(make_process(1, private=7 * MB),), timestamp=2.0))
        self.assertEqual(facts.peak("private_bytes"), (7 * MB, 2.0))

    def test_long_thinned_session_keeps_exact_facts(self) -> None:
        session = _SmallHistorySession("C:/targets/worker.exe", console=make_console())
        snapshots, spike_at = long_session_snapshots()
        for snapshot in snapshots:
            session._accept_snapshot(snapshot)
        report = json.loads(json.dumps(session.build_report(), default=str))

        stored = report["snapshots"]
        self.assertGreater(report["snapshot_stride"], 1)
        self.assertLess(len(stored), len(snapshots))
        # The chart lost the spike and most episodes...
        stored_peak = max(item["totals"]["private_bytes"] for item in stored)
        self.assertLess(stored_peak, 500 * MB)

        facts = report["session_facts"]
        self.assertEqual(facts["peaks"]["private_bytes"]["value"], 100 * MB + 500 * MB)
        self.assertEqual(facts["peaks"]["private_bytes"]["timestamp"], spike_at)
        memory = facts["signals"]["memory"]
        self.assertEqual(memory["episodes"], 30)
        self.assertEqual(memory["escalations"], 30)
        self.assertEqual(memory["worst"], "medium")
        self.assertEqual(memory["worst_at"], START + 3 * 0.5)  # first time it reached MEDIUM
        self.assertEqual(memory["signal_seconds"], 30.0)  # 30 episodes of 1 s
        self.assertEqual(facts["signals"]["handles"]["episodes"], 0)
        self.assertEqual(facts["samples"]["collected"], len(snapshots))
        self.assertEqual(facts["evaluation"]["evaluated_seconds"], (len(snapshots) - 1) * 0.5)
        self.assertEqual(
            facts["chart"],
            {"stride": report["snapshot_stride"], "stored": len(stored), "seen": len(snapshots)},
        )
        monitor = facts["monitor"]
        self.assertEqual(monitor["collection_ms"]["max"], 13.0)
        self.assertEqual(monitor["collection_ms"]["count"], len(snapshots))
        self.assertEqual(monitor["interval_ms"]["mean"], 500.0)
        self.assertEqual(monitor["monitor_rss_peak_bytes"], 40 * MB)
        self.assertEqual(facts["coverage"]["samples"], 0)  # these snapshots record no coverage


class QueueOverflowTests(unittest.TestCase):
    def _runner(self, budgets=None):
        from src.config import MonitorSettings
        from src.events import MetricsStream
        from src.headless import HeadlessRunner

        class TinyRunner(HeadlessRunner):
            SNAPSHOT_LIMIT = 8

        runner = TinyRunner("C:/targets/worker.exe", console=make_console(),
                            settings=replace(MonitorSettings(), budgets=budgets or {}))
        runner.detector.stream = MetricsStream(max_queue_size=2, facts=runner.facts)
        return runner

    def _publish(self, runner, snapshot):
        runner.detector.stream.publish(replace(snapshot, diagnostics=MonitorDiagnostics(
            500, dropped_snapshots=runner.detector.stream.dropped
        )))

    def test_dropped_only_signal_peak_coverage_and_judgement_remain_authoritative(self) -> None:
        from helpers import make_denied

        from src.headless import EXIT_LEAK

        runner = self._runner({"private_bytes": 512 * MB})
        first = make_snapshot(
            timestamp=0, processes=(make_process(100, private=600 * MB), make_denied(200)),
            alerts=(make_alert(severity=LeakSeverity.HIGH),), measured_coverage=True,
        )
        self._publish(runner, replace(first, evidence=_judged()))
        for stamp in range(1, 4):
            self._publish(runner, make_snapshot(timestamp=stamp, measured_coverage=True))
        self.assertEqual(runner.poll(), 2)
        report = runner.build_report()
        self.assertEqual(runner.worst_severity, LeakSeverity.HIGH)
        self.assertEqual(runner.worst_severity_for(LeakResource.MEMORY), LeakSeverity.HIGH)
        self.assertEqual(runner.exit_status(), EXIT_LEAK)
        self.assertTrue(runner.over_budget)
        self.assertTrue(runner.conclusive)
        self.assertEqual(runner._judged_resources, set(LeakResource))
        self.assertTrue(report["detection_ran"])
        self.assertEqual(report["sample_count"], 4)
        self.assertEqual(report["session_facts"]["samples"]["taken"], 4)
        self.assertEqual(report["session_facts"]["peaks"]["private_bytes"]["value"], 600 * MB)
        self.assertEqual(report["session_facts"]["signals"]["memory"]["episodes"], 1)
        self.assertEqual(report["coverage"]["samples"], 4)
        self.assertEqual(report["coverage"]["metrics"]["private_bytes"]["partial_samples"], 1)
        self.assertEqual(report["coverage"]["unreadable_pids"], [200])

    def test_a_dropped_budget_peak_alone_still_fails_the_run(self) -> None:
        from src.headless import EXIT_BUDGET

        runner = self._runner({"handles": 10})
        self._publish(runner, make_snapshot(timestamp=0, processes=(make_process(100, handles=20),)))
        for stamp in range(1, 4):
            self._publish(runner, make_snapshot(timestamp=stamp, processes=(make_process(100, handles=1),)))
        runner.poll()
        self.assertEqual(runner.exit_status(), EXIT_BUDGET)
        self.assertEqual(runner.budget_checks()[0].peak, 20)

    def test_final_publication_records_its_own_drop(self) -> None:
        runner = self._runner()
        for stamp in range(3):
            self._publish(runner, make_snapshot(timestamp=stamp))
        runner.poll()
        self.assertEqual(runner.detector.stream.dropped, 1)
        self.assertEqual(runner.diagnostics.dropped_snapshots, 1)
        self.assertEqual(runner.snapshot.diagnostics.dropped_snapshots, 1)
        self.assertEqual(runner.facts.to_dict()["monitor"]["dropped_snapshots"], 1)

    def test_live_report_uses_one_atomic_capture_despite_a_new_publication(self) -> None:
        from unittest import mock

        from helpers import make_denied

        runner = self._runner()
        self._publish(runner, make_snapshot(timestamp=0, measured_coverage=True))
        runner.poll()
        capture = runner.facts.capture

        def publish_after_capture():
            result = capture()
            self._publish(runner, make_snapshot(
                timestamp=1, processes=(make_denied(200),), measured_coverage=True,
            ))
            return result

        with mock.patch.object(runner.facts, "capture", side_effect=publish_after_capture) as captured:
            report = runner.build_report()
        captured.assert_called_once_with()
        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(report["coverage"]["samples"], 1)
        self.assertFalse(report["coverage"]["partial"])
        self.assertEqual(report["session_facts"]["samples"]["taken"], 1)
        self.assertEqual(report["session_facts"]["coverage"]["samples"], 1)
        self.assertEqual(runner.facts.to_dict()["samples"]["taken"], 2)
        next_report = runner.build_report()
        self.assertEqual(next_report["sample_count"], 2)
        self.assertTrue(next_report["coverage"]["partial"])

    def test_direct_and_queued_samples_are_counted_once_each(self) -> None:
        runner = self._runner()
        runner._accept_snapshot(make_snapshot(timestamp=0))
        for stamp in range(1, 7):
            self._publish(runner, make_snapshot(timestamp=stamp))
        self.assertEqual(runner.poll(), 2)
        self.assertEqual(runner.poll(), 0)
        runner._accept_snapshot(make_snapshot(timestamp=7))
        self.assertEqual(runner.facts.to_dict()["samples"]["taken"], 8)
        self.assertEqual(runner._sample_count, 8)
        self.assertEqual(runner._snapshot_history.seen, 4)

    def test_dropped_failure_and_pause_counts_survive(self) -> None:
        runner = self._runner()
        self._publish(runner, make_snapshot(timestamp=0, metadata={"sampling_error": "lost failure"}))
        self._publish(runner, make_snapshot(timestamp=1, paused=True))
        self._publish(runner, make_snapshot(timestamp=2))
        self._publish(runner, make_snapshot(timestamp=3))
        runner.poll()
        self.assertEqual(runner.facts.to_dict()["samples"],
                         {"taken": 4, "collected": 2, "measured": 2, "paused": 1, "failed": 1})
        self.assertIn("sampling error: lost failure", runner.monitor_errors)

    def test_publisher_keeps_bounded_state_and_never_calls_session_ui(self) -> None:
        import threading
        from unittest import mock

        from helpers import make_denied

        runner = self._runner()

        def publish():
            for index in range(100):
                runner.detector.stream.publish(make_snapshot(
                    timestamp=index * 2, metadata={"sampling_error": f"failure {index}"}
                ))
                runner.detector.stream.publish(make_snapshot(
                    timestamp=index * 2 + 1, processes=(make_denied(100 + index),),
                    measured_coverage=True,
                ))

        with mock.patch.object(runner, "_append_event") as render_event:
            worker = threading.Thread(target=publish)
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            render_event.assert_not_called()
        runner.poll()
        state = runner.facts.session_state()
        self.assertEqual(runner.facts.to_dict()["samples"]["taken"], 200)
        self.assertEqual(len(state["errors"]), 20)
        self.assertEqual(len(state["unreadable_pids"]), 50)
        self.assertEqual(len(runner._snapshot_history), 2)


def thinned_session_report() -> tuple[dict, float, int]:
    """A reopened (JSON round-tripped) report of the long session: report, spike time, samples."""
    session = _SmallHistorySession("C:/targets/worker.exe", console=make_console())
    snapshots, spike_at = long_session_snapshots()
    for snapshot in snapshots:
        session._accept_snapshot(snapshot)
    return json.loads(json.dumps(session.build_report(), default=str)), spike_at, len(snapshots)


class ReloadedFactsTests(unittest.TestCase):
    def _report(self) -> tuple[dict, float, int]:
        return thinned_session_report()

    def test_analysis_of_a_reopened_report_keeps_the_exact_facts(self) -> None:
        from src.analysis import alert_episodes, load_samples, summarize

        report, spike_at, total = self._report()
        samples = load_samples(report)
        self.assertLess(len(alert_episodes(samples)), 30)  # the chart alone loses episodes

        summary = summarize(report, samples)
        self.assertTrue(summary.exact)
        self.assertTrue(summary.chart_thinned)
        self.assertEqual(summary.chart_stride, report["snapshot_stride"])
        self.assertEqual(summary.peaks["private_bytes"], 600 * MB)
        self.assertEqual(summary.peak_times["private_bytes"], spike_at)
        memory = summary.resources["memory"]
        self.assertEqual(memory.episodes, 30)
        self.assertEqual(memory.worst, "medium")
        self.assertEqual(memory.worst_at, START + 1.5)
        self.assertEqual(memory.signal_seconds, 30.0)
        self.assertEqual(summary.sample_count, total)
        self.assertEqual(summary.facts.evaluated_seconds, (total - 1) * 0.5)
        self.assertEqual(summary.facts.monitor["collection_ms"]["max"], 13.0)

    def test_older_reports_fall_back_to_the_snapshots(self) -> None:
        from src.analysis import load_samples, summarize

        report, _spike_at, _total = self._report()
        del report["session_facts"]
        summary = summarize(report)
        self.assertFalse(summary.exact)
        self.assertIsNone(summary.peak_times)
        self.assertIsNone(summary.resources["memory"].worst_at)
        self.assertLess(summary.peaks["private_bytes"], 500 * MB)
        self.assertEqual(summary.sample_count, len(load_samples(report)))
        self.assertTrue(summary.chart_thinned)  # snapshot_stride still says so

    def test_malformed_facts_are_ignored_field_by_field(self) -> None:
        from src.analysis import load_facts

        facts = load_facts({"session_facts": {
            "peaks": {"private_bytes": {"value": "big"}, "handles": {"value": 9, "timestamp": 3}},
            "signals": {"memory": {"worst": "bogus", "episodes": -1}, "disks": {}},
            "samples": {"collected": 4.0, "failed": None},
            "chart": "thinned",
        }})
        self.assertEqual(dict(facts.peaks), {"handles": (9.0, 3.0)})
        self.assertEqual(facts.worst["memory"], ("none", None))
        self.assertEqual(dict(facts.episodes), {})
        self.assertEqual(dict(facts.samples), {"collected": 4})
        self.assertIsNone(facts.chart_stride)
        self.assertIsNone(load_facts({"session_facts": []}))


if __name__ == "__main__":
    unittest.main()
