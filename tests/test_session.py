"""MonitorSession lifecycle against real short-lived processes."""

from __future__ import annotations

import tempfile
import time
import unittest
from unittest import mock
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


class ReportExportTests(unittest.TestCase):
    def test_html_interruption_is_recorded_in_saved_json_before_propagating(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as temp:
            session = _Session("worker.exe", console=make_console(), report_dir=temp)
            with mock.patch("src.session.write_html_report", side_effect=KeyboardInterrupt), \
                    self.assertRaises(KeyboardInterrupt):
                session.export_log()
            saved = json.loads(session._last_report.read_text(encoding="utf-8"))
            self.assertIn("HTML report interrupted", saved["outcome"]["monitor_errors"])
            self.assertEqual(saved["outcome"], session._last_report_data["outcome"])
            self.assertIsNone(session._last_html_report)

    def test_html_failure_is_recorded_in_json_and_cached_report(self) -> None:
        import json

        from helpers import make_snapshot

        for failure in (OSError("disk full"), RuntimeError("render failed")):
            with self.subTest(failure=str(failure)), tempfile.TemporaryDirectory() as temp:
                session = _Session("worker.exe", console=make_console(), report_dir=temp)
                session._accept_snapshot(make_snapshot())
                with mock.patch("src.session.write_html_report", side_effect=failure):
                    path = session.export_log()
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["outcome"]["monitor_errors"],
                                 [f"HTML report failed: {failure}"])
                self.assertEqual(saved["outcome"], session._last_report_data["outcome"])
                self.assertEqual(path.stem, f"leak_report_{session._last_report_id}")
                self.assertIsNone(session._last_html_report)

    def test_missing_html_assets_are_recorded_in_json(self) -> None:
        import json

        from src import html_report

        with tempfile.TemporaryDirectory() as temp:
            session = _Session("worker.exe", console=make_console(), report_dir=temp)
            html_report._load_frame.cache_clear()
            self.addCleanup(html_report._load_frame.cache_clear)
            with mock.patch.object(html_report, "_FRAME_DIR", Path(temp) / "missing"):
                path = session.export_log()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(any(error.startswith("HTML report failed:")
                                for error in saved["outcome"]["monitor_errors"]))

    def test_failed_atomic_update_preserves_first_json(self) -> None:
        import json
        import os

        real_replace = os.replace
        replacements = 0

        def fail_second_replace(source, destination):
            nonlocal replacements
            replacements += 1
            if replacements == 2:
                raise OSError("disk full while updating outcome")
            return real_replace(source, destination)

        with tempfile.TemporaryDirectory() as temp:
            session = _Session("worker.exe", console=make_console(), report_dir=temp)
            with mock.patch("src.session.write_html_report", side_effect=OSError("no HTML")), \
                    mock.patch("src.report.os.replace", side_effect=fail_second_replace):
                path = session.export_log()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["outcome"]["monitor_errors"], [])
            self.assertIn("HTML report failed: no HTML", session.monitor_errors)
            self.assertTrue(any(error.startswith("Could not record HTML export failure in JSON:")
                                for error in session.monitor_errors))
            self.assertEqual(list(Path(temp).rglob("*.tmp")), [])
            self.assertEqual(saved["report_id"], session._last_report_id)


