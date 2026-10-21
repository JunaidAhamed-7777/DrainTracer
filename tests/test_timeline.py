"""Session timeline annotations, user markers, and per-process lifecycle history."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from helpers import make_console, make_denied, make_process, make_snapshot
from helpers import sample_trees as sampled

from src.events import LeakResource, LeakSeverity, TimelineKind
from src.session import MonitorSession

MB = 1024 * 1024


class _Session(MonitorSession):
    VERSION = "test"


def _session() -> _Session:
    return _Session("C:/targets/worker.exe", console=make_console())


def _worker(pid: int, private_mb: float = 10, ppid: int = 100):
    return replace(make_process(pid, ppid=ppid, private=int(private_mb * MB)), create_time=float(pid))


class TimelineMarkTests(unittest.TestCase):
    def test_lifecycle_signals_and_access_changes_are_marked(self) -> None:
        session = _session()
        root = make_process(100, name="parent.exe")
        trees = [[root, _worker(1), _worker(2, 30 + index * 4)] for index in range(21)]
        trees.append([root, _worker(2, 200), make_denied(3, ppid=100)])
        trees.append([root, _worker(2, 200)])
        sampled(session, trees)
        kinds = [mark.kind for mark in session.timeline]
        self.assertIn(TimelineKind.SIGNAL, kinds)
        self.assertIn(TimelineKind.EXITED, kinds)
        self.assertIn(TimelineKind.STARTED, kinds)
        self.assertIn(TimelineKind.ACCESS_LOST, kinds)
        signal = next(mark for mark in session.timeline if mark.kind == TimelineKind.SIGNAL)
        self.assertEqual(signal.resource, LeakResource.MEMORY)
        self.assertNotEqual(signal.severity, LeakSeverity.NONE)
        exit_mark = next(mark for mark in session.timeline if mark.kind == TimelineKind.EXITED)
        self.assertEqual(exit_mark.pid, 1)
        # Marks use sample time, so they line up with the chart.
        self.assertEqual(exit_mark.timestamp, 1000.0 + 21 * 0.5)

    def test_pause_resume_and_warmup_end_are_marked(self) -> None:
        session = _session()
        session._accept_snapshot(make_snapshot(timestamp=1.0))
        session._accept_snapshot(make_snapshot(timestamp=2.0, paused=True))
        session._accept_snapshot(make_snapshot(timestamp=3.0, paused=True))
        session._accept_snapshot(make_snapshot(timestamp=4.0))
        self.assertEqual(
            [(mark.kind, mark.timestamp) for mark in session.timeline],
            [(TimelineKind.PAUSED, 2.0), (TimelineKind.RESUMED, 4.0)],
        )
        warm = _session()
        warm._accept_snapshot(replace(make_snapshot(timestamp=1.0), warming_up=True))
        warm._accept_snapshot(make_snapshot(timestamp=2.0))
        self.assertEqual([m.kind for m in warm.timeline], [TimelineKind.WARMUP_END])

    def test_bursts_of_starts_become_one_mark(self) -> None:
        session = _session()
        root = make_process(100, name="parent.exe")
        sampled(session, [[root], [root, *[_worker(10 + n) for n in range(6)]]])
        starts = [m for m in session.timeline if m.kind == TimelineKind.STARTED]
        self.assertEqual([m.label for m in starts], ["6 processes started"])


class MarkerTests(unittest.TestCase):
    def test_markers_are_numbered_logged_and_exported(self) -> None:
        session = _session()
        session._started_at = 1000.0
        with mock.patch("src.session.time.time", return_value=1065.0):
            first = session.add_marker()
        second = session.add_marker("opened document")
        self.assertEqual((first.index, first.label), (1, "marker 1"))
        self.assertEqual(second.index, 2)
        messages = [message for _stamp, message, _level in session._events]
        self.assertIn("Marker 1: marker 1 at +00:01:05", messages)
        self.assertTrue(any(m.startswith("Marker 2: opened document") for m in messages))
        self.assertEqual([m.kind for m in session.timeline], [TimelineKind.MARKER] * 2)
        report = session.build_report()
        self.assertEqual(
            [(item["index"], item["label"]) for item in report["markers"]],
            [(1, "marker 1"), (2, "opened document")],
        )
        self.assertEqual(report["markers"][0]["elapsed_seconds"], 65.0)
        self.assertIn("time", report["markers"][0])

    def test_markers_stop_at_the_limit(self) -> None:
        class Limited(_Session):
            MARKER_LIMIT = 2

        session = Limited("C:/targets/worker.exe", console=make_console())
        self.assertIsNotNone(session.add_marker())
        self.assertIsNotNone(session.add_marker())
        self.assertIsNone(session.add_marker())
        self.assertEqual(len(session.markers), 2)
        self.assertIn("Marker limit of 2 reached", session._events[-1][1])


class LifecycleHistoryTests(unittest.TestCase):
    def test_events_for_one_identity_include_restarts_that_replace_it(self) -> None:
        session = _session()
        root = make_process(100, name="parent.exe")
        sampled(session, [[root, _worker(1)], [root, _worker(1)], [root], [root, _worker(2)]])
        events = session.lifecycle_for(1, 1.0)
        self.assertEqual([event.kind.value for event in events], ["exited", "restarted"])
        self.assertEqual(events[-1].pid, 2)
        self.assertEqual(session.lifecycle_for(1, 99.0)[-1].replaces_pid, 1)  # other identity
        self.assertEqual(
            [event.kind.value for event in session.lifecycle_for(1, 99.0)], ["restarted"]
        )


if __name__ == "__main__":
    unittest.main()
