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
from src.session_facts import SessionFacts


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
        facts = SessionFacts()
        for _ in range(4):
            facts.observe(self.tick())
        sample = self.tick(9, value=180 * 1024**2)
        facts.observe(sample)
        self.assertEqual(sample.metadata.get("timing_reset"), "sampling_gap")
        self.assertEqual(len(self.detector._memory_series.points), 1)
        self.assertFalse(sample.alerts)
        self.assertFalse(self.detector.detection_started)
        monitor = facts.to_dict()["monitor"]
        self.assertEqual(monitor["sampling_gaps"], 1)
        self.assertEqual(monitor["clock_adjustments"], 0)
        self.assertLess(monitor["interval_ms"]["max"], 2000)
        self.assertLess(facts.to_dict()["evaluation"]["after_warmup_seconds"], 5)

    def test_existing_process_growth_is_not_new_capacity_after_a_clock_reset(self) -> None:
        for _ in range(22):
            self.tick()
        self.wall -= 600
        self.tick()
        for index in range(1, 23):
            sample = self.tick(value=(100 + 4 * index) * 1024**2)
        alert = next(item for item in sample.alerts if item.resource == LeakResource.MEMORY)
        self.assertEqual(alert.growth_from_new_processes, 0)
        self.assertGreater(alert.growth_within_existing, 0)
        self.assertFalse(alert.mostly_new_processes)

    def test_gap_restarts_warmup_without_counting_unobserved_sleep_time(self) -> None:
        self.detector.warmup_seconds = 5
        for _ in range(4):
            self.tick()
        sample = self.tick(20)
        self.assertTrue(sample.warming_up)
        self.assertGreater(self.detector.warmup_remaining, 3)


if __name__ == "__main__":
    unittest.main()
