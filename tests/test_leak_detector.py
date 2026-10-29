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

    def test_memory_alert_names_private_bytes(self) -> None:
        detector = _detector(window_seconds=(10.0,))
        values = [100 * MB + index * 4 * MB for index in range(21)]
        now = _feed(detector, values)
        alert = detector._evaluate_series(
            detector._memory_series, LeakResource.MEMORY, now, values[-1]
        )
        self.assertEqual(alert.metric, "private_bytes")
        self.assertTrue(alert.message.startswith("Potential memory (private bytes) leak: +"))

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
        proc.create_time.return_value = 1.0
        proc.cpu_times.side_effect = [
            SimpleNamespace(user=1.0, system=0.5),
            SimpleNamespace(user=1.6, system=0.9),
            SimpleNamespace(user=4.6, system=0.9),
        ]
        with mock.patch("src.leak_detector.time.monotonic", side_effect=[10.0, 11.0, 12.0]):
            self.assertEqual(detector._sample_cpu_percent(proc), 0.0)  # first sight: baseline
            self.assertAlmostEqual(detector._sample_cpu_percent(proc), 100.0)
            self.assertAlmostEqual(detector._sample_cpu_percent(proc), 300.0)  # three cores

    def test_rate_baselines_restart_when_a_pid_changes_identity(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        detector = _detector()
        proc = mock.Mock(pid=42)
        proc.create_time.return_value = 1.0
        proc.cpu_times.return_value = SimpleNamespace(user=1.0, system=0.0)
        proc.io_counters.return_value = SimpleNamespace(read_bytes=100, write_bytes=50)
        with mock.patch("src.leak_detector.time.monotonic", side_effect=[10, 10, 11, 11, 12, 12]):
            self.assertEqual(detector._sample_cpu_percent(proc), 0.0)
            self.assertEqual(detector._sample_io_rates(proc), (0.0, 0.0))
            proc.create_time.return_value = 2.0
            proc.cpu_times.return_value = SimpleNamespace(user=6.0, system=0.0)
            proc.io_counters.return_value = SimpleNamespace(read_bytes=1_000_100, write_bytes=500_050)
            self.assertEqual(detector._sample_cpu_percent(proc), 0.0)
            self.assertEqual(detector._sample_io_rates(proc), (0.0, 0.0))
            proc.cpu_times.return_value = SimpleNamespace(user=7.0, system=0.0)
            proc.io_counters.return_value = SimpleNamespace(read_bytes=1_000_200, write_bytes=500_100)
            self.assertEqual(detector._sample_cpu_percent(proc), 100.0)
            self.assertEqual(detector._sample_io_rates(proc), (100.0, 50.0))

    def test_unreadable_identity_discards_both_rate_baselines(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        import psutil

        detector = _detector()
        proc = mock.Mock(pid=42)
        proc.create_time.return_value = 1.0
        proc.cpu_times.return_value = SimpleNamespace(user=1.0, system=0.0)
        proc.io_counters.return_value = SimpleNamespace(read_bytes=100, write_bytes=50)
        detector._sample_cpu_percent(proc)
        detector._sample_io_rates(proc)
        proc.create_time.side_effect = psutil.AccessDenied(42)
        self.assertIsNone(detector._sample_cpu_percent(proc))
        self.assertIsNone(detector._sample_io_rates(proc))
        self.assertNotIn(42, detector._cpu_baselines)
        self.assertNotIn(42, detector._io_baselines)
        proc.create_time.side_effect = None
        self.assertEqual(detector._sample_cpu_percent(proc), 0.0)
        self.assertEqual(detector._sample_io_rates(proc), (0.0, 0.0))

    def test_baselines_of_departed_processes_are_dropped(self) -> None:
        from unittest import mock

        from helpers import make_process

        detector = _detector()
        detector._cpu_baselines = {1: (1.0, 0.0, 0.0), 2: (2.0, 0.0, 0.0)}
        detector._io_baselines = {1: (1.0, 0.0, 0, 0), 2: (2.0, 0.0, 0, 0)}
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

    def test_samples_carry_the_process_identity(self) -> None:
        from unittest import mock

        detector = self._detector()
        detector.process_manager._identities[7] = 1234.5
        proc = mock.MagicMock(pid=7)
        proc.name.return_value = "app.exe"
        proc.status.return_value = "running"
        proc.memory_info.return_value = mock.Mock(rss=10, vms=20, private=8)
        proc.num_handles.return_value = 5
        proc.num_threads.return_value = 3
        proc.cpu_times.side_effect = OSError("no")
        proc.io_counters.side_effect = OSError("no")
        metrics = detector._sample_process(proc)
        self.assertEqual(metrics.create_time, 1234.5)
        self.assertEqual(metrics.identity, (7, 1234.5))

        proc.memory_info.side_effect = psutil_access_denied()
        self.assertEqual(detector._sample_process(proc).identity, (7, 1234.5))
        self.assertIsNone(detector.process_manager.created_at(8))

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


class MonitorDiagnosticsTests(unittest.TestCase):
    def _collect(self, detector: LeakDetector, clocks: list[float]):
        from unittest import mock

        from helpers import make_process

        snapshots = []
        for clock in clocks:
            with mock.patch.object(
                detector, "_sample_processes", return_value=[make_process(1)]
            ), mock.patch("src.leak_detector.time.monotonic", return_value=clock):
                snapshots.append(detector._collect_snapshot())
        return snapshots

    def test_snapshots_report_cadence_cost_and_monitor_footprint(self) -> None:
        from unittest import mock

        detector = _detector(sample_interval_ms=500, classifier=mock.Mock(available=False))
        first, second, third = self._collect(detector, [10.0, 10.6, 11.2])
        self.assertIsNone(first.diagnostics.actual_interval_ms)  # one start: no cadence yet
        diagnostics = third.diagnostics
        self.assertEqual(diagnostics.requested_interval_ms, 500)
        self.assertAlmostEqual(diagnostics.actual_interval_ms, 600.0)
        self.assertAlmostEqual(diagnostics.cadence_lag_ms, 100.0)
        self.assertGreaterEqual(diagnostics.collection_ms, 0.0)
        self.assertIsNone(diagnostics.handle_types_age_seconds)  # never classified
        self.assertGreater(diagnostics.monitor_rss_bytes, 0)
        self.assertIsNotNone(diagnostics.monitor_cpu_percent)  # baseline from earlier samples
        self.assertEqual((diagnostics.failed_samples, diagnostics.dropped_snapshots), (0, 0))

    def test_a_pause_restarts_the_cadence_and_paused_snapshots_cost_nothing(self) -> None:
        from unittest import mock

        detector = _detector(sample_interval_ms=500, classifier=mock.Mock(available=False))
        self._collect(detector, [10.0, 10.5])
        detector._collect_starts.clear()  # what the loop does on a paused tick
        paused = detector._build_paused_snapshot()
        self.assertIsNone(paused.diagnostics.collection_ms)
        self.assertIsNone(paused.diagnostics.actual_interval_ms)
        (after,) = self._collect(detector, [100.0])
        self.assertIsNone(after.diagnostics.actual_interval_ms)  # the pause is not a slow sample

    def test_classification_age_and_cost(self) -> None:
        from unittest import mock

        from helpers import make_process

        classifier = mock.Mock(available=True)
        classifier.classify.return_value = {1: _counts(file=1)}
        detector = _detector(classifier=classifier)
        with mock.patch("src.leak_detector.time.monotonic", side_effect=[50.0, 50.02]):
            detector._classify_handles([make_process(1)])
        self.assertAlmostEqual(detector._classify_cost_ms, 20.0)
        with mock.patch("src.leak_detector.time.monotonic", return_value=53.0):
            diagnostics = detector._diagnostics(1.0)
        self.assertAlmostEqual(diagnostics.handle_types_age_seconds, 3.0)
        self.assertAlmostEqual(diagnostics.handle_types_cost_ms, 20.0)

    def test_failures_and_dropped_snapshots_are_counted(self) -> None:
        import time
        from unittest import mock

        from src.events import MetricsStream

        detector = _detector(
            sample_interval_ms=20, classifier=mock.Mock(available=False),
            stream=MetricsStream(max_queue_size=2),
        )
        with mock.patch.object(detector, "_collect_snapshot", side_effect=RuntimeError("boom")), \
                self.assertLogs("src.leak_detector", "ERROR"):
            detector.start()
            time.sleep(0.2)
            detector.stop()
        last = detector.stream.drain()[-1]
        self.assertGreater(last.diagnostics.failed_samples, 2)
        self.assertGreater(detector.stream.dropped, 0)
        self.assertGreater(last.diagnostics.dropped_snapshots, 0)

    def test_an_unreadable_monitor_process_is_unknown(self) -> None:
        from unittest import mock

        import psutil

        detector = _detector(classifier=mock.Mock(available=False))
        with mock.patch("src.leak_detector.psutil.Process", side_effect=psutil.AccessDenied(1)):
            self.assertEqual(detector._sample_self(), (None, None, None))


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


class WindowEvaluationTests(unittest.TestCase):
    """Each window's checks are emitted as data, matching the alert decision."""

    def _windows(self, values: list[float], windows=(10.0,)):
        detector = _detector(window_seconds=windows)
        now = _feed(detector, values)
        return detector._evaluate_windows(
            detector._memory_series, LeakResource.MEMORY, now, values[-1]
        )

    def test_a_qualifying_window_records_the_values_behind_the_alert(self) -> None:
        from src.events import WindowVerdict

        values = [100 * MB + index * 4 * MB for index in range(21)]
        alert, (window,) = self._windows(values)
        self.assertEqual(window.verdict, WindowVerdict.QUALIFIED)
        self.assertTrue(window.qualified and window.judged)
        self.assertEqual(window.points, 21)
        self.assertEqual(window.span_seconds, 10.0)
        self.assertEqual(window.baseline_value, alert.baseline_value)
        self.assertEqual(window.delta, 80 * MB)
        self.assertAlmostEqual(window.slope_per_second, alert.slope_per_second)
        self.assertEqual(window.monotonic_ratio, 1.0)
        self.assertEqual(window.drop_ratio, 0.0)
        self.assertEqual(window.severity, alert.severity)
        # 80/50 + min(8/5, 3) + 1.0 = 4.2: HIGH, below the CRITICAL score of 4.5.
        self.assertAlmostEqual(window.score, 4.2)

    def test_each_failed_check_names_itself(self) -> None:
        from src.events import WindowVerdict

        cases = {
            WindowVerdict.NO_UPWARD_TREND: [100 * MB] * 21,
            WindowVerdict.TOO_MANY_DROPS: [100 * MB + (i % 4) * 20 * MB for i in range(21)],
            WindowVerdict.NOT_MONOTONIC: [100 * MB + i * MB + (i % 2) * 3 * MB for i in range(21)],
            WindowVerdict.BELOW_THRESHOLDS: [100 * MB + i * 1024 for i in range(21)],
        }
        for verdict, values in cases.items():
            with self.subTest(verdict=verdict):
                alert, (window,) = self._windows(values)
                self.assertIsNone(alert)
                self.assertEqual(window.verdict, verdict)
                self.assertTrue(window.judged)
                self.assertIsNotNone(window.monotonic_ratio)
                self.assertIsNotNone(window.drop_ratio)
        _alert, (below,) = self._windows(cases[WindowVerdict.BELOW_THRESHOLDS])
        self.assertLess(below.score, 1.8)

    def test_unfilled_windows_say_what_is_missing(self) -> None:
        from src.events import WindowVerdict

        _alert, (short, long) = self._windows(
            [100 * MB + i * MB for i in range(16)], windows=(10.0, 30.0)
        )
        self.assertEqual(short.verdict, WindowVerdict.SHORT_SPAN)
        self.assertEqual((short.points, short.span_seconds), (16, 7.5))
        self.assertFalse(short.judged)
        self.assertIsNone(short.slope_per_second)
        self.assertEqual(long.verdict, WindowVerdict.SHORT_SPAN)
        _alert, (few,) = self._windows([1.0, 2.0])
        self.assertEqual(few.verdict, WindowVerdict.TOO_FEW_SAMPLES)

    def test_criteria_match_the_checks_applied(self) -> None:
        criteria = _detector().criteria
        self.assertEqual(
            (criteria.min_points, criteria.min_span_ratio, criteria.min_monotonic_ratio,
             criteria.max_drop_ratio),
            (4, 0.85, 0.65, 0.15),
        )
        self.assertEqual(criteria.severity_scores[-1], (LeakSeverity.LOW, 1.8))

    def test_snapshot_evidence_carries_every_window(self) -> None:
        from helpers import make_process

        detector = _detector(window_seconds=(10.0, 30.0))
        snapshots = _collect_trees(detector, [[make_process(1)]] * 25)
        evidence = snapshots[-1].evidence_for(LeakResource.MEMORY)
        self.assertEqual([w.window_seconds for w in evidence.windows], [10.0, 30.0])
        self.assertTrue(evidence.windows[0].judged)
        self.assertFalse(evidence.windows[1].judged)

    def test_no_windows_while_warming_up_or_unreadable(self) -> None:
        from helpers import make_process

        warming = _detector(warmup_seconds=60.0)
        (snapshot,) = _collect_trees(warming, [[make_process(1)]])
        self.assertTrue(all(item.windows == () for item in snapshot.evidence))
        blocked = _detector()
        snapshots = _collect_trees(blocked, [[_denied(1)]] * 5)
        self.assertTrue(all(item.windows == () for item in snapshots[-1].evidence))


if __name__ == "__main__":
    unittest.main()