class SettingsTests(unittest.TestCase):
    def test_selected_identity_reaches_the_manager_before_sampling(self) -> None:
        from helpers import make_snapshot

        from src.dashboard import DashboardApp

        session = DashboardApp(None, attach_pid=4242, attach_create_time=10.0,
                               console=make_console())
        with (
            mock.patch.object(session.manager, "attach") as attach,
            mock.patch.object(session.detector, "sample_once", return_value=make_snapshot()),
            mock.patch.object(session.detector, "start"),
        ):
            session.start()
        attach.assert_called_once_with(4242, expected_create_time=10.0)

    def test_rejected_selection_never_starts_sampling(self) -> None:
        from src.process_runner import ProcessNotFoundError

        session = _Session(None, attach_pid=4242, attach_create_time=10.0,
                           console=make_console())
        with (
            mock.patch.object(session.manager, "attach", side_effect=ProcessNotFoundError("changed")),
            mock.patch.object(session.detector, "sample_once") as sample,
            mock.patch.object(session.detector, "start") as start,
            self.assertRaises(ProcessNotFoundError),
        ):
            session.start()
        sample.assert_not_called()
        start.assert_not_called()

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

    def test_reports_record_redacted_experiment_provenance(self) -> None:
        from dataclasses import replace

        from src.config import MonitorSettings

        settings = replace(
            MonitorSettings(), workload_id="import-1k", build="1.4.2", operations=1000.0,
            note="token=abc123 nightly", budgets={"handles": 2000.0},
        )
        session = _Session("C:/targets/worker.exe", args=["--password", "hunter2"],
                           console=make_console(), settings=settings)
        record = session.build_report()["experiment"]
        self.assertEqual((record["workload_id"], record["build"], record["operations"]),
                         ("import-1k", "1.4.2", 1000.0))
        self.assertEqual(record["note"], "token=<redacted> nightly")
        self.assertEqual(record["arguments"]["count"], 2)
        self.assertNotIn("hunter2", repr(session.build_report()))
        self.assertEqual(record["budgets"], {"handles": 2000.0})
        self.assertEqual(record["sampling"]["interval_ms"], 500)
        self.assertEqual(record["environment"]["draintracer_version"], "test")
        self.assertIsNone(record["duration_seconds"])  # never started

    def test_reports_describe_a_replaced_detector(self) -> None:
        from src.leak_detector import LeakDetector

        session = _Session("C:/targets/worker.exe", console=make_console())
        session.detector = LeakDetector(session.manager, sample_interval_ms=100,
                                        window_seconds=(4.0,))
        record = session.build_report()["settings"]
        self.assertEqual((record["sample_interval_ms"], record["windows_seconds"]), (100, [4.0]))
        self.assertIsNone(record["profile"])

    def test_short_basic_credentials_never_reach_json_or_html_exports(self) -> None:
        import json

        from src.config import MonitorSettings

        settings = MonitorSettings(
            workload_id="Authorization: Basic dTpw",
            build="basic dTo=",
            note="Basic Og==. basic test",
        )
        with tempfile.TemporaryDirectory() as root:
            session = _Session("C:/targets/worker.exe", console=make_console(),
                               settings=settings, report_dir=root)
            json_path = session.export_log()
            json_text = json_path.read_text(encoding="utf-8")
            experiment = json.loads(json_text)["experiment"]
            self.assertEqual(experiment["workload_id"], "Authorization: Basic <redacted>")
            self.assertEqual(experiment["build"], "basic <redacted>")
            self.assertEqual(experiment["note"], "Basic <redacted>. basic test")
            self.assertIsNotNone(session._last_html_report)
            html_text = session._last_html_report.read_text(encoding="utf-8")
            for token in ("dTpw", "dTo=", "Og=="):
                with self.subTest(token=token):
                    self.assertNotIn(token, json_text)
                    self.assertNotIn(token, html_text)
            self.assertIn("&lt;redacted&gt;", html_text)
            self.assertIn("basic test", html_text)


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


