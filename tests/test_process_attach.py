"""Attach/detach behaviour of ProcessManager against real child processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

import helpers  # noqa: F401  (adds the repository root to sys.path)
import psutil

from src.process_runner import ProcessManager, ProcessManagerError, ProcessNotFoundError

# A virtual environment's python.exe on Windows is a redirector that starts
# the real interpreter as a child. The base interpreter keeps the tree exact.
_PYTHON = getattr(sys, "_base_executable", None) or sys.executable

# Parent sleeps after starting one sleeping child, giving a two-level tree.
_TREE_SCRIPT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "time.sleep(60)\n"
)


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


class AttachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = subprocess.Popen([_PYTHON, "-c", _TREE_SCRIPT])
        self.addCleanup(self._cleanup)
        self.assertTrue(
            _wait_for(lambda: len(psutil.Process(self.target.pid).children()) == 1),
            "target never started its child",
        )

    def _cleanup(self) -> None:
        try:
            for child in psutil.Process(self.target.pid).children(recursive=True):
                child.kill()
        except psutil.Error:
            pass
        self.target.kill()
        self.target.wait(timeout=10)

    def test_attach_tracks_existing_tree_and_detach_leaves_it_running(self) -> None:
        manager = ProcessManager(None)
        self.assertEqual(manager.attach(self.target.pid), self.target.pid)
        self.assertTrue(manager.is_attached)
        self.assertTrue(manager.is_running)
        tracked = manager.refresh_process_tree()
        self.assertEqual(len(tracked), 2)
        child_pid = next(pid for pid in tracked if pid != self.target.pid)
        self.assertEqual(manager.parent_of(child_pid), self.target.pid)

        manager.detach()
        self.assertFalse(manager.has_tracked_processes)
        self.assertFalse(manager.is_running)
        self.assertIsNone(self.target.poll(), "detach must not terminate the target")
        self.assertTrue(psutil.pid_exists(child_pid))

    def test_detach_resumes_a_paused_target(self) -> None:
        manager = ProcessManager(None)
        manager.attach(self.target.pid)
        self.assertTrue(manager.pause())
        self.assertEqual(psutil.Process(self.target.pid).status(), psutil.STATUS_STOPPED)
        manager.detach()
        self.assertNotEqual(psutil.Process(self.target.pid).status(), psutil.STATUS_STOPPED)

    def test_explicit_kill_ends_the_attached_tree(self) -> None:
        manager = ProcessManager(None)
        manager.attach(self.target.pid)
        tracked = manager.refresh_process_tree()
        manager.kill()
        self.assertFalse(manager.has_tracked_processes)
        self.assertTrue(_wait_for(lambda: not any(psutil.pid_exists(p) for p in tracked)))

    def test_a_tree_that_contains_draintracer_is_never_paused_or_killed(self) -> None:
        from unittest import mock

        # Pretend DrainTracer runs as the target's child, as it would under
        # an attached terminal; only processes this test started are at risk.
        child = psutil.Process(self.target.pid).children()[0]
        chain = {child.pid: child.create_time(),
                 self.target.pid: psutil.Process(self.target.pid).create_time()}
        manager = ProcessManager(None)
        with mock.patch("src.process_runner._monitor_chain", return_value=chain):
            manager.attach(self.target.pid)
        self.addCleanup(manager.detach)
        self.assertTrue(manager.contains_monitor)
        self.assertEqual(manager.refresh_process_tree(), {self.target.pid})

        self.assertFalse(manager.pause())
        self.assertIn("could freeze DrainTracer's own terminal", manager.last_error)
        self.assertNotEqual(psutil.Process(self.target.pid).status(), psutil.STATUS_STOPPED)
        manager.kill()
        manager.terminate()
        self.assertIn("left running", manager.last_error)
        self.assertIsNone(self.target.poll())
        self.assertTrue(child.is_running())


class PollLoopTests(unittest.TestCase):
    def test_a_failed_refresh_does_not_stop_tracking(self) -> None:
        from unittest import mock

        manager = ProcessManager(None, poll_interval=0.02)
        manager._root_pid = os.getpid()
        manager._attached = True
        manager._tracked_pids = {os.getpid()}
        calls = []

        def flaky() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")

        with mock.patch.object(manager, "_refresh_tracked_pids", side_effect=flaky):
            manager._start_poll_thread()
            self.assertTrue(_wait_for(lambda: len(calls) >= 3, timeout=5.0))
            self.assertTrue(manager._poll_thread.is_alive())
            manager._stop_poll_thread()


class AttachValidationTests(unittest.TestCase):
    def test_missing_pid_is_rejected(self) -> None:
        unused = max(psutil.pids()) + 4096
        with self.assertRaises(ProcessNotFoundError):
            ProcessManager(None).attach(unused)

    def test_self_attach_is_rejected(self) -> None:
        with self.assertRaises(ProcessManagerError):
            ProcessManager(None).attach(os.getpid())

    def test_attaching_to_an_ancestor_leaves_draintracer_out(self) -> None:
        # Read-only: the test runner's parent is tracked but never touched.
        parent = psutil.Process().parent()
        if parent is None:
            self.skipTest("the test runner has no parent process")
        manager = ProcessManager(None)
        manager.attach(parent.pid)
        try:
            self.assertTrue(manager.contains_monitor)
            tracked = manager.refresh_process_tree()
            self.assertIn(parent.pid, tracked)
            self.assertNotIn(os.getpid(), tracked)
        finally:
            manager.detach()

    def test_attach_only_manager_cannot_launch(self) -> None:
        with self.assertRaises(ProcessManagerError):
            ProcessManager(None).start()


if __name__ == "__main__":
    unittest.main()
