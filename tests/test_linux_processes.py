"""Linux launch, signal handling, orphan discovery, and process identity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401 (adds the repository root to sys.path)
import psutil

from src import process_runner
from src.process_runner import ProcessManager, ProcessManagerError, ProcessNotFoundError


def _wait(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.025)
    return bool(predicate())


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


class LinuxCommandTests(unittest.TestCase):
    def test_windows_batch_files_have_a_clear_linux_error(self) -> None:
        with mock.patch.object(process_runner.sys, "platform", "linux"):
            for suffix in (".bat", ".cmd"):
                with self.subTest(suffix=suffix), self.assertRaisesRegex(
                    ProcessManagerError, "Windows batch files require Windows"
                ):
                    ProcessManager("target" + suffix)._build_command()


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process integration")
class LinuxProcessTests(unittest.TestCase):
    def _launch(self, script: Path, source: str, *, args=None, executable=False):
        script.write_text(source, encoding="utf-8")
        if executable:
            script.chmod(0o700)
        manager = ProcessManager(str(script), args=args, poll_interval=60)
        manager.start()
        self.addCleanup(manager.kill)
        return manager

    def test_executable_shebang_receives_arguments_without_shell_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            output = folder / "received.json"
            arguments = [str(output), "a b", "$(touch unexpected)", "x&y", "", "café"]
            manager = self._launch(
                folder / "script with spaces",
                f"#!{sys.executable}\nimport json, sys\n"
                "with open(sys.argv[1], 'w') as stream:\n"
                "    json.dump(sys.argv[2:], stream)\n",
                args=arguments,
                executable=True,
            )
            self.assertTrue(_wait(lambda: manager.exit_code is not None))
            self.assertEqual(manager.exit_code, 0)
            self.assertEqual(json.loads(output.read_text()), arguments[1:])

    def test_launch_starts_a_new_session_and_keeps_the_monitors_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            monitor_session = os.getsid(0)
            manager = self._launch(
                Path(temp) / "worker.py", "import time\ntime.sleep(60)\n"
            )
            self.assertEqual(os.getsid(manager.root_pid), manager.root_pid)
            self.assertEqual(os.getsid(0), monitor_session)

    def test_linux_binary_can_be_launched(self) -> None:
        manager = ProcessManager("/bin/sleep", args=["60"])
        manager.start()
        self.addCleanup(manager.kill)
        self.assertTrue(manager.is_running)
        manager.kill()
        self.assertFalse(manager.has_tracked_processes)
        self.assertIsNotNone(manager.exit_code)

    def test_pause_resume_and_graceful_termination_use_real_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            ready, stopped = folder / "ready", folder / "terminated"
            manager = self._launch(
                folder / "worker.py",
                "import pathlib, signal, sys, time\n"
                "def terminate(*_):\n"
                f"    pathlib.Path({str(stopped)!r}).write_text('SIGTERM')\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, terminate)\n"
                f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
                "while True: time.sleep(0.05)\n",
            )
            self.assertTrue(_wait(ready.exists))
            pid = manager.root_pid
            self.assertTrue(manager.pause())
            self.assertTrue(_wait(lambda: psutil.Process(pid).status() == psutil.STATUS_STOPPED))
            self.assertTrue(manager.resume())
            self.assertTrue(_wait(lambda: psutil.Process(pid).status() != psutil.STATUS_STOPPED))
            self.assertTrue(manager.pause())
            manager.terminate(timeout=2)
            self.assertEqual(stopped.read_text(), "SIGTERM")
            self.assertEqual(manager.exit_code, 0)
            self.assertFalse(manager.has_tracked_processes)

    def test_surviving_children_are_discovered_after_the_root_was_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            child_file, grandchild_file = folder / "child", folder / "grandchild"
            child_source = (
                "import pathlib, subprocess, sys, time\n"
                "time.sleep(0.5)\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(grandchild_file)!r}).write_text(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            manager = self._launch(
                folder / "root.py",
                "import pathlib, subprocess, sys\n"
                f"child = subprocess.Popen([sys.executable, '-c', {child_source!r}])\n"
                f"pathlib.Path({str(child_file)!r}).write_text(str(child.pid))\n",
            )
            self.assertTrue(_wait(lambda: manager.exit_code is not None))
            child = int(child_file.read_text())
            self.assertIn(child, manager.refresh_process_tree())
            self.assertTrue(_wait(lambda: grandchild_file.exists() and grandchild_file.stat().st_size))
            grandchild = int(grandchild_file.read_text())
            self.assertIn(grandchild, manager.refresh_process_tree())
            self.assertEqual(manager._termination_order({child, grandchild}), [grandchild, child])
            self.assertTrue(manager.pause(), "surviving descendants must remain controllable")
            self.assertTrue(_wait(lambda: psutil.Process(child).status() == psutil.STATUS_STOPPED))
            self.assertTrue(manager.resume())
            manager.kill()
            self.assertFalse(manager.has_tracked_processes)
            self.assertTrue(_wait(lambda: not _alive(child) and not _alive(grandchild)))

    def test_zombies_are_exited_even_before_the_parent_reaps_them(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(child.wait, 10)
        self.assertTrue(_wait(lambda: psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE))
        manager = ProcessManager(None)
        with self.assertRaises(ProcessNotFoundError):
            manager.attach(child.pid)
        self.assertIsNone(manager._open_tracked(child.pid))

    def test_a_reused_session_leader_is_never_adopted(self) -> None:
        manager = ProcessManager(None)
        manager._linux_session_id = 123
        manager._root_created = 100.0
        with mock.patch.object(process_runner, "_reused_since", return_value=100.1), \
                mock.patch.object(process_runner.os, "getsid") as getsid:
            self.assertEqual(manager._linux_session_members({123: 1}), set())
        getsid.assert_not_called()
        self.assertIsNone(manager._linux_session_id)

    def test_pid_reuse_within_one_second_is_rejected(self) -> None:
        manager = ProcessManager(None)
        manager._identities[123] = 100.0
        proc = mock.Mock()
        proc.is_running.return_value = True
        proc.status.return_value = psutil.STATUS_RUNNING
        proc.create_time.return_value = 100.1
        with mock.patch.object(process_runner.psutil, "Process", return_value=proc):
            self.assertIsNone(manager._open_tracked(123))
        self.assertIn("was reused", manager.last_error)


if __name__ == "__main__":
    unittest.main()
