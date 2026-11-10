"""Termination signals and a closed console still stop the target and export a report."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (adds the repository root to sys.path)
import psutil
from rich.console import Console

from src import main as cli
from src.headless import HeadlessRunner, _BestEffortOutput

REPOSITORY = Path(__file__).resolve().parents[1]


class _ClosedPipe(io.StringIO):
    """A stream whose reader has gone away after ``allowed`` writes."""

    def __init__(self, allowed: int) -> None:
        super().__init__()
        self.allowed = allowed

    def write(self, text: str) -> int:
        if self.allowed <= 0:
            raise OSError(22, "Invalid argument")
        self.allowed -= 1
        return super().write(text)


class BestEffortOutputTests(unittest.TestCase):
    def test_writes_after_a_failure_are_dropped_without_raising(self) -> None:
        stream = _BestEffortOutput(_ClosedPipe(allowed=1))
        self.assertEqual(stream.write("first\n"), 6)
        self.assertEqual(stream.write("second\n"), 7)
        self.assertTrue(stream.lost)
        self.assertEqual(stream.write("third\n"), 6)
        stream.flush()

    def test_runner_records_lost_output_once_and_keeps_going(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            console = Console(file=_ClosedPipe(allowed=1), width=120, no_color=True)
            runner = HeadlessRunner(None, console=console, attach_pid=os.getpid(),
                                    report_dir=Path(folder))
            for index in range(3):
                runner._append_event(f"event {index}", "warn")
            notes = [message for _stamp, message, _level in runner._events
                     if "Console output closed" in message]
            self.assertEqual(len(notes), 1)


def _launch_headless(folder: Path, *, seconds: int, timeout: str, stdout=subprocess.PIPE):
    marker = folder / "target.pid"
    target = folder / "sleeper.py"
    target.write_text(
        "import os, time\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        f"time.sleep({seconds})\n", encoding="utf-8")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    monitor = subprocess.Popen(
        [sys.executable, "-B", str(REPOSITORY / "src" / "main.py"), "--headless", "--no-config",
         "--target", str(target), "--timeout", timeout, "--report-dir", str(folder / "reports")],
        cwd=folder, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT,
        creationflags=flags, start_new_session=sys.platform != "win32")
    return monitor, marker


def _target_pid(monitor: subprocess.Popen, marker: Path) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and monitor.poll() is None:
        if marker.exists() and marker.read_text(encoding="utf-8"):
            return int(marker.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError("the monitored target never started")


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


class TerminationTests(unittest.TestCase):
    def _cleanup(self, monitor: subprocess.Popen, pid_holder: list[int]) -> None:
        if monitor.poll() is None:
            monitor.kill()
            monitor.wait(timeout=10)
        for pid in pid_holder:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        if monitor.stdout is not None:
            monitor.stdout.close()

    def test_termination_signal_stops_the_target_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            monitor, marker = _launch_headless(folder, seconds=60, timeout="50s")
            started: list[int] = []
            self.addCleanup(self._cleanup, monitor, started)
            started.append(_target_pid(monitor, marker))
            time.sleep(1.0)
            if sys.platform == "win32":
                os.kill(monitor.pid, signal.CTRL_BREAK_EVENT)
            else:
                monitor.send_signal(signal.SIGTERM)
            output = monitor.communicate(timeout=30)[0].decode("utf-8", "replace")
            self.assertEqual(monitor.returncode, 130, output)
            self.assertIn("RESULT", output)
            reports = sorted(path.suffix for path in (folder / "reports").rglob("leak_report_*"))
            self.assertEqual(reports, [".html", ".json"], output)
            deadline = time.monotonic() + 10
            while not _gone(started[0]) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(_gone(started[0]), "the launched target outlived the monitor")

    def test_closed_output_pipe_still_exports_and_reports_the_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            monitor, marker = _launch_headless(folder, seconds=2, timeout="20s")
            started: list[int] = []
            self.addCleanup(self._cleanup, monitor, started)
            started.append(_target_pid(monitor, marker))
            monitor.stdout.readline()
            monitor.stdout.close()
            self.assertEqual(monitor.wait(timeout=30), 0)
            reports = sorted(path.suffix for path in (folder / "reports").rglob("leak_report_*"))
            self.assertEqual(reports, [".html", ".json"])


class HandlerInstallationTests(unittest.TestCase):
    def test_handlers_are_restored_after_main(self) -> None:
        number = signal.SIGBREAK if sys.platform == "win32" else signal.SIGTERM
        before = signal.getsignal(number)
        with mock.patch.object(cli, "_main", return_value=0):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertEqual(signal.getsignal(number), before)

    @unittest.skipIf(sys.platform == "win32", "SIGHUP is POSIX-only")
    def test_ignored_hangup_stays_ignored(self) -> None:
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        restored = cli._install_termination_handlers()
        self.addCleanup(cli._restore_handlers, restored)
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)

    def test_a_handled_signal_raises_an_interrupt(self) -> None:
        number = signal.SIGBREAK if sys.platform == "win32" else signal.SIGTERM
        restored = cli._install_termination_handlers()
        self.addCleanup(cli._restore_handlers, restored)
        handler = signal.getsignal(number)
        self.assertTrue(callable(handler))
        with self.assertRaises(KeyboardInterrupt):
            handler(number, None)


if __name__ == "__main__":
    unittest.main()