class StreamTests(unittest.TestCase):
    def test_event_eviction_counts_are_exact_without_changing_order(self) -> None:
        class TinyEventSession(_Session):
            EVENT_LIMIT = 3

        session = TinyEventSession("worker.exe", console=make_console())
        for index in range(8):
            session._append_event(f"event {index}")
        report = session.build_report()
        self.assertEqual(report["event_counts"], {"observed": 8, "retained": 3, "dropped": 5})
        self.assertEqual([event["message"] for event in report["event_log"]],
                         ["event 5", "event 6", "event 7"])
        self.assertEqual(len(session._events), 3)
        self.assertEqual(report["sample_count"], 0)

    def test_a_stalled_main_thread_loses_no_samples(self) -> None:
        from helpers import make_alert, make_snapshot

        from src.events import LeakSeverity

        session = _Session("C:/targets/worker.exe", console=make_console())
        stream = session.detector.stream
        stream.publish(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        for _ in range(600):  # five minutes of samples at 500 ms
            stream.publish(make_snapshot())
        self.assertEqual(session.poll(), 601)
        self.assertEqual(session.worst_severity, LeakSeverity.HIGH)


class StartupHintTests(unittest.TestCase):
    @mock.patch("src.session.sys.platform", "win32")
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

    @mock.patch("src.session.sys.platform", "linux")
    def test_linux_hints_explain_permissions_and_executable_format(self) -> None:
        import errno
        from src.process_runner import (
            ProcessAccessDeniedError, ProcessManagerError, ProcessNotFoundError,
        )

        launched = _Session("/opt/worker", console=make_console())
        attached = _Session(None, console=make_console(), attach_pid=4242)
        self.assertIn("ps or top", attached.startup_hint(ProcessNotFoundError("gone")))
        self.assertIn("execute permission", launched.startup_hint(ProcessAccessDeniedError("denied")))
        error = ProcessManagerError("bad executable")
        error.__cause__ = OSError(errno.ENOEXEC, "Exec format error")
        self.assertIn("#! interpreter", launched.startup_hint(error))

    @mock.patch("src.session.sys.platform", "linux")
    def test_linux_counter_definitions_are_recorded_in_events(self) -> None:
        session = _Session("/opt/worker", console=make_console())
        session._log_start()
        self.assertTrue(any("USS" in message and "file descriptors" in message
                            for _stamp, message, _level in session._events))

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

    def test_a_root_that_exits_by_itself_keeps_its_exit_code(self) -> None:
        # The root starts a helper, waits for it to be discovered, and fails.
        session = _Session(
            str(self._script(
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "time.sleep(1.5)\n"
                "raise SystemExit(3)\n"
            )),
            console=make_console(),
        )
        session.start()
        self.addCleanup(session.manager.kill)
        self.assertTrue(_wait_for(lambda: not session.manager.is_running), "root never exited")
        self.assertTrue(session.manager.has_tracked_processes, "the helper was not tracked")
        session.shutdown()
        self.assertFalse(session.manager.has_tracked_processes)
        self.assertFalse(session.stopped_target)
        self.assertEqual(
            session.outcome_text(),
            "launched, exited with code 3; DrainTracer stopped the processes it left running",
        )
        self.assertEqual(session.build_report()["target_exit_code"], 3)
        self.assertTrue(any("the target left running" in m for _s, m, _l in session._events))

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
        # Subtracting monotonic timestamps can round just above the timeout.
        self.assertLessEqual(remaining, 0.3 + 1e-9)
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
        from contextlib import nullcontext
        from unittest import mock

        from src.dashboard import DashboardApp

        app = DashboardApp(
            str(self._script("import time\ntime.sleep(60)\n")), console=make_console()
        )
        clock = [0.0]
        renders: list[float] = []

        def render():
            renders.append(clock[0])
            return "frame"

        def advance(seconds):
            clock[0] = round(clock[0] + seconds, 3)

        # Exercise an update, an idle clock tick, and Q independently of the
        # machine's startup/sample/render speed. No background timer races start().
        with (
            mock.patch.object(app, "start"),
            mock.patch.object(app, "shutdown") as shutdown,
            mock.patch.object(app, "poll", side_effect=lambda: clock[0] == 0.3) as poll,
            mock.patch.object(app, "_read_key", side_effect=lambda: "q" if clock[0] >= 1.6 else None),
            mock.patch.object(app, "_render", side_effect=render),
            mock.patch.object(app.console, "status", return_value=nullcontext()),
            mock.patch("src.dashboard.Live"),
            mock.patch("src.dashboard.time.monotonic", side_effect=lambda: clock[0]),
            mock.patch("src.dashboard.time.sleep", side_effect=advance),
        ):
            self.assertEqual(app.run(), 0)
        self.assertEqual(renders, [0.0, 0.3, 1.3, 1.6])
        self.assertEqual(poll.call_count, 17)
        shutdown.assert_called_once_with()

    def test_a_display_bug_does_not_end_the_session(self) -> None:
        import threading

        from src.dashboard import DashboardApp

        app = DashboardApp(
            str(self._script("import time\ntime.sleep(60)\n")), console=make_console()
        )
        calls: list[int] = []

        def broken_render():
            calls.append(1)
            if len(calls) >= 3:
                app._quit_requested = True
            raise ValueError("layout bug")

        # Broken from the very first frame.
        app._render = broken_render  # type: ignore[method-assign]
        # Bound a failed retry loop without making redraw count depend on startup speed.
        timer = threading.Timer(5.0, setattr, args=(app, "_quit_requested", True))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertLogs("src.dashboard", "ERROR") as logs:
            self.assertEqual(app.run(), 0)
        self.assertEqual(len(logs.records), 1)  # the traceback, once
        failures = [m for _s, m, _l in app._events if "failed: layout bug" in m]
        self.assertEqual(failures, ["Drawing the dashboard failed: layout bug"])
        self.assertGreater(len(calls), 2)  # kept trying to draw
        output = app.console.file.getvalue()
        self.assertIn("could not draw its dashboard: layout bug", output)
        self.assertIn("SESSION SUMMARY", output)
        self.assertFalse(app.manager.has_tracked_processes)

    def test_a_summary_bug_still_ends_cleanly_with_the_report_path(self) -> None:
        import threading
        from unittest import mock

        from src.dashboard import DashboardApp

        app = DashboardApp(
            str(self._script("import time\ntime.sleep(60)\n")), console=make_console()
        )
        app._last_report = Path(self.temp.name) / "leak_report_x.json"
        timer = threading.Timer(0.5, setattr, args=(app, "_quit_requested", True))
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(app, "render_summary", side_effect=ValueError("summary bug")), \
                self.assertLogs("src.dashboard", "ERROR"):
            self.assertEqual(app.run(), 0)
        output = app.console.file.getvalue()
        self.assertIn("Session summary unavailable: summary bug", output)
        self.assertIn("leak_report_x.json", output)
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


class PartialPauseCleanupTests(unittest.TestCase):
    def test_shutdown_retries_partial_pause_without_losing_suspended_identities(self) -> None:
        from src.process_runner import ProcessManager, ProcessManagerError

        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                session = _Session(None, attach_pid=101, console=make_console())
                manager = session.manager
                manager._root_pid = 101
                manager._attached = True
                manager._tracked_pids = {101, 102}
                manager._identities = {101: 10.0, 102: 20.0}
                manager._depths = {101: 0, 102: 1}
                resume_attempts = 0

                def resume_known_identity(pid, manager=manager, recovered=recovered):
                    nonlocal resume_attempts
                    resume_attempts += 1
                    self.assertEqual(pid, 101)
                    self.assertEqual(manager._identities[pid], 10.0)
                    return recovered and resume_attempts == 3

                with (
                    mock.patch.object(ProcessManager, "is_running", new_callable=mock.PropertyMock,
                                      return_value=True),
                    mock.patch.object(manager, "_refresh_tracked_pids"),
                    mock.patch.object(manager, "_start_poll_thread"),
                    mock.patch.object(manager, "_suspend_pid", side_effect=[True, False]),
                    mock.patch.object(manager, "_resume_pid", side_effect=resume_known_identity),
                    mock.patch.object(session.detector, "stop"),
                ):
                    self.assertFalse(manager.pause())
                    self.assertTrue(manager.is_paused)
                    with self.assertRaisesRegex(ProcessManagerError, "Cannot detach"):
                        session.shutdown()
                self.assertEqual(resume_attempts, 3)
                if recovered:
                    self.assertFalse(manager.is_paused)
                    self.assertFalse(manager.has_tracked_processes)
                else:
                    self.assertTrue(manager.is_paused)
                    self.assertEqual(manager._suspended_pids, {101})
                    self.assertEqual(manager._identities, {101: 10.0, 102: 20.0})


class CoverageTests(unittest.TestCase):
    def _session(self) -> _Session:
        return _Session("C:/targets/worker.exe", console=make_console())

    def test_access_loss_and_recovery_are_logged_and_reported(self) -> None:
        from helpers import make_denied, make_process, make_snapshot

        session = self._session()
        full = (make_process(100), make_process(200, ppid=100))
        partial = (make_process(100), make_denied(200, ppid=100))
        for processes in (full, partial, partial, full):
            session._accept_snapshot(make_snapshot(processes=processes, measured_coverage=True))
        messages = [message for _s, message, _l in session._events]
        self.assertTrue(any(m.startswith("Cannot read PID 200") for m in messages))
        self.assertTrue(any(m.startswith("PID 200 is readable again") for m in messages))
        self.assertTrue(session.partial_coverage)
        self.assertEqual(session.coverage_text(), "1 process unreadable (PID 200) in 2 of 4 samples")
        record = session.build_report()["coverage"]
        self.assertEqual(record["samples"], 4)
        self.assertEqual(record["metrics"]["private_bytes"]["partial_samples"], 2)
        self.assertEqual(record["unreadable_pids"], [200])

    def test_an_empty_tree_is_not_missing_coverage(self) -> None:
        from dataclasses import replace

        from helpers import coverage_of, make_snapshot

        session = self._session()
        session._accept_snapshot(replace(make_snapshot(), processes=(), coverage=coverage_of(())))
        self.assertFalse(session.partial_coverage)
        self.assertIsNone(session.coverage_text())

    def test_an_unreadable_tree_is_inconclusive_for_that_reason(self) -> None:
        from helpers import make_denied, make_snapshot

        session = self._session()
        session._accept_snapshot(make_snapshot(processes=(make_denied(100),), measured_coverage=True))
        self.assertIn("no process in the tree could be measured", session.inconclusive_reason())

    def test_outcomes_are_reported_separately(self) -> None:
        from unittest import mock

        from helpers import make_denied, make_process, make_snapshot

        session = self._session()
        session._accept_snapshot(make_snapshot(metadata={"sampling_error": "boom"}))
        session._accept_snapshot(make_snapshot(
            processes=(make_process(100), make_denied(200)), measured_coverage=True))
        session.detector._detection_started = True
        with mock.patch.object(type(session.manager), "exit_code",
                               new_callable=mock.PropertyMock, return_value=3):
            outcome = session.build_report()["outcome"]
            self.assertTrue(session.target_failed)
        self.assertEqual(outcome["target"]["state"], "exited")
        self.assertEqual(outcome["target"]["exit_code"], 3)
        self.assertTrue(outcome["target"]["failed"])
        self.assertEqual(outcome["measurement"]["result"], "partial")
        self.assertIsNone(outcome["measurement"]["reason"])
        self.assertEqual(outcome["monitor_errors"], ["sampling error: boom"])
        session._stopped_target = True
        self.assertFalse(session.target_failed)


class ProcessLifecycleTests(unittest.TestCase):
    """Lifecycle and attribution from detector snapshots, as the session sees them."""

    MB = 1024 * 1024

    def _run(self, trees) -> _Session:
        from unittest import mock

        session = _Session("C:/targets/worker.exe", console=make_console())
        detector = session.detector
        detector.window_seconds = (10.0,)
        detector.classifier = mock.Mock(available=False)
        for index, processes in enumerate(trees):
            with mock.patch.object(detector, "_sample_processes", return_value=list(processes)),                     mock.patch("src.leak_detector.time.time", return_value=1000.0 + index * 0.5),                     mock.patch("src.leak_detector.time.monotonic", return_value=50.0 + index * 0.5):
                session._accept_snapshot(detector._collect_snapshot())
        return session

    def _worker(self, pid: int, private_mb: float = 10):
        from dataclasses import replace

        from helpers import make_process

        return replace(
            make_process(pid, ppid=100, private=int(private_mb * self.MB)), create_time=float(pid)
        )

    def _messages(self, session: _Session) -> list[str]:
        return [message for _s, message, _l in session._events]

    def test_starts_and_exits_are_logged_and_bursts_coalesced(self) -> None:
        from helpers import make_process

        root = make_process(100, name="parent.exe")
        burst = [self._worker(pid) for pid in range(1, 7)]
        session = self._run([
            [root, self._worker(1)],
            [root, self._worker(1), self._worker(2)],
            [root, self._worker(2)],
            [root, *burst],
            [root],
        ])
        messages = self._messages(session)
        self.assertIn("Process started: worker.exe PID 2", messages)
        self.assertIn("Process exited: worker.exe PID 1", messages)
        self.assertIn("6 processes exited (worker.exe x6)", messages)
        restarted = [m for m in messages if m.startswith("Process restarted")]
        self.assertEqual(len(restarted), 1)  # one exit can be replaced once
        self.assertIn("4 processes started (worker.exe x4)", messages)
        counts = session.process_counts
        self.assertEqual((counts.live, counts.exited), (1, 7))

        report = session.build_report()
        self.assertEqual(report["processes"]["counts"]["exited"], 7)
        self.assertEqual(len(report["processes"]["exited"]), 7)
        self.assertEqual(report["processes"]["live"][0]["pid"], 100)
        self.assertNotIn("process_summaries", report["snapshots"][-1])
        stored = list(session._snapshot_history)[-1]
        self.assertEqual(stored.exited_processes, ())  # history stays lean

    def test_alert_summaries_name_the_growing_worker(self) -> None:
        from helpers import make_process

        root = make_process(100, name="parent.exe")
        trees = [[root, self._worker(1), self._worker(2, 30 + index * 4)] for index in range(21)]
        session = self._run(trees)
        line = next(m for m in self._messages(session) if m.startswith("Memory leak signal"))
        self.assertIn(" MB private in 10s, mostly worker.exe PID 2 ", line)
        report = session.build_report()
        self.assertEqual(report["snapshots"][-1]["alerts"][0]["contributors"][0]["pid"], 2)

    def test_new_worker_growth_is_worded_as_capacity(self) -> None:
        from helpers import make_process

        root = make_process(100, name="parent.exe")
        trees = [
            [root, *[self._worker(10 + n, 20) for n in range(index // 5 + 1)]]
            for index in range(21)
        ]
        session = self._run(trees)
        line = next(m for m in self._messages(session) if m.startswith("Memory leak signal"))
        self.assertIn("mostly new worker processes", line)


if __name__ == "__main__":
    unittest.main()
