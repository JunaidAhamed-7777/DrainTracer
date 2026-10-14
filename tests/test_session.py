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


class SettingsTests(unittest.TestCase):
    def test_settings_configure_the_detector_and_reach_reports(self) -> None:
        from dataclasses import replace

        from src.config import MonitorSettings
        from src.events import LeakResource
        from src.leak_detector import ResourceThresholds

        settings = replace(
            MonitorSettings(),
            sample_interval_ms=250,
            windows=(5.0, 20.0),
            thresholds={
                **MonitorSettings().thresholds,
                LeakResource.HANDLES: ResourceThresholds(1000, 40),
            },
            profile="ci",
            source=Path("C:/project/.drainrc"),
        )
        session = _Session("C:/targets/worker.exe", console=make_console(), settings=settings,
                           timeout=30.0)
        self.assertEqual(session.detector.sample_interval_ms, 250)
        self.assertEqual(session.detector.window_seconds, (5.0, 20.0))
        record = session.build_report()["settings"]
        self.assertEqual(record["profile"], "ci")
        self.assertEqual(Path(record["config_path"]), Path("C:/project/.drainrc"))
        self.assertEqual(record["windows_seconds"], [5.0, 20.0])
        self.assertEqual(record["thresholds"]["handles"],
                         {"critical_delta": 1000, "critical_slope": 40})
        self.assertEqual(record["timeout_seconds"], 30.0)
        self.assertEqual(record["warmup_seconds"], 0.0)

    def test_reports_describe_a_replaced_detector(self) -> None:
        from src.leak_detector import LeakDetector

        session = _Session("C:/targets/worker.exe", console=make_console())
        session.detector = LeakDetector(session.manager, sample_interval_ms=100,
                                        window_seconds=(4.0,))
        record = session.build_report()["settings"]
        self.assertEqual((record["sample_interval_ms"], record["windows_seconds"]), (100, [4.0]))
        self.assertIsNone(record["profile"])


class WarmupEventTests(unittest.TestCase):
    def _session(self, warmup: float) -> _Session:
        from dataclasses import replace

        from src.config import MonitorSettings

        return _Session(
            "C:/targets/worker.exe",
            console=make_console(),
            settings=replace(MonitorSettings(), warmup=warmup),
        )

    def test_completion_is_logged_once_with_the_baseline(self) -> None:
        from dataclasses import replace

        from helpers import make_snapshot

        session = self._session(5.0)
        session.detector.warmup_baseline = {"private_bytes": 2 * 1024**2, "handles": 120.0,
                                            "threads": 7.0}
        warm = replace(make_snapshot(), warming_up=True)
        session._accept_snapshot(warm)
        session._accept_snapshot(replace(make_snapshot(), warming_up=True, monitoring_paused=True))
        session._accept_snapshot(make_snapshot())
        session._accept_snapshot(make_snapshot())
        messages = [message for _s, message, _l in session._events]
        self.assertEqual(len(messages), 1)
        self.assertEqual(
            messages[0],
            "Warm-up complete; leak detection started from a baseline of "
            "2.0 MB private · 120 handles · 7 threads",
        )

    def test_reports_say_whether_the_warmup_finished(self) -> None:
        session = self._session(5.0)
        record = session.build_report()["warmup"]
        self.assertEqual(record, {"seconds": 5.0, "completed": False, "completed_at": None,
                                  "baseline": None})
        session.detector._warmup_elapsed = 5.0
        session.detector.warmup_completed_at = 1_700_000_000.0
        session.detector.warmup_baseline = {"handles": 3.0}
        record = session.build_report()["warmup"]
        self.assertTrue(record["completed"])
        self.assertEqual(record["baseline"], {"handles": 3.0})
        self.assertIsNotNone(record["completed_at"])
        self.assertIsNone(self._session(0.0).build_report()["warmup"])


