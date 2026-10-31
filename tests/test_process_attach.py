"""Attach/detach behaviour of ProcessManager against real child processes."""

from __future__ import annotations

import ctypes
import math
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import helpers  # noqa: F401  (adds the repository root to sys.path)
import psutil

from src import process_runner
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
        self.addCleanup(manager.detach)
        self.assertTrue(manager.pause())
        # Windows reports thread suspension asynchronously after NtSuspendProcess.
        self.assertTrue(_wait_for(
            lambda: psutil.Process(self.target.pid).status() == psutil.STATUS_STOPPED,
        ), "target did not become suspended")
        manager.detach()
        self.assertTrue(_wait_for(
            lambda: psutil.Process(self.target.pid).status() != psutil.STATUS_STOPPED,
        ), "detach did not resume the target")

    def test_explicit_kill_ends_the_attached_tree(self) -> None:
        manager = ProcessManager(None)
        manager.attach(self.target.pid)
        tracked = manager.refresh_process_tree()
        manager.kill()
        self.assertFalse(manager.has_tracked_processes)
        # Linux retains exited PIDs as zombies until their parent reaps
        # them. They no longer run and must not count as failed cleanup.
        self.target.wait(timeout=10)
        def alive(pid: int) -> bool:
            try:
                return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return False
        self.assertTrue(_wait_for(lambda: not any(alive(p) for p in tracked)))

    def test_failed_kill_keeps_live_processes_tracked(self) -> None:
        manager = ProcessManager(None)
        manager.attach(self.target.pid)
        self.addCleanup(manager.detach)
        with (
            mock.patch.object(manager, "_terminate_pid"),
            mock.patch.object(manager, "KILL_WAIT_SECONDS", 0),
        ):
            manager.kill()
        self.assertTrue(manager.has_tracked_processes)
        self.assertIn("remaining PIDs", manager.last_error)
        self.assertIsNone(self.target.poll())

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


class SelectedIdentityTests(unittest.TestCase):
    def test_matching_selection_captures_the_verified_identity_without_reopening(self) -> None:
        manager = ProcessManager(None)
        process = mock.Mock(pid=4242)
        process.is_running.return_value = True
        process.status.return_value = psutil.STATUS_RUNNING
        process.create_time.return_value = 10.0
        with (
            mock.patch.object(process_runner.psutil, "Process", return_value=process) as opened,
            mock.patch.object(process_runner, "_monitor_chain", return_value={}),
            mock.patch.object(manager, "_refresh_tracked_pids"),
            mock.patch.object(manager, "_start_poll_thread"),
        ):
            self.assertEqual(manager.attach(4242, expected_create_time=10.0), 4242)
        opened.assert_called_once_with(4242)
        self.assertEqual(manager.created_at(4242), 10.0)

    def test_changed_or_unreadable_selection_never_starts_tracking(self) -> None:
        from src.process_runner import ProcessAccessDeniedError

        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                manager = ProcessManager(None)
                process = mock.Mock(pid=4242)
                process.is_running.return_value = True
                process.status.return_value = psutil.STATUS_RUNNING
                process.create_time.return_value = 20.0
                if unreadable:
                    process.create_time.side_effect = psutil.AccessDenied(4242)
                error = ProcessAccessDeniedError if unreadable else ProcessNotFoundError
                with (
                    mock.patch.object(process_runner.psutil, "Process", return_value=process),
                    mock.patch.object(manager, "_refresh_tracked_pids") as refresh,
                    mock.patch.object(manager, "_start_poll_thread") as start_poll,
                    self.assertRaises(error),
                ):
                    manager.attach(4242, expected_create_time=10.0)
                refresh.assert_not_called()
                start_poll.assert_not_called()
                self.assertIsNone(manager.root_pid)
                self.assertFalse(manager.has_tracked_processes)
                process.suspend.assert_not_called()
                process.kill.assert_not_called()

    def test_direct_pid_attachment_can_still_select_an_unreadable_identity(self) -> None:
        manager = ProcessManager(None)
        process = mock.Mock(pid=4242)
        process.create_time.side_effect = psutil.AccessDenied(4242)
        with (
            mock.patch.object(process_runner.psutil, "Process", return_value=process),
            mock.patch.object(process_runner, "_monitor_chain", return_value={}),
            mock.patch.object(manager, "_refresh_tracked_pids"),
            mock.patch.object(manager, "_start_poll_thread"),
        ):
            self.assertEqual(manager.attach(4242), 4242)
        self.assertIsNone(manager.created_at(4242))


