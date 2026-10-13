"""MonitorSession lifecycle against real short-lived processes."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from helpers import make_console

from src.session import MonitorSession


class _Session(MonitorSession):
    VERSION = "test"


def _wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _script(self, body: str) -> Path:
        path = Path(self.temp.name) / "target.py"
        path.write_text(body, encoding="utf-8")
        return path

    def test_natural_exit_reports_code_and_finishes_tree(self) -> None:
        session = _Session(
            str(self._script("import time\ntime.sleep(0.5)\nraise SystemExit(7)\n")),
            console=make_console(),
        )
        session.start()
        self.addCleanup(session.shutdown)
        self.assertFalse(session.tree_finished)
        self.assertTrue(_wait_for(lambda: session.tree_finished), "target never exited")

        def saw_exit_event() -> bool:
            session.poll()
            return any("exited with code 7" in message for _s, message, _l in session._events)

        self.assertTrue(_wait_for(saw_exit_event), "exit was never logged with its code")
        self.assertEqual(session.manager.exit_code, 7)

    def test_time_limit_counts_down_from_start(self) -> None:
        session = _Session(
            str(self._script("import time\ntime.sleep(30)\n")),
            console=make_console(),
            timeout=0.3,
        )
        self.assertIsNone(session.time_remaining)
        session.start()
        self.addCleanup(session.shutdown)
        remaining = session.time_remaining
        self.assertIsNotNone(remaining)
        self.assertLessEqual(remaining, 0.3)
        self.assertTrue(_wait_for(lambda: session.timed_out, timeout=5.0))
        self.assertEqual(session.time_remaining, 0.0)

    def test_failed_start_leaves_nothing_running(self) -> None:
        session = _Session(str(Path(self.temp.name) / "missing.py"), console=make_console())
        with self.assertRaises(Exception):
            session.start()
        self.assertFalse(session.manager.has_tracked_processes)
        self.assertFalse(session.detector.is_running)

    def test_non_positive_timeout_is_rejected(self) -> None:
        for timeout in (0, -1.0):
            with self.assertRaises(ValueError):
                _Session("C:/targets/worker.exe", console=make_console(), timeout=timeout)


if __name__ == "__main__":
    unittest.main()
