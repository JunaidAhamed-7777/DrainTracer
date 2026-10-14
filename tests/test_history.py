"""Session-long snapshot history: bounded, thinned evenly, keeping state changes."""

from __future__ import annotations

import unittest
from dataclasses import replace

from helpers import make_alert, make_snapshot

from src.events import LeakSeverity
from src.history import SnapshotHistory

HIGH = (make_alert(severity=LeakSeverity.HIGH),)


def _stamps(history: SnapshotHistory) -> list[float]:
    return [snapshot.timestamp for snapshot in history]


class SnapshotHistoryTests(unittest.TestCase):
    def test_short_sessions_keep_every_snapshot(self) -> None:
        history = SnapshotHistory(16)
        for index in range(15):
            history.append(make_snapshot(timestamp=float(index)))
        self.assertEqual(_stamps(history), [float(index) for index in range(15)])
        self.assertEqual(history.stride, 1)

    def test_long_sessions_are_thinned_evenly_from_the_first_sample(self) -> None:
        history = SnapshotHistory(16)
        for index in range(1000):
            history.append(make_snapshot(timestamp=float(index)))
            self.assertLessEqual(len(history), 16)
        stamps = _stamps(history)
        self.assertEqual(stamps[0], 0.0)
        self.assertEqual(stamps[-1], 999.0)  # the latest is always included
        stride = history.stride
        self.assertEqual(stride & (stride - 1), 0)  # a power of two
        self.assertTrue(all(stamp % stride == 0 for stamp in stamps[:-1]))
        self.assertGreaterEqual(len(history), 8)  # halved, never emptied
        self.assertEqual(history.seen, 1000)

    def test_state_changes_survive_thinning(self) -> None:
        history = SnapshotHistory(16)
        for index in range(1000):
            snapshot = make_snapshot(timestamp=float(index), alerts=HIGH if 301 <= index <= 303 else ())
            if index >= 900:
                snapshot = replace(snapshot, process_alive=False)
            history.append(snapshot)
        stamps = _stamps(history)
        for moment in (301.0, 304.0, 900.0):  # raised, cleared, exited
            self.assertIn(moment, stamps)
        self.assertEqual(stamps, sorted(stamps))

    def test_a_signal_flapping_on_every_sample_stays_bounded(self) -> None:
        history = SnapshotHistory(16)
        for index in range(5000):
            history.append(make_snapshot(timestamp=float(index), alerts=HIGH if index % 2 else ()))
            self.assertLessEqual(len(history), 16)
        stamps = _stamps(history)
        self.assertEqual((stamps[0], stamps[-1]), (0.0, 4999.0))
        self.assertEqual(stamps, sorted(stamps))

    def test_tiny_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SnapshotHistory(4)


if __name__ == "__main__":
    unittest.main()
