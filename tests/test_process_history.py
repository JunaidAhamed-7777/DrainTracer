"""Per-process histories, lifecycle events, and growth attribution."""

from __future__ import annotations

import unittest
from dataclasses import replace

from helpers import make_denied, make_process

from src.events import LifecycleKind
from src.process_history import ProcessHistory

MB = 1024 * 1024


def _worker(pid: int, private_mb: float, created: float | None = None, name="worker.exe"):
    process = make_process(pid, ppid=100, name=name, private=int(private_mb * MB))
    return replace(process, create_time=float(pid) if created is None else created)


def _parent():
    return replace(make_process(100, name="parent.exe"), create_time=1.0)


class LifecycleTests(unittest.TestCase):
    def test_the_first_sample_sets_the_tree_without_events(self) -> None:
        history = ProcessHistory(90.0)
        self.assertEqual(history.observe(0.0, [_parent(), _worker(1, 10)]), ())
        counts = history.counts
        self.assertEqual((counts.live, counts.started, counts.exited), (2, 0, 0))

    def test_starts_exits_and_restarts(self) -> None:
        history = ProcessHistory(90.0)
        history.observe(0.0, [_parent(), _worker(1, 10)])
        started = history.observe(0.5, [_parent(), _worker(1, 10), _worker(2, 10)])
        self.assertEqual([(e.kind, e.pid) for e in started], [(LifecycleKind.STARTED, 2)])

        exited = history.observe(1.0, [_parent(), _worker(2, 10)])
        self.assertEqual([(e.kind, e.pid, e.name) for e in exited],
                         [(LifecycleKind.EXITED, 1, "worker.exe")])

        restarted = history.observe(1.5, [_parent(), _worker(2, 10), _worker(3, 10)])
        self.assertEqual(restarted[0].kind, LifecycleKind.RESTARTED)
        self.assertEqual(restarted[0].replaces_pid, 1)
        counts = history.counts
        self.assertEqual((counts.live, counts.started, counts.exited, counts.restarted),
                         (3, 2, 1, 1))

    def test_a_reused_pid_is_a_different_process(self) -> None:
        history = ProcessHistory(90.0)
        history.observe(0.0, [_parent(), _worker(5, 10, created=50.0)])
        events = history.observe(0.5, [_parent(), _worker(5, 10, created=60.0)])
        self.assertEqual([e.kind for e in events], [LifecycleKind.EXITED, LifecycleKind.RESTARTED])
        self.assertEqual(history.exited()[0].create_time, 50.0)
        self.assertEqual(history.summaries()[0].create_time, 60.0)

    def test_an_exit_can_replace_only_one_worker_across_samples(self) -> None:
        history = ProcessHistory(90.0)
        history.observe(0.0, [_worker(1, 10)])
        history.observe(1.0, [])
        first = history.observe(2.0, [_worker(2, 10)])
        second = history.observe(3.0, [_worker(2, 10), _worker(3, 10)])
        self.assertEqual(first[0].replaces_pid, 1)
        self.assertEqual(second[0].kind, LifecycleKind.STARTED)
        self.assertIsNone(second[0].replaces_pid)
        self.assertEqual(history.counts.restarted, 1)

    def test_same_named_exits_match_distinct_arrivals_in_a_burst(self) -> None:
        history = ProcessHistory(90.0)
        history.observe(0.0, [_worker(1, 10), _worker(2, 10)])
        history.observe(1.0, [])
        events = history.observe(2.0, [_worker(3, 10), _worker(4, 10), _worker(5, 10)])
        self.assertEqual({e.replaces_pid for e in events[:2]}, {1, 2})
        self.assertEqual(events[2].kind, LifecycleKind.STARTED)

    def test_unknown_creation_time_does_not_conflate_separate_pid_exits(self) -> None:
        worker = lambda pid: replace(_worker(pid, 10), create_time=None)
        history = ProcessHistory(90.0)
        history.observe(0.0, [worker(1)])
        history.observe(1.0, [])
        history.observe(2.0, [worker(2)])
        history.observe(3.0, [worker(1), worker(2)])
        history.observe(4.0, [worker(2)])
        events = history.observe(5.0, [worker(2), worker(3)])
        self.assertEqual(events[0].kind, LifecycleKind.RESTARTED)
        self.assertEqual(events[0].replaces_pid, 1)
        self.assertEqual(history.counts.restarted, 2)

    def test_restart_candidates_are_consumed_bounded_and_expire(self) -> None:
        history = ProcessHistory(90.0, max_exited=1)
        history.observe(0.0, [_worker(1, 10)])
        history.observe(1.0, [])
        history.observe(2.0, [_worker(2, 10)])
        self.assertEqual(len(history._restart_candidates), 0)
        history.observe(3.0, [])
        self.assertEqual(len(history._restart_candidates), 1)
        history.observe(4.0, [_worker(3, 10)])
        history.observe(14.0, [_worker(3, 10), _worker(4, 10)])
        self.assertEqual(len(history._restart_candidates), 0)
        self.assertEqual(history.counts.restarted, 2)
        history.observe(15.0, [])
        self.assertEqual(len(history._restart_candidates), 1)

    def test_an_unmatched_exit_outside_the_restart_window_is_not_reused(self) -> None:
        history = ProcessHistory(90.0)
        history.observe(0.0, [_worker(1, 10)])
        history.observe(1.0, [])
        events = history.observe(12.0, [_worker(2, 10)])
        self.assertEqual(events[0].kind, LifecycleKind.STARTED)

    def test_dead_processes_have_exited_and_unreadable_ones_keep_their_name(self) -> None:
        history = ProcessHistory(90.0)
        history.observe(0.0, [_parent(), _worker(1, 10)])
        denied = replace(make_denied(1, ppid=100), create_time=1.0)
        self.assertEqual(history.observe(0.5, [_parent(), denied]), ())
        self.assertEqual(history.summaries()[0].name, "worker.exe")
        dead = replace(_worker(1, 10), status="dead")
        self.assertEqual(history.observe(1.0, [_parent(), dead])[0].kind, LifecycleKind.EXITED)

    def test_an_exited_worker_keeps_its_summary(self) -> None:
        history = ProcessHistory(90.0)
        for index in range(10):
            history.observe(index * 0.5, [_parent(), _worker(7, 10 + index * 4)])
        history.observe(5.0, [_parent()])
        (summary,) = history.exited()
        self.assertEqual((summary.pid, summary.name, summary.exited_at), (7, "worker.exe", 5.0))
        self.assertEqual(summary.first["private_bytes"], 10 * MB)
        self.assertEqual(summary.peak["private_bytes"], 46 * MB)
        self.assertEqual(summary.growth("private_bytes"), 36 * MB)
        self.assertEqual(len(history.points((7, 7.0))), 10)  # evidence outlives the process

    def test_histories_and_exit_records_are_bounded(self) -> None:
        history = ProcessHistory(1.0, max_processes=2, max_exited=3)
        history.observe(0.0, [_worker(pid, 1) for pid in range(1, 6)])
        self.assertEqual(sum(1 for pid in range(1, 6) if history.points((pid, None))), 2)
        for stamp in range(1, 10):
            history.observe(float(stamp), [_worker(1, 1)])
        self.assertEqual(len(history.exited()), 3)
        self.assertLessEqual(len(history.points((1, None))), 2)  # older points aged out

    def test_overflow_worker_starts_history_when_a_live_slot_is_freed(self) -> None:
        history = ProcessHistory(90.0, max_processes=1)
        history.observe(0.0, [_worker(1, 10), _worker(2, 10)])
        self.assertEqual(history.points((2, 2.0)), ())
        history.observe(1.0, [_worker(2, 20)])
        history.observe(2.0, [_worker(2, 30)])
        points = history.points((2, 2.0))
        self.assertEqual([stamp for stamp, _values in points], [1.0, 2.0])
        summary = history.summaries()[0]
        self.assertEqual(summary.first["private_bytes"], 10 * MB)
        self.assertEqual(summary.peak["private_bytes"], 30 * MB)
        growth = next(row for row in history.growth("private_bytes", 0) if row.pid == 2)
        self.assertEqual(growth.delta, 10 * MB)

    def test_freed_slot_is_not_overallocated_to_multiple_overflow_workers(self) -> None:
        history = ProcessHistory(90.0, max_processes=1)
        history.observe(0.0, [_worker(pid, 10) for pid in (1, 2, 3)])
        history.observe(1.0, [_worker(2, 20), _worker(3, 20)])
        self.assertEqual(len(history.points((2, 2.0))), 1)
        self.assertEqual(history.points((3, 3.0)), ())
        self.assertEqual(history.counts.live, 2)


