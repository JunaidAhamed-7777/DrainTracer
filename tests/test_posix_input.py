"""Exercise immediate Linux terminal input using real pseudo-terminals."""

from __future__ import annotations

import io
import os
import select
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401 (adds the repository root to sys.path)

from src import keys
from src.picker_input import PickerKey, PickerKeyboard


@unittest.skipUnless(os.name == "posix", "requires POSIX pseudo-terminals")
class PosixInputTests(unittest.TestCase):
    def setUp(self) -> None:
        import pty
        import termios

        self.termios = termios
        self.master, self.slave = pty.openpty()
        self.addCleanup(self._close_master)
        self.addCleanup(os.close, self.slave)
        self.stream = os.fdopen(os.dup(self.slave), encoding="utf-8")
        self.addCleanup(self.stream.close)
        self.saved = termios.tcgetattr(self.slave)
        patch = mock.patch.object(keys.sys, "stdin", self.stream)
        patch.start()
        self.addCleanup(patch.stop)

    def _close_master(self) -> None:
        if self.master is not None:
            os.close(self.master)
            self.master = None

    def send(self, value: bytes) -> None:
        os.write(self.master, value)
        self.assertTrue(select.select([self.stream], [], [], 1)[0], "terminal input not delivered")

    def test_immediate_no_echo_input_and_restore_after_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "handler failed"):
            with keys.KeyReader() as reader:
                attributes = self.termios.tcgetattr(self.slave)
                self.assertFalse(attributes[3] & self.termios.ICANON)
                self.assertFalse(attributes[3] & self.termios.ECHO)
                self.assertTrue(attributes[3] & self.termios.ISIG)
                self.assertIsNone(reader.read())
                self.send(b"q")
                self.assertEqual(reader.read(), "q")
                raise RuntimeError("handler failed")
        self.assertEqual(self.termios.tcgetattr(self.slave), self.saved)

    def test_fragmented_arrows_and_unicode_are_buffered(self) -> None:
        with keys.KeyReader() as reader:
            for fragment in (b"\x1b", b"[1;"):
                self.send(fragment)
                self.assertIsNone(reader.read())
            self.send(b"5A")
            self.assertEqual(reader.read(), keys.UP)
            value = "日".encode("utf-8")
            self.send(value[:2])
            self.assertIsNone(reader.read())
            self.send(value[2:])
            self.assertEqual(reader.read(), "日")

    def test_escape_resolves_after_short_sequence_timeout(self) -> None:
        with keys.KeyReader() as reader, mock.patch.object(keys.time, "monotonic", return_value=100.0):
            self.send(b"\x1b")
            self.assertIsNone(reader.read())
            with mock.patch.object(keys.time, "monotonic", return_value=100.06):
                self.assertEqual(reader.read(), keys.ESCAPE)

    def test_delayed_sequence_tail_never_becomes_a_dashboard_hotkey(self) -> None:
        with keys.KeyReader() as reader, mock.patch.object(keys.time, "monotonic", return_value=100.0):
            self.send(b"\x1b[")
            self.assertIsNone(reader.read())
            with mock.patch.object(keys.time, "monotonic", return_value=101.0):
                self.assertIsNone(reader.read())
                self.send(b"H")
                self.assertEqual(reader.read(), keys.HOME)

    def test_picker_reads_arrows_text_and_eof_without_enter(self) -> None:
        with PickerKeyboard() as keyboard:
            self.send("\x1b[B日\x7f\x04".encode("utf-8"))
            self.assertEqual(keyboard.read(), PickerKey("down"))
            self.assertEqual(keyboard.read(), PickerKey("text", "日"))
            self.assertEqual(keyboard.read(), PickerKey("backspace"))
            with self.assertRaises(EOFError):
                keyboard.read()
        self.assertEqual(self.termios.tcgetattr(self.slave), self.saved)

    def test_redirected_input_does_not_change_terminal_mode(self) -> None:
        with mock.patch.object(keys.sys, "stdin", io.StringIO("q")):
            with keys.KeyReader() as reader:
                self.assertIsNone(reader.read())
        self.assertEqual(self.termios.tcgetattr(self.slave), self.saved)

    def test_terminal_disconnect_marks_end_of_input(self) -> None:
        with keys.KeyReader() as reader:
            self._close_master()
            self.assertIsNone(reader.read())
            self.assertTrue(reader.eof, "disconnected terminal must end the input loop")

    def _dashboard(self, directory: str):
        from helpers import make_console
        from src.config import MonitorSettings
        from src.dashboard import DashboardApp

        target = Path(directory) / "target.py"
        target.write_text("import time\nwhile True: time.sleep(0.05)\n", encoding="utf-8")
        app = DashboardApp(
            str(target), console=make_console(96, 30),
            report_dir=Path(directory) / "reports", timeout=8,
            settings=MonitorSettings(sample_interval_ms=100, windows=(1.0,)),
        )
        app.FRAME_SECONDS = 0.01
        return app

    def _wait_until(self, predicate, message: str) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    def _run_controlled_dashboard(self, app, controller) -> None:
        errors = []

        def control() -> None:
            try:
                self._wait_until(lambda: app._keys._terminal is not None and app._sample_count >= 2,
                                 "dashboard never accepted live samples in cbreak mode")
                controller()
            except BaseException as exc:
                errors.append(exc)
                os.write(self.master, b"q")

        worker = threading.Thread(target=control, daemon=True)
        worker.start()
        result = app.run()
        worker.join(timeout=4)
        self.assertFalse(worker.is_alive(), "terminal controller failed to finish")
        if errors:
            raise errors[0]
        self.assertEqual(result, 0)
        self.assertFalse(app.timed_out, "dashboard needed the timeout instead of responding to input")
        self.assertFalse(app.manager.has_tracked_processes)
        self.assertFalse(app.detector.is_running)
        self.assertEqual(self.termios.tcgetattr(self.slave), self.saved)

    def test_dashboard_space_export_and_quit_work_without_enter(self) -> None:
        import json
        import psutil

        with tempfile.TemporaryDirectory() as directory:
            app = self._dashboard(directory)

            def control() -> None:
                pid = app.manager.root_pid
                os.write(self.master, b" ")
                self._wait_until(
                    lambda: app.detector.is_paused and psutil.Process(pid).status() == psutil.STATUS_STOPPED,
                    "Space did not suspend the target and pause sampling",
                )
                # Let any sample already in flight reach the main thread.
                time.sleep(0.2)
                before = app._sample_count
                time.sleep(0.2)
                self.assertEqual(app._sample_count, before, "paused sampling added active samples")
                os.write(self.master, b" ")
                self._wait_until(lambda: not app.detector.is_paused and app._sample_count > before,
                                 "Space did not resume sampling")
                self.assertNotEqual(psutil.Process(pid).status(), psutil.STATUS_STOPPED)
                os.write(self.master, b"e")
                self._wait_until(lambda: app._last_html_report is not None,
                                 "E did not export a JSON and HTML report")
                report = json.loads(app._last_report.read_text(encoding="utf-8"))
                self.assertGreaterEqual(len(report["snapshots"]), 2)
                self.assertEqual(report["experiment"]["environment"]["handle_metric"], "file_descriptors")
                self.assertEqual(report["experiment"]["environment"]["private_memory_metric"], "uss")
                self.assertTrue(app._last_html_report.is_file())
                os.write(self.master, b"q")

            self._run_controlled_dashboard(app, control)
            messages = [event[1] for event in app._events]
            self.assertIn("Monitoring paused", messages)
            self.assertIn("Monitoring resumed", messages)

    def test_dashboard_interrupt_cleans_target_and_restores_terminal(self) -> None:
        import signal

        with tempfile.TemporaryDirectory() as directory:
            app = self._dashboard(directory)
            self._run_controlled_dashboard(app, lambda: os.kill(os.getpid(), signal.SIGINT))
            self.assertTrue(any("interrupted by user" in event[1] for event in app._events))


if __name__ == "__main__":
    unittest.main()