@unittest.skipUnless(sys.platform == "win32", "Windows process-handle liveness")
class WindowsLivenessTests(unittest.TestCase):
    def test_creation_time_is_identical_across_native_fast_and_fallback_reads(self) -> None:
        from ctypes import wintypes

        import psutil._pswindows as windows

        target = subprocess.Popen([_PYTHON, "-c", "import time; time.sleep(60)"])
        self.addCleanup(target.wait, 10)
        self.addCleanup(target.kill)
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        api.GetProcessTimes.restype = wintypes.BOOL
        stamps = [wintypes.FILETIME() for _ in range(4)]
        self.assertTrue(api.GetProcessTimes(target._handle, *map(ctypes.byref, stamps)))
        ticks = (stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime
        # Subtract the integer epoch before float division, matching psutil.
        # Dividing first can introduce a different rounding error.
        expected = (ticks - 116444736000000000) / 10000000
        self.assertEqual({psutil.Process(target.pid).create_time() for _ in range(200)}, {expected})
        with mock.patch.object(windows.cext, "proc_times", side_effect=ctypes.WinError(5)):
            self.assertEqual({psutil.Process(target.pid).create_time() for _ in range(20)}, {expected})

    def test_close_but_distinct_windows_identities_never_reach_process_controls(self) -> None:
        original = 1700000000.0
        for replacement in (original + .005, math.nextafter(original, math.inf)):
            with self.subTest(replacement=replacement):
                manager = ProcessManager(None)
                manager._root_pid = 4242
                manager._root_created = original
                manager._tracked_pids = {4242}
                manager._identities = {4242: original}
                process = mock.Mock(pid=4242)
                process.create_time.return_value = replacement
                with (mock.patch.object(process_runner.psutil, "Process", return_value=process),
                      mock.patch.object(process_runner, "_windows_process_exited", return_value=False)):
                    self.assertEqual(process_runner._reused_since(4242, original), replacement)
                    self.assertIsNone(manager._open_tracked(4242))
                    self.assertFalse(manager._suspend_pid(4242))
                    self.assertTrue(manager._resume_pid(4242))  # original no longer needs resuming
                    manager._terminate_pid(4242, graceful=True)
                    manager._terminate_pid(4242, graceful=False)
                process.suspend.assert_not_called()
                process.resume.assert_not_called()
                process.terminate.assert_not_called()
                process.kill.assert_not_called()
                self.assertEqual(manager._identities[4242], original)
                self.assertIn("was reused", manager.last_error)

    def test_an_exited_process_is_rejected_while_its_handle_is_still_open(self) -> None:
        target = subprocess.Popen([_PYTHON, "-c", "pass"])
        self.addCleanup(target.wait, 10)
        self.addCleanup(target.kill)
        manager = ProcessManager(None)
        manager._root_pid = target.pid
        manager._remember_identity(target.pid)
        manager._root_created = manager.created_at(target.pid)
        target.wait(timeout=10)
        # Popen retains the Windows handle even after wait() returns.
        self.assertIsNone(manager._open_tracked(target.pid))
        self.assertFalse(manager._pid_is_alive(target.pid))

    def test_access_denied_keeps_the_live_process_tracked(self) -> None:
        manager = ProcessManager(None)
        manager._root_pid = os.getpid()
        manager._remember_identity(os.getpid())
        with mock.patch.object(process_runner, "_windows_process_exited", side_effect=PermissionError):
            self.assertIsNotNone(manager._open_tracked(os.getpid()))

    def test_exit_checks_are_nonblocking_and_always_close_the_handle(self) -> None:
        api = mock.Mock()
        api.OpenProcess.return_value = 123
        with mock.patch.object(process_runner, "_windows_process_api", return_value=api):
            for result, expected in ((0, True), (0x102, False)):
                with self.subTest(result=result):
                    api.reset_mock()
                    api.WaitForSingleObject.return_value = result
                    self.assertIs(process_runner._windows_process_exited(42), expected)
                    api.WaitForSingleObject.assert_called_once_with(123, 0)
                    api.CloseHandle.assert_called_once_with(123)
            api.reset_mock()
            api.WaitForSingleObject.return_value = 0xFFFFFFFF
            with self.assertRaises(OSError):
                process_runner._windows_process_exited(42)
            api.CloseHandle.assert_called_once_with(123)

    def test_missing_and_inaccessible_pids_have_distinct_results(self) -> None:
        api = mock.Mock()
        api.OpenProcess.return_value = None
        with (
            mock.patch.object(process_runner, "_windows_process_api", return_value=api),
            mock.patch.object(process_runner.ctypes, "get_last_error", return_value=87) as error,
        ):
            self.assertTrue(process_runner._windows_process_exited(42))
            error.return_value = 5
            with self.assertRaises(OSError):
                process_runner._windows_process_exited(42)
        api.WaitForSingleObject.assert_not_called()
        api.CloseHandle.assert_not_called()


class PidReuseTests(unittest.TestCase):
    def test_a_process_reusing_an_exited_roots_pid_is_left_alone(self) -> None:
        from unittest import mock

        root = subprocess.Popen([_PYTHON, "-c", "import time; time.sleep(1)"])
        manager = ProcessManager(None, poll_interval=60)
        manager.attach(root.pid)
        self.addCleanup(manager.detach)
        root.wait(timeout=10)
        self.assertFalse(manager.is_running)
        self.assertEqual(manager.refresh_process_tree(), set())

        # Windows hands the old root PID to a process started later: the decoy.
        decoy = subprocess.Popen([_PYTHON, "-c", "import time; time.sleep(60)"])
        self.addCleanup(decoy.wait, 10)
        self.addCleanup(decoy.kill)
        real = psutil.Process

        class Reused(real):
            def __init__(self, pid=None):
                super().__init__(decoy.pid if pid == root.pid else pid)

        with mock.patch("src.process_runner.psutil.Process", Reused):
            self.assertFalse(manager.is_running)
            self.assertEqual(manager.refresh_process_tree(), set())
            manager.kill()
        self.assertIn("was reused", manager.last_error)
        self.assertIsNone(decoy.poll(), "the unrelated process was killed")


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

        with mock.patch.object(manager, "_refresh_tracked_pids", side_effect=flaky), \
                self.assertLogs("src.process_runner", "ERROR"):
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