class AttributionTests(unittest.TestCase):
    def test_the_growing_worker_is_the_top_contributor(self) -> None:
        history = ProcessHistory(90.0)
        for index in range(21):
            history.observe(index * 0.5, [
                _parent(), _worker(1, 50), _worker(2, 30 + index * 2), _worker(3, 80),
            ])
        result = history.attribute("private_bytes", 0.0, 10.0)
        top = result.contributors[0]
        self.assertEqual((top.pid, top.name, top.new_process), (2, "worker.exe", False))
        self.assertEqual(top.delta, 40 * MB)
        self.assertEqual(top.share, 1.0)
        self.assertEqual(result.growth_within_existing, 40 * MB)
        self.assertEqual(result.growth_from_new_processes, 0.0)

    def test_new_workers_are_new_process_growth(self) -> None:
        history = ProcessHistory(90.0)
        for index in range(21):
            workers = [_worker(10 + n, 20) for n in range(index // 5 + 1)]
            history.observe(index * 0.5, [_parent(), *workers])
        result = history.attribute("private_bytes", 0.0, 10.0)
        self.assertEqual(result.growth_within_existing, 0.0)
        self.assertEqual(result.growth_from_new_processes, 4 * 20 * MB)
        self.assertTrue(all(c.new_process for c in result.contributors))

    def test_an_exited_process_releases_its_share(self) -> None:
        history = ProcessHistory(90.0)
        for index in range(10):
            history.observe(index * 0.5, [_parent(), _worker(1, 10 + index), _worker(2, 30)])
        history.observe(5.0, [_parent(), _worker(1, 20)])
        result = history.attribute("private_bytes", 0.0, 5.0)
        self.assertEqual([c.pid for c in result.contributors], [1])

        grower = ProcessHistory(90.0)
        for index in range(10):
            grower.observe(index * 0.5, [_parent(), _worker(1, 10 + index * 5)])
        grower.observe(5.0, [_parent()])
        (contributor,) = grower.attribute("private_bytes", 0.0, 4.5).contributors
        self.assertFalse(contributor.exited)  # still running at the window end
        self.assertEqual(grower.attribute("private_bytes", 0.0, 5.0).contributors, ())


class DetectorLifecycleTests(unittest.TestCase):
    def _collect(self, trees):
        from unittest import mock

        from src.leak_detector import LeakDetector
        from src.process_runner import ProcessManager

        detector = LeakDetector(
            ProcessManager(None), window_seconds=(10.0,), classifier=mock.Mock(available=False)
        )
        snapshots = []
        for index, processes in enumerate(trees):
            with mock.patch.object(detector, "_sample_processes", return_value=list(processes)),                     mock.patch("src.leak_detector.time.time", return_value=1000.0 + index * 0.5),                     mock.patch("src.leak_detector.time.monotonic", return_value=50.0 + index * 0.5):
                snapshots.append(detector._collect_snapshot())
        return detector, snapshots

    def test_snapshots_carry_lifecycle_counts_and_summaries(self) -> None:
        trees = [[_parent(), _worker(1, 10)], [_parent(), _worker(1, 10), _worker(2, 10)],
                 [_parent(), _worker(2, 10)]]
        detector, snapshots = self._collect(trees)
        self.assertEqual(snapshots[0].lifecycle, ())
        self.assertEqual(snapshots[1].lifecycle[0].kind, LifecycleKind.STARTED)
        self.assertEqual(snapshots[2].lifecycle[0].kind, LifecycleKind.EXITED)
        counts = snapshots[2].process_counts
        self.assertEqual((counts.live, counts.started, counts.exited), (2, 1, 1))
        self.assertEqual([s.pid for s in snapshots[2].process_summaries], [2, 100])
        self.assertEqual([s.pid for s in snapshots[2].exited_processes], [1])
        self.assertIs(detector.process_history.exited(), snapshots[2].exited_processes)

        from src.report import snapshot_to_dict

        data = snapshot_to_dict(snapshots[2])
        self.assertNotIn("process_summaries", data)
        self.assertNotIn("exited_processes", data)
        self.assertEqual(data["lifecycle"][0]["kind"], "exited")
        self.assertEqual(data["process_counts"]["live"], 2)

    def test_alerts_name_the_growing_worker(self) -> None:
        trees = [
            [_parent(), _worker(1, 50), _worker(2, 30 + index * 4), _worker(3, 80)]
            for index in range(21)
        ]
        _detector, snapshots = self._collect(trees)
        (alert,) = [a for a in snapshots[-1].alerts if a.metric == "private_bytes"]
        self.assertEqual(alert.top_contributor.pid, 2)
        self.assertEqual(alert.top_contributor.delta, 80 * MB)
        self.assertFalse(alert.mostly_new_processes)
        self.assertEqual(alert.growth_within_existing, 80 * MB)
        self.assertIn("; mostly worker.exe PID 2 (+80.0 MB)", alert.message)

        from src.report import snapshot_to_dict

        (record,) = snapshot_to_dict(snapshots[-1])["alerts"]
        self.assertEqual(record["contributors"][0]["pid"], 2)
        self.assertEqual(record["contributors"][0]["share"], 1.0)
        self.assertFalse(record["mostly_new_processes"])

    def test_new_worker_capacity_is_not_described_as_one_process_leaking(self) -> None:
        trees = [
            [_parent(), *[_worker(10 + n, 20) for n in range(index // 5 + 1)]]
            for index in range(21)
        ]
        _detector, snapshots = self._collect(trees)
        (alert,) = [a for a in snapshots[-1].alerts if a.metric == "private_bytes"]
        self.assertTrue(alert.mostly_new_processes)
        self.assertEqual(alert.growth_from_new_processes, 80 * MB)
        self.assertIn("mostly new worker processes (4 started in the window)", alert.message)
        self.assertTrue(all(c.new_process for c in alert.contributors))

    def test_an_exited_worker_is_still_found_in_reports(self) -> None:
        trees = [[_parent(), _worker(2, 30 + index * 4)] for index in range(21)]
        trees += [[_parent()]] * 2
        _detector, snapshots = self._collect(trees)
        alerted = next(s for s in snapshots if s.alerts)
        self.assertEqual(alerted.alerts[0].top_contributor.pid, 2)
        (summary,) = snapshots[-1].exited_processes
        self.assertEqual((summary.pid, summary.growth("private_bytes")), (2, 80 * MB))


class GrowthTests(unittest.TestCase):
    def test_signed_growth_and_slope_per_process_over_an_interval(self) -> None:
        history = ProcessHistory(90.0)
        for step in range(11):
            stamp = step * 1.0
            processes = [_parent(), _worker(1, 10 + step), _worker(2, 50 - step * 2)]
            if step >= 6:
                processes.append(_worker(3, 5))
            history.observe(stamp, processes)
        rows = {row.pid: row for row in history.growth("private_bytes", since=4.0)}
        self.assertEqual(sorted(rows), [1, 2, 3, 100])
        self.assertEqual(rows[1].delta, 6 * MB)
        self.assertAlmostEqual(rows[1].slope_per_second, 1 * MB)
        self.assertEqual(rows[2].delta, -12 * MB)
        self.assertAlmostEqual(rows[2].slope_per_second, -2 * MB)
        self.assertEqual((rows[1].samples, rows[1].span_seconds), (7, 6.0))
        self.assertTrue(rows[3].new_process)
        self.assertEqual(rows[3].delta, 0.0)  # measured values only, unlike attribution
        self.assertFalse(rows[1].new_process)

    def test_exited_processes_keep_their_growth_and_unmeasured_points_are_skipped(self) -> None:
        history = ProcessHistory(90.0)
        history.observe(0.0, [_parent(), _worker(1, 10)])
        history.observe(1.0, [_parent(), _worker(1, 14)])
        history.observe(2.0, [_parent(), replace(make_denied(5, ppid=100), create_time=5.0)])
        rows = {row.pid: row for row in history.growth("private_bytes", since=0.0)}
        self.assertTrue(rows[1].exited)
        self.assertEqual(rows[1].delta, 4 * MB)
        self.assertNotIn(5, rows)  # never measured
        self.assertEqual(history.growth("handles", since=10.0), ())

if __name__ == "__main__":
    unittest.main()
