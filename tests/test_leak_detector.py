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
