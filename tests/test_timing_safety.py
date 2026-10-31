"""Clock changes and unobserved gaps must not masquerade as continuous evidence."""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import make_process

from src.events import LeakResource
from src.leak_detector import LeakDetector
from src.process_runner import ProcessManager


class TimingSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = LeakDetector(ProcessManager(None), window_seconds=(10,),
                                     classifier=mock.Mock(available=False))
        self.wall, self.monotonic = 1000.0, 50.0

    def collect(self, value=100 * 1024**2):
        clock = SimpleNamespace(time=lambda: self.wall, monotonic=lambda: self.monotonic,
                                perf_counter=time.perf_counter)
        with mock.patch("src.leak_detector.time", clock), mock.patch.object(
            self.detector, "_sample_processes", return_value=[make_process(100, rss=value)]
        ):
            return self.detector.sample_once()

    def tick(self, seconds=0.5, value=100 * 1024**2):
        self.wall += seconds
        self.monotonic += seconds
        return self.collect(value)

    def test_backwards_clock_restarts_windows_and_bounds_future_point_retention(self) -> None:
        for _ in range(22):
            self.tick()
        self.wall -= 600
        sample = self.tick()
        self.assertEqual(sample.metadata.get("timing_reset"), "clock_adjustment")
        self.assertEqual(len(self.detector._memory_series.points), 1)
        self.assertEqual(len(self.detector.process_history.points((100, None))), 1)
        self.assertGreaterEqual(sample.evidence_for(LeakResource.MEMORY).elapsed_seconds, 0)
        for _ in range(100):
            self.tick()
        self.assertLessEqual(len(self.detector._memory_series.points), 31)

    def test_forward_clock_restart_is_explicit_and_keeps_prior_judgments_historical(self) -> None:
        for _ in range(22):
            self.tick()
        self.assertTrue(self.detector.detection_started)
        self.wall += 600
        sample = self.tick()
        self.assertEqual(sample.metadata.get("timing_reset"), "clock_adjustment")
        self.assertEqual(len(self.detector._memory_series.points), 1)
        self.assertTrue(self.detector.detection_started)
        self.assertFalse(any(window.judged for window in sample.evidence_for(LeakResource.MEMORY).windows))

    def test_long_unobserved_gap_cannot_complete_a_window_from_four_sparse_points(self) -> None:
        for _ in range(4):
            self.tick()
        sample = self.tick(9, value=180 * 1024**2)
        self.assertEqual(sample.metadata.get("timing_reset"), "sampling_gap")
        self.assertEqual(len(self.detector._memory_series.points), 1)
        self.assertFalse(sample.alerts)
        self.assertFalse(self.detector.detection_started)


if __name__ == "__main__":
    unittest.main()
