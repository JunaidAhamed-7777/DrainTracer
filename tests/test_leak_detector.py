"""Leak heuristics on synthetic resource series."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401  (adds the repository root to sys.path)

from src.events import LeakResource, LeakSeverity
from src.leak_detector import LeakDetector
from src.process_runner import ProcessManager
from src.stats import least_squares_slope

MB = 1024 * 1024


def _detector(**kwargs) -> LeakDetector:
    return LeakDetector(ProcessManager(None), **kwargs)


def _feed(detector: LeakDetector, values: list[float], step: float = 0.5, start: float = 1000.0):
    """Add values to the memory series; return the last timestamp."""
    stamp = start
    for index, value in enumerate(values):
        stamp = start + index * step
        detector._memory_series.add(stamp, value, detector.max_window_seconds * 1.5)
    return stamp


class SlopeTests(unittest.TestCase):
    def test_linear_growth(self) -> None:
        self.assertAlmostEqual(least_squares_slope([0, 1, 2, 3], [0, 2, 4, 6]), 2.0)

    def test_offset_timestamps_keep_precision(self) -> None:
        times = [1_700_000_000.0 + index * 0.5 for index in range(20)]
        values = [index * 3.0 for index in range(20)]
        self.assertAlmostEqual(least_squares_slope(times, values), 6.0)

    def test_degenerate_inputs(self) -> None:
        self.assertEqual(least_squares_slope([1.0], [5.0]), 0.0)
        self.assertEqual(least_squares_slope([1.0, 1.0], [1.0, 9.0]), 0.0)
        self.assertEqual(least_squares_slope([0.0, 1.0], [1.0]), 0.0)


class SeriesEvaluationTests(unittest.TestCase):
    def test_sustained_growth_raises_an_alert(self) -> None:
        detector = _detector(window_seconds=(10.0,))
        values = [100 * MB + index * 4 * MB for index in range(21)]  # 8 MB/s for 10 s
        now = _feed(detector, values)
        alert = detector._evaluate_series(
            detector._memory_series, LeakResource.MEMORY, now, values[-1]
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.resource, LeakResource.MEMORY)
        self.assertEqual(alert.severity, LeakSeverity.HIGH)
        self.assertAlmostEqual(alert.slope_per_second, 8 * MB)
        self.assertEqual(alert.baseline_value, values[0])

    def test_window_must_be_mostly_covered(self) -> None:
        detector = _detector(window_seconds=(10.0,))
        values = [100 * MB + index * 4 * MB for index in range(16)]  # only 7.5 s
        now = _feed(detector, values)
        self.assertIsNone(
            detector._evaluate_series(detector._memory_series, LeakResource.MEMORY, now, values[-1])
        )

    def test_recovering_bursts_are_not_leaks(self) -> None:
        detector = _detector(window_seconds=(10.0,))
        values = [100 * MB + (index % 4) * 20 * MB for index in range(21)]
        now = _feed(detector, values)
        self.assertIsNone(
            detector._evaluate_series(detector._memory_series, LeakResource.MEMORY, now, values[-1])
        )

    def test_flat_series_is_clear(self) -> None:
        detector = _detector(window_seconds=(10.0,))
        values = [100 * MB] * 21
        now = _feed(detector, values)
        self.assertIsNone(
            detector._evaluate_series(detector._memory_series, LeakResource.MEMORY, now, values[-1])
        )

    def test_strongest_window_wins(self) -> None:
        detector = _detector(window_seconds=(10.0, 30.0))
        values = [100 * MB + index * 2.5 * MB for index in range(61)]  # 5 MB/s for 30 s
        now = _feed(detector, values)
        alert = detector._evaluate_series(
            detector._memory_series, LeakResource.MEMORY, now, values[-1]
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.window_seconds, 30.0)
        self.assertEqual(alert.severity, LeakSeverity.CRITICAL)


class CpuSamplingTests(unittest.TestCase):
    def test_cpu_percent_comes_from_cpu_time_deltas(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        detector = _detector()
        proc = mock.Mock(pid=42)
        proc.cpu_times.side_effect = [
            SimpleNamespace(user=1.0, system=0.5),
            SimpleNamespace(user=1.6, system=0.9),
            SimpleNamespace(user=4.6, system=0.9),
        ]
        with mock.patch("src.leak_detector.time.monotonic", side_effect=[10.0, 11.0, 12.0]):
            self.assertEqual(detector._sample_cpu_percent(proc), 0.0)  # first sight: baseline
            self.assertAlmostEqual(detector._sample_cpu_percent(proc), 100.0)
            self.assertAlmostEqual(detector._sample_cpu_percent(proc), 300.0)  # three cores

    def test_baselines_of_departed_processes_are_dropped(self) -> None:
        from unittest import mock

        from helpers import make_process

        detector = _detector()
        detector._cpu_baselines = {1: (0.0, 0.0), 2: (0.0, 0.0)}
        detector._io_baselines = {1: (0.0, 0, 0), 2: (0.0, 0, 0)}
        with mock.patch.object(
            detector.process_manager, "iter_psutil_processes", return_value=[mock.Mock(pid=2)]
        ), mock.patch.object(detector, "_sample_process", return_value=make_process(2)):
            detector._sample_processes()
        self.assertEqual(list(detector._cpu_baselines), [2])
        self.assertEqual(list(detector._io_baselines), [2])

    def test_a_busy_process_registers_cpu_use(self) -> None:
        import subprocess
        import sys
        import time

        python = getattr(sys, "_base_executable", None) or sys.executable
        busy = subprocess.Popen([python, "-c", "while True: pass"])
        self.addCleanup(busy.wait, 10)
        self.addCleanup(busy.kill)
        manager = ProcessManager(None)
        manager.attach(busy.pid)
        self.addCleanup(manager.detach)
        detector = LeakDetector(manager)
        detector.sample_once()
        time.sleep(0.6)
        snapshot = detector.sample_once()
        root = next(p for p in snapshot.processes if p.pid == busy.pid)
        self.assertGreater(root.cpu_percent, 30.0)


def _counts(**values: int) -> dict[str, int]:
    from src.handle_types import CATEGORIES

    return {**dict.fromkeys(CATEGORIES, 0), **values}


class HandleTypeSamplingTests(unittest.TestCase):
    def _detector_with(self, *results):
        from unittest import mock

        classifier = mock.Mock(available=True)
        classifier.classify.side_effect = list(results)
        return _detector(handle_types_interval=2.0, classifier=classifier), classifier

    def test_classification_follows_its_interval_and_skips_unreadable_processes(self) -> None:
        from dataclasses import replace
        from unittest import mock

        from helpers import make_process

        first = {1: _counts(file=5, event=2)}
        detector, classifier = self._detector_with(first, first)
        processes = [make_process(1), replace(make_process(2), access_denied=True)]
        clock = [100.0, 100.01, 101.0, 102.5, 102.51]
        with mock.patch("src.leak_detector.time.monotonic", side_effect=clock):
            classified, totals = detector._classify_handles(processes)
            self.assertEqual(classifier.classify.call_args.args[0], {1})
            self.assertEqual(classified[0].handle_types, first[1])
            self.assertIsNone(classified[1].handle_types)
            self.assertEqual(totals["file"], 5)
            self.assertEqual(sum(totals.values()), 7)

            _, skipped = detector._classify_handles(processes)  # 101.0: not due yet
            self.assertIsNone(skipped)
            _, again = detector._classify_handles(processes)  # 102.5: due
            self.assertIsNotNone(again)
        self.assertEqual(classifier.classify.call_count, 2)

    def test_slow_passes_back_off(self) -> None:
        from unittest import mock

        from helpers import make_process

        detector, _classifier = self._detector_with({1: _counts(file=1)})
        with mock.patch("src.leak_detector.time.monotonic", side_effect=[50.0, 50.5]):
            detector._classify_handles([make_process(1)])
        self.assertEqual(detector._next_classification, 60.0)  # 0.5 s pass x 20

    def test_handle_alert_names_the_type_that_grew(self) -> None:
        detector = _detector(window_seconds=(10.0,))
        values = [200 + index * 10 for index in range(21)]  # 20 handles/s for 10 s
        now = 1000.0
        for index, value in enumerate(values):
            now = 1000.0 + index * 0.5
            detector._handle_series.add(now, value, 90.0)
        detector._handle_type_history.extend([
            (1000.0, _counts(file=100, event=40)),
            (1004.0, _counts(file=150, event=40)),
            (1010.0, _counts(file=290, event=48, thread=1)),
        ])
        alert = detector._evaluate_series(
            detector._handle_series, LeakResource.HANDLES, now, values[-1]
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.growth_by_type, {"file": 190, "event": 8, "thread": 1})
        self.assertTrue(alert.message.endswith("; by type: file +190, event +8"))

    def test_memory_alerts_carry_no_type_growth(self) -> None:
        detector = _detector(window_seconds=(10.0,))
        values = [100 * MB + index * 4 * MB for index in range(21)]
        now = _feed(detector, values)
        detector._handle_type_history.extend([(1000.0, _counts(file=1)), (1010.0, _counts(file=9))])
        alert = detector._evaluate_series(
            detector._memory_series, LeakResource.MEMORY, now, values[-1]
        )
        self.assertIsNone(alert.growth_by_type)
        self.assertNotIn("by type", alert.message)


class ThresholdTests(unittest.TestCase):
    def test_higher_tolerances_quiet_the_same_growth(self) -> None:
        from src.leak_detector import ResourceThresholds

        values = [100 * MB + index * 4 * MB for index in range(21)]  # 8 MB/s for 10 s
        tolerant = _detector(
            window_seconds=(10.0,),
            thresholds={LeakResource.MEMORY: ResourceThresholds(1024 * MB, 100 * MB)},
        )
        now = _feed(tolerant, values)
        self.assertIsNone(
            tolerant._evaluate_series(tolerant._memory_series, LeakResource.MEMORY, now, values[-1])
        )
        strict = _detector(
            window_seconds=(10.0,),
            thresholds={LeakResource.MEMORY: ResourceThresholds(10 * MB, 1 * MB)},
        )
        now = _feed(strict, values)
        alert = strict._evaluate_series(strict._memory_series, LeakResource.MEMORY, now, values[-1])
        self.assertEqual(alert.severity, LeakSeverity.CRITICAL)

    def test_overrides_keep_defaults_for_other_resources(self) -> None:
        from src.leak_detector import DEFAULT_THRESHOLDS, ResourceThresholds

        detector = _detector(thresholds={LeakResource.THREADS: ResourceThresholds(5, 1)})
        self.assertEqual(detector.thresholds[LeakResource.THREADS], ResourceThresholds(5, 1))
        self.assertEqual(
            detector.thresholds[LeakResource.MEMORY], DEFAULT_THRESHOLDS[LeakResource.MEMORY]
        )

    def test_invalid_parameters_are_rejected(self) -> None:
        from src.leak_detector import ResourceThresholds

        for delta, slope in ((0, 1), (1, -1), (float("inf"), 1), (True, 1)):
            with self.subTest(delta=delta, slope=slope):
                with self.assertRaises(ValueError):
                    ResourceThresholds(delta, slope)
        for windows in ((), (0.0,), (-5.0, 10.0), (float("nan"),)):
            with self.subTest(windows=windows):
                with self.assertRaises(ValueError):
                    _detector(window_seconds=windows)
        with self.assertRaises(ValueError):
            _detector(sample_interval_ms=0)

    def test_windows_are_sorted_and_deduplicated(self) -> None:
        detector = _detector(window_seconds=(60, 10, 30, 10))
        self.assertEqual(detector.window_seconds, (10.0, 30.0, 60.0))
        self.assertEqual(detector.max_window_seconds, 60.0)


class WarmupTests(unittest.TestCase):
    def _detector(self, warmup: float) -> LeakDetector:
        from unittest import mock

        return _detector(
            window_seconds=(10.0,), warmup_seconds=warmup,
            classifier=mock.Mock(available=False),
        )

    def _collect(self, detector: LeakDetector, values: list[float], clock_gaps=(), start=0):
        """Collect one snapshot per private-bytes value, 0.5 s apart.

        ``start`` continues the timeline of an earlier call.
        """
        from unittest import mock

        from helpers import make_process

        snapshots = []
        clock = 50.0 + start * 0.5
        for index, value in enumerate(values, start):
            clock += 0.5
            if index in clock_gaps:
                detector._last_active_sample = None  # what a paused stretch leaves behind
                clock += 100.0
            with mock.patch.object(
                detector, "_sample_processes", return_value=[make_process(1, rss=int(value))]
            ), mock.patch("src.leak_detector.time.time", return_value=1000.0 + index * 0.5), \
                    mock.patch("src.leak_detector.time.monotonic", return_value=clock):
                snapshots.append(detector._collect_snapshot())
        return snapshots

    def test_heuristics_wait_for_the_warmup_and_start_clean(self) -> None:
        detector = self._detector(warmup=5.0)
        values = [100 * MB + index * 4 * MB for index in range(31)]  # 8 MB/s throughout
        snapshots = self._collect(detector, values)
        warm = [snapshot.warming_up for snapshot in snapshots]
        self.assertEqual(warm, [True] * 10 + [False] * 21)
        self.assertFalse(any(snapshot.alerts for snapshot in snapshots[:27]))
        self.assertTrue(snapshots[-1].alerts)  # the window refilled after the warm-up
        self.assertEqual(detector.warmup_baseline["private_bytes"], values[10])
        self.assertEqual(detector.warmup_completed_at, 1005.0)
        self.assertEqual(len(detector._memory_series.points), 21)
        self.assertEqual(detector.warmup_remaining, 0.0)
        self.assertTrue(detector.detection_started)

    def test_detection_starts_only_once_a_window_fills(self) -> None:
        detector = self._detector(warmup=5.0)
        self._collect(detector, [100 * MB] * 27)  # warm-up, then 8 s of samples
        self.assertFalse(detector.detection_started)
        self._collect(detector, [100 * MB], start=27)  # 8.5 s: the 10 s window is ready
        self.assertTrue(detector.detection_started)

    def test_startup_growth_is_only_a_leak_without_warmup(self) -> None:
        spike = [100 * MB + min(index, 10) * 8 * MB for index in range(31)]  # 5 s ramp, then flat
        quiet = self._collect(self._detector(warmup=5.0), spike)
        self.assertFalse(any(snapshot.alerts for snapshot in quiet))
        noisy = self._collect(self._detector(warmup=0.0), spike)
        self.assertTrue(any(snapshot.alerts for snapshot in noisy))
        self.assertFalse(any(snapshot.warming_up for snapshot in noisy))

    def test_paused_time_does_not_count_toward_the_warmup(self) -> None:
        detector = self._detector(warmup=5.0)
        snapshots = self._collect(detector, [100 * MB] * 12, clock_gaps={4})
        self.assertTrue(snapshots[10].warming_up)  # the 100 s gap was not counted
        self.assertFalse(snapshots[11].warming_up)

    def test_negative_warmup_is_rejected(self) -> None:
        for value in (-1.0, float("nan")):
            with self.assertRaises(ValueError):
                _detector(warmup_seconds=value)


def _denied(pid: int):
    """A process whose counters could not be read, as the sampler reports it."""
    from src.events import METRIC_KEYS, ProcessMetrics

    return ProcessMetrics(
        pid=pid, name="access-denied", status="unknown", rss_bytes=0, vms_bytes=0,
        private_bytes=0, handles=0, threads=0, cpu_percent=0.0,
        io_read_bytes_per_sec=0.0, io_write_bytes_per_sec=0.0,
        access_denied=True, unmeasured=tuple(METRIC_KEYS),
    )


def _collect_trees(detector: LeakDetector, trees: list[list]) -> list:
    """Collect one snapshot per process list, 0.5 s apart."""
    from unittest import mock

    snapshots = []
    for index, processes in enumerate(trees):
        with mock.patch.object(detector, "_sample_processes", return_value=list(processes)), \
                mock.patch("src.leak_detector.time.time", return_value=1000.0 + index * 0.5), \
                mock.patch("src.leak_detector.time.monotonic", return_value=50.0 + index * 0.5):
            snapshots.append(detector._collect_snapshot())
    return snapshots


class MeasurementValidityTests(unittest.TestCase):
    def _detector(self, **kwargs) -> LeakDetector:
        from unittest import mock

        return _detector(
            window_seconds=(10.0,), classifier=mock.Mock(available=False), **kwargs
        )

    def test_unreadable_handle_counts_are_unknown_not_zero(self) -> None:
        from unittest import mock

        proc = mock.Mock()
        proc.num_handles.side_effect = OSError("denied")
        self.assertIsNone(LeakDetector._safe_num_handles(proc))

        detector = self._detector()
        proc = mock.MagicMock(pid=7)
        proc.name.return_value = "app.exe"
        proc.status.return_value = "running"
        proc.memory_info.return_value = mock.Mock(rss=10, vms=20, private=8)
        proc.num_handles.side_effect = psutil_access_denied()
        proc.num_threads.return_value = 3
        proc.cpu_times.side_effect = OSError("no")
        proc.io_counters.return_value = mock.Mock(read_bytes=0, write_bytes=0)
        metrics = detector._sample_process(proc)
        self.assertEqual(metrics.handles, 0)
        self.assertFalse(metrics.measured("handles"))
        self.assertFalse(metrics.measured("cpu_percent"))
        self.assertTrue(metrics.measured("private_bytes"))
        self.assertTrue(metrics.measured("io_read_bytes_per_sec"))

    def test_partial_coverage_is_reported(self) -> None:
        from helpers import make_process

        detector = self._detector()
        snapshot = _collect_trees(detector, [[make_process(1), _denied(2)]])[0]
        coverage = snapshot.coverage["private_bytes"]
        self.assertEqual((coverage.measured, coverage.total), (1, 2))
        self.assertEqual(coverage.unmeasured_pids, (2,))
        self.assertTrue(coverage.partial)
        self.assertEqual(snapshot.unmeasured_pids, (2,))
        memory = snapshot.evidence_for(LeakResource.MEMORY)
        self.assertTrue(memory.partial)
        self.assertEqual(snapshot.totals["private_bytes"], 10 * MB)

    def test_exited_processes_are_neither_measured_nor_missing(self) -> None:
        from dataclasses import replace

        from helpers import make_process

        gone = replace(make_process(3), status="dead", unmeasured=("handles",))
        snapshot = _collect_trees(self._detector(), [[make_process(1), gone]])[0]
        self.assertEqual(snapshot.coverage["handles"].total, 1)
        self.assertFalse(snapshot.coverage["handles"].partial)

    def test_access_loss_and_recovery_raise_no_alert(self) -> None:
        from dataclasses import replace

        from helpers import make_process

        steady = make_process(1, rss=100 * MB)
        worker = make_process(2, rss=50 * MB)
        trees = (
            [[steady, worker]] * 21 + [[steady, _denied(2)]] * 10 + [[steady, worker]] * 25
        )
        snapshots = _collect_trees(self._detector(), trees)
        self.assertFalse(any(snapshot.alerts for snapshot in snapshots))
        states = [snapshot.evidence_for(LeakResource.MEMORY).state for snapshot in snapshots]
        from src.events import EvidenceState

        self.assertEqual(states[20], EvidenceState.NO_GROWTH)
        self.assertEqual(states[21], EvidenceState.COLLECTING)  # the segment restarted
        self.assertEqual(states[31], EvidenceState.COLLECTING)  # and again on recovery
        self.assertEqual(states[-1], EvidenceState.NO_GROWTH)

        # Control: the same totals measured for real are a genuine change.
        emptied = replace(worker, rss_bytes=0, vms_bytes=0, private_bytes=0)
        control = [[steady, worker]] * 21 + [[steady, emptied]] * 10 + [[steady, worker]] * 25
        self.assertTrue(any(s.alerts for s in _collect_trees(self._detector(), control)))

    def test_an_unmeasurable_tree_is_unavailable(self) -> None:
        from src.events import EvidenceState

        snapshots = _collect_trees(self._detector(), [[_denied(1)]] * 30)
        evidence = snapshots[-1].evidence_for(LeakResource.HANDLES)
        self.assertEqual(evidence.state, EvidenceState.UNAVAILABLE)
        self.assertEqual(evidence.valid_samples, 0)
        self.assertTrue(snapshots[-1].coverage["handles"].unavailable)


class EvidenceStateTests(unittest.TestCase):
    def test_states_progress_from_collecting_to_a_judgement(self) -> None:
        from unittest import mock

        from helpers import make_process
        from src.events import EvidenceState

        detector = _detector(window_seconds=(10.0, 30.0), classifier=mock.Mock(available=False))
        flat = _collect_trees(detector, [[make_process(1)]] * 21)
        first, last = flat[0].evidence_for(LeakResource.THREADS), flat[-1].evidence_for(
            LeakResource.THREADS
        )
        self.assertEqual(first.state, EvidenceState.COLLECTING)
        self.assertEqual((first.valid_samples, first.needed_samples), (1, 4))
        self.assertEqual(first.needed_seconds, 8.5)
        self.assertEqual(flat[16].evidence_for(LeakResource.THREADS).elapsed_seconds, 8.0)
        self.assertEqual(last.state, EvidenceState.NO_GROWTH)
        self.assertEqual(last.evaluated_window_seconds, 10.0)
        self.assertIsNone(first.evaluated_window_seconds)

        growing = _detector(window_seconds=(10.0,), classifier=mock.Mock(available=False))
        trees = [[make_process(1, rss=100 * MB + index * 4 * MB)] for index in range(21)]
        evidence = _collect_trees(growing, trees)[-1].evidence_for(LeakResource.MEMORY)
        self.assertEqual(evidence.state, EvidenceState.GROWTH)
        self.assertEqual(evidence.severity, LeakSeverity.HIGH)
        self.assertEqual(evidence.evaluated_window_seconds, 10.0)

    def test_warmup_and_pause_have_their_own_states(self) -> None:
        from unittest import mock

        from helpers import make_process
        from src.events import EvidenceState

        detector = _detector(
            window_seconds=(10.0,), warmup_seconds=5.0, classifier=mock.Mock(available=False)
        )
        snapshot = _collect_trees(detector, [[make_process(1)]])[0]
        self.assertEqual(
            {item.state for item in snapshot.evidence}, {EvidenceState.WARMING_UP}
        )
        with mock.patch.object(type(detector.process_manager), "is_running",
                               new_callable=mock.PropertyMock, return_value=True):
            paused = detector._build_paused_snapshot()
        self.assertEqual({item.state for item in paused.evidence}, {EvidenceState.STALE})
        self.assertIsNotNone(paused.coverage)


def psutil_access_denied():
    import psutil

    return psutil.AccessDenied(7)


class SamplingLoopTests(unittest.TestCase):
    def test_the_loop_survives_failures_and_reports_them(self) -> None:
        import time
        from unittest import mock

        detector = _detector(sample_interval_ms=20, classifier=mock.Mock(available=False))
        with mock.patch.object(detector, "_collect_snapshot", side_effect=RuntimeError("boom")), \
                mock.patch.object(type(detector.process_manager), "is_running",
                                  new_callable=mock.PropertyMock, side_effect=OSError("gone")),                 self.assertLogs("src.leak_detector", "ERROR") as logs:
            detector.start()
            time.sleep(0.2)
            detector.stop()
        snapshots = detector.stream.drain()
        self.assertGreater(len(snapshots), 2)
        self.assertEqual(len(logs.records), 1)  # one traceback, not one per sample
        self.assertTrue(all(s.metadata.get("sampling_error") == "boom" for s in snapshots))
        self.assertFalse(snapshots[-1].process_alive)

    def test_ticks_follow_the_cadence_but_back_off_after_slow_samples(self) -> None:
        tick = LeakDetector._next_tick
        self.assertAlmostEqual(tick(10.0, 10.01, 0.05), 10.05)  # cheap: stay on cadence
        self.assertAlmostEqual(tick(10.0, 10.2, 0.05), 10.225)  # slow: idle half a tick
        self.assertAlmostEqual(tick(10.0, 10.04, 0.1), 10.1)

    def test_the_loop_keeps_sampling(self) -> None:
        import time
        from unittest import mock

        detector = _detector(sample_interval_ms=50, classifier=mock.Mock(available=False))
        with mock.patch.object(detector, "_collect_snapshot",
                               side_effect=lambda: detector._build_paused_snapshot()):
            detector.start()
            time.sleep(0.6)
            detector.stop()
        self.assertGreaterEqual(len(detector.stream.drain()), 5)


class SeverityScoreTests(unittest.TestCase):
    def test_score_tiers(self) -> None:
        score = LeakDetector._score_severity
        resource = LeakResource.HANDLES
        self.assertEqual(score(resource, 0.1, 10, 0.7, 10.0), LeakSeverity.NONE)
        # 15/20 + 400/500 + 1.0 = 2.55
        self.assertEqual(score(resource, 15.0, 400, 1.0, 10.0), LeakSeverity.MEDIUM)
        self.assertEqual(score(resource, 40.0, 1000, 1.0, 10.0), LeakSeverity.CRITICAL)

    def test_long_windows_earn_a_bonus(self) -> None:
        score = LeakDetector._score_severity
        resource = LeakResource.THREADS
        # 0.8 + 0.4 + 1.0 = 2.2 (LOW); the 60 s bonus lifts it to 2.7 (MEDIUM).
        self.assertEqual(score(resource, 0.8, 40, 1.0, 10.0), LeakSeverity.LOW)
        self.assertEqual(score(resource, 0.8, 40, 1.0, 60.0), LeakSeverity.MEDIUM)


if __name__ == "__main__":
    unittest.main()