class StartupHintTests(unittest.TestCase):
    def test_hints_for_common_failures(self) -> None:
        from src.process_runner import (
            ProcessAccessDeniedError,
            ProcessManagerError,
            ProcessNotFoundError,
        )

        launched = _Session("C:/targets/worker.exe", console=make_console())
        attached = _Session(None, console=make_console(), attach_pid=4242)
        self.assertIn("Check the path", launched.startup_hint(ProcessNotFoundError("x")))
        self.assertIn("Task Manager", attached.startup_hint(ProcessNotFoundError("x")))
        self.assertIn("administrator", launched.startup_hint(ProcessAccessDeniedError("x")))

        bad_image = OSError(8, "bad image")
        bad_image.winerror = 216
        error = ProcessManagerError("Failed to start process")
        error.__cause__ = bad_image
        self.assertIn("not a Windows program", launched.startup_hint(error))
        self.assertIsNone(launched.startup_hint(ProcessManagerError("other")))

    def test_dashboard_startup_panel_shows_the_hint(self) -> None:
        from src.dashboard import DashboardApp

        console = make_console()
        app = DashboardApp("C:/definitely/missing/worker.exe", console=console)
        self.assertEqual(app.run(), 1)
        output = console.file.getvalue()
        self.assertIn("STARTUP ERROR", output)
        self.assertIn("Check the path", output)


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

    def test_ctrl_c_during_termination_kills_the_tree(self) -> None:
        from unittest import mock

        session = _Session(
            str(self._script("import time\ntime.sleep(60)\n")), console=make_console()
        )
        session.start()
        self.addCleanup(session.manager.kill)
        with mock.patch.object(session.manager, "terminate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                session.shutdown()
        self.assertFalse(session.manager.has_tracked_processes)
        self.assertTrue(session.stopped_target)
        self.assertFalse(session.detector.is_running)

    def test_a_failing_shutdown_step_still_ends_the_tree(self) -> None:
        from unittest import mock

        for broken in ("terminate", "stop"):
            with self.subTest(broken=broken):
                session = _Session(
                    str(self._script("import time\ntime.sleep(60)\n")), console=make_console()
                )
                session.start()
                self.addCleanup(session.manager.kill)
                self.addCleanup(session.detector.stop)
                owner = session.manager if broken == "terminate" else session.detector
                with mock.patch.object(owner, broken, side_effect=RuntimeError("stuck")):
                    with self.assertRaises(RuntimeError):
                        session.shutdown()
                self.assertFalse(session.manager.has_tracked_processes)
                self.assertTrue(session.stopped_target)

    def test_the_dashboard_redraws_only_when_something_changes(self) -> None:
        import threading

        from src.dashboard import DashboardApp

        app = DashboardApp(
            str(self._script("import time\ntime.sleep(60)\n")), console=make_console()
        )
        renders: list[int] = []
        original = app._render
        app._render = lambda: (renders.append(1), original())[1]  # type: ignore[method-assign]
        timer = threading.Timer(1.6, setattr, args=(app, "_quit_requested", True))
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(app.run(), 0)
        # 16 frames passed; snapshots arrive twice a second plus a clock tick.
        self.assertGreaterEqual(len(renders), 3)
        self.assertLessEqual(len(renders), 9)
        self.assertFalse(app.manager.has_tracked_processes)

    def test_a_display_bug_does_not_end_the_session(self) -> None:
        import threading

        from src.dashboard import DashboardApp

        app = DashboardApp(
            str(self._script("import time\ntime.sleep(60)\n")), console=make_console()
        )
        original = app._render
        calls: list[int] = []

        def flaky_render():
            calls.append(1)
            if len(calls) > 1:
                raise ValueError("layout bug")
            return original()

        app._render = flaky_render  # type: ignore[method-assign]
        timer = threading.Timer(1.2, setattr, args=(app, "_quit_requested", True))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertLogs("src.dashboard", "ERROR") as logs:
            self.assertEqual(app.run(), 0)
        self.assertEqual(len(logs.records), 1)  # the traceback, once
        failures = [m for _s, m, _l in app._events if "failed: layout bug" in m]
        self.assertEqual(failures, ["Drawing the dashboard failed: layout bug"])
        self.assertGreater(len(calls), 2)  # kept trying to draw
        self.assertFalse(app.manager.has_tracked_processes)

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
