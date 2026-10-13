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
