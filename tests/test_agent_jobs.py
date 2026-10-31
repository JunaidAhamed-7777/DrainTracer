"""Native suspended-launch and kill-on-close crash ownership drills."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
import psutil

from src.agent_jobs import (
    AgentProcessManager,
    JobProcess,
    _BasicLimits,
    _ExtendedLimits,
    _job_api,
    _ProcessInfo,
    _StartupInfo,
)
from src.process_runner import ProcessManagerError, _windows_process_exited


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("native drill exceeded its bounded wait")
        time.sleep(0.01)


def exited(pid):
    try:
        return _windows_process_exited(pid)
    except (OSError, psutil.NoSuchProcess):
        return not psutil.pid_exists(pid)


@unittest.skipUnless(os.name == "nt", "Windows job ownership")
class NativeJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_structures_match_windows_sdk_pointer_sized_layouts(self):
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            expected = (104, 24, 64, 144)
        else:
            expected = (68, 16, 48, 112)
        self.assertEqual(tuple(map(ctypes.sizeof, (_StartupInfo, _ProcessInfo, _BasicLimits, _ExtendedLimits))), expected)

    def test_closing_job_stops_root_and_inherited_child_without_console_io(self):
        child_file = self.root / "child.pid"
        code = ("import pathlib,subprocess,sys,time; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "print('discarded'); "
                f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid)); time.sleep(60)")
        process = JobProcess([sys.executable, "-c", code])
        self.addCleanup(process.close)
        wait_for(child_file.exists)
        child_pid = int(child_file.read_text())
        self.assertIsNone(process.poll())
        self.assertTrue(psutil.pid_exists(child_pid))
        process.close()
        wait_for(lambda: exited(process.pid) and exited(child_pid))
        self.assertIsNotNone(process.poll())

    def test_job_assignment_failure_never_runs_unowned_code(self):
        marker = self.root / "must-not-run"
        api = _job_api()
        observed = []
        api.GetProcessId.argtypes = [ctypes.c_void_p]
        api.GetProcessId.restype = ctypes.c_ulong

        def deny(_job, process):
            observed.append(api.GetProcessId(process))
            raise OSError("controlled assignment failure")

        with (mock.patch.object(api, "AssignProcessToJobObject", side_effect=deny),
              self.assertRaisesRegex(OSError, "assignment failure")):
            JobProcess([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"])
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        wait_for(lambda: exited(observed[0]))
        self.assertFalse(marker.exists())

    def test_agent_abrupt_exit_closes_job_and_stops_only_owned_workload(self):
        root_file, child_file = self.root / "root.pid", self.root / "child.pid"
        code = ("import pathlib,subprocess,sys,time; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid)); time.sleep(60)")
        helper = ("from pathlib import Path; import sys,time; from src.agent_jobs import JobProcess; "
                  f"p=JobProcess([sys.executable,'-c',{code!r}]); "
                  f"Path({str(root_file)!r}).write_text(str(p.pid)); time.sleep(60)")
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(unrelated.wait, 5)
        self.addCleanup(unrelated.kill)
        agent = subprocess.Popen([sys.executable, "-c", helper], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(lambda: root_file.exists() and child_file.exists())
            root_pid, child_pid = int(root_file.read_text()), int(child_file.read_text())
            agent.kill()
            agent.communicate(timeout=5)
            wait_for(lambda: exited(root_pid) and exited(child_pid))
            self.assertIsNone(unrelated.poll())
        finally:
            if agent.poll() is None:
                agent.kill()
            agent.communicate(timeout=5)

    def test_adapter_preserves_process_manager_cleanup_and_rejects_batch(self):
        script = self.root / "sleep.py"
        script.write_text("import time; time.sleep(60)")
        manager = AgentProcessManager(str(script))
        self.addCleanup(manager.kill)
        pid = manager.start()
        self.assertTrue(manager.is_running)
        manager.terminate(timeout=0.1)
        wait_for(lambda: exited(pid))
        self.assertFalse(manager.has_tracked_processes)
        self.assertIsNone(manager._owned_job)
        with self.assertRaisesRegex(ProcessManagerError, "wrap batch"):
            manager._spawn_process('"cmd.exe" /c example.cmd', {})


if __name__ == "__main__":
    unittest.main()
