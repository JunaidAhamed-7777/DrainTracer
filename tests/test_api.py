"""Supported API contracts against real owned and attached processes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psutil

from src.api import (
    CommandTimeoutError,
    Session,
    SessionEvent,
    SessionOptions,
    SessionStatus,
    Subscription,
)
from src.events import LeakSeverity
from src.report_contract import validate_report


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "steady.py"
        self.target.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    def options(self, **changes):
        return SessionOptions(sample_interval_ms=100, windows_seconds=(1,),
                              report_dir=self.root / "reports", export_on_close=False, **changes)

    def test_validation_precedes_any_process_start_and_freezes_inputs(self):
        for changes in ({"sample_interval_ms": 1}, {"windows_seconds": ()},
                        {"timeout_seconds": float("nan")}, {"allow_kill": 1},
                        {"budgets": {"unknown": 1}}, {"report_dir": "bad\x00path"}):
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                SessionOptions(**changes)
        budgets = {"handles": 1000}
        options = SessionOptions(budgets=budgets)
        budgets["handles"] = 1
        self.assertEqual(options.budgets["handles"], 1000)
        with self.assertRaises(TypeError):
            options.budgets["handles"] = 1
        with mock.patch("subprocess.Popen") as spawn:
            session = Session.launch(self.target, options=self.options())
            with self.assertRaises(AttributeError):
                session.options = options
            self.assertEqual(session.status.state, "created")
            self.assertEqual(session.close(5).reason, "cancelled")
            spawn.assert_not_called()

    def test_private_agent_checkpoint_identity_is_assigned_before_backend_start(self):
        from src.api import _version
        from src.checkpoints import read_checkpoint

        identity = str(uuid.uuid4())
        checkpoint = self.root / "assigned-checkpoint.json"
        with mock.patch("subprocess.Popen") as spawn, self.assertRaises(ValueError):
            Session.launch(self.target, _checkpoint_session_id="not-a-uuid")
        spawn.assert_not_called()
        session = Session.launch(self.target, options=self.options(timeout_seconds=1.2, checkpoint=str(checkpoint),
                                 checkpoint_interval_seconds=1), _checkpoint_session_id=identity)
        session.start()
        self.addCleanup(session.close, 5)
        self.assertIsNotNone(session.wait(5))
        saved = read_checkpoint(checkpoint, app_version=_version(), session_id=identity)
        self.assertEqual(saved["session_id"], identity)
        self.assertEqual(saved["checkpoint"]["session_id"], identity)

    def test_nonblocking_launch_timeout_exact_facts_and_detached_immutable_values(self):
        session = Session.launch(self.target, options=self.options(timeout_seconds=1.3))
        before = time.monotonic()
        session.start()
        self.assertLess(time.monotonic() - before, 0.5)
        result = session.wait(10)
        self.assertIsNotNone(result)
        self.assertEqual((result.reason, result.exit_code, result.startup, result.cleanup),
                         ("timeout", 0, "started", "complete"))
        self.assertGreaterEqual(session.status.sample_count, 8)
        snapshot = session.snapshot
        self.assertIsNotNone(snapshot)
        self.assertIsInstance(snapshot.root_create_time, float)
        with self.assertRaises(TypeError):
            snapshot.data["totals"]["handles"] = -1
        with self.assertRaises(TypeError):
            result.summary["counts"]["observed_samples"] = -1
        report = session.report()
        validate_report(report)
        report["snapshots"].clear()
        self.assertTrue(session.report()["snapshots"])
        self.assertFalse(psutil.pid_exists(snapshot.root_pid))
        with self.assertRaises(RuntimeError):
            session.start()

    def test_context_exception_cancels_and_cleans_owned_target(self):
        session = Session.launch(self.target, options=self.options())
        with self.assertRaisesRegex(ValueError, "caller"), session:
            self.assertTrue(wait_until(lambda: session.snapshot is not None))
            raise ValueError("caller")
        result = session.wait(0)
        self.assertEqual((result.reason, result.exit_code, result.cleanup), ("cancelled", 130, "complete"))
        self.assertFalse(psutil.pid_exists(session.snapshot.root_pid))

    def test_attach_identity_rejection_and_detach_never_terminate_external_target(self):
        process = subprocess.Popen([sys.executable, str(self.target)])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.addCleanup(lambda: process.poll() is None and process.wait(timeout=5))
        identity = psutil.Process(process.pid).create_time()
        wrong = Session.attach(process.pid, create_time=identity + 100, options=self.options())
        wrong.start()
        result = wrong.wait(8)
        self.assertEqual((result.reason, result.startup), ("failed", "failed"))
        self.assertTrue(result.errors)
        self.assertIsNone(process.poll())
        session = Session.attach(process.pid, create_time=identity, options=self.options())
        session.start()
        self.assertTrue(wait_until(lambda: session.status.sample_count >= 2))
        self.assertEqual(session.snapshot.root_create_time, identity)
        self.assertEqual(session.close(8).cleanup, "complete")
        self.assertIsNone(process.poll())
        process.kill()
        process.wait(timeout=5)

    def test_control_capabilities_are_separate_and_default_off(self):
        session = Session.launch(self.target, options=self.options())
        for control in (session.pause, session.resume, session.kill):
            with self.assertRaises(PermissionError):
                control()
        session.close(5)

    def test_exit_codes_keep_signals_before_later_cancel_errors_or_budgets(self):
        options = self.options(budgets={"handles": 1})
        session = Session.launch(self.target, options=options)
        backend = SimpleNamespace(settings=options._settings(), _startup_state="started",
                                  worst_severity=LeakSeverity.HIGH, _peaks={"handles": 2},
                                  target_failed=False, full_coverage=True, conclusive=True)
        for reason, errors in (("timeout", []), ("cancelled", []), ("failed", ["sample failed"])):
            with self.subTest(reason=reason):
                self.assertEqual(session._exit_code(backend, reason, errors), 1)
        backend._startup_state = "failed"
        self.assertEqual(session._exit_code(backend, "failed", ["startup failed"]), 2)
        backend._startup_state = "started"
        backend.worst_severity = LeakSeverity.NONE
        self.assertEqual(session._exit_code(backend, "cancelled", []), 130)
        self.assertEqual(session._exit_code(backend, "failed", ["sample failed"]), 2)
        self.assertEqual(session._exit_code(backend, "timeout", []), 5)

    def test_enabled_controls_run_on_controller_and_record_kill_ownership(self):
        session = Session.launch(self.target, options=self.options(allow_pause=True, allow_kill=True))
        session.start()
        self.assertTrue(wait_until(lambda: session.status.sample_count >= 2))
        try:
            self.assertTrue(session.pause())
            self.assertTrue(wait_until(lambda: session.snapshot.data["monitoring_paused"]))
            self.assertTrue(session.resume())
            self.assertTrue(session.kill())
            result = session.wait(8)
            self.assertEqual(result.cleanup, "complete")
            self.assertEqual(session.report()["outcome"]["cleanup"]["attempted_actions"]["kill"], 1)
        finally:
            session.close(8)

    def test_queued_expired_command_is_cancelled_and_queue_is_bounded(self):
        session = Session.launch(self.target, options=self.options())
        session._thread = mock.Mock()  # Keep execution under this test's control.
        with self.assertRaises(CommandTimeoutError) as caught:
            session._command("pause", 0)
        self.assertFalse(caught.exception.may_have_run)
        backend = mock.Mock()
        session._drain_commands(backend)
        backend.manager.pause.assert_not_called()
        for _ in range(32):
            session._commands.put_nowait(("pause", Future()))
        with self.assertRaisesRegex(RuntimeError, "queue is full"):
            session._command("pause", 0)

    def test_inflight_command_timeout_reports_uncertain_execution(self):
        session = Session.launch(self.target, options=self.options())
        session._thread = mock.Mock()
        entered, release = threading.Event(), threading.Event()
        backend = mock.Mock()
        def pause():
            entered.set()
            release.wait(5)
            return True
        backend.manager.pause.side_effect = pause
        errors = []
        def caller():
            try:
                session._command("pause", 0.2)
            except CommandTimeoutError as error:
                errors.append(error)
        request = threading.Thread(target=caller)
        request.start()
        self.assertTrue(wait_until(lambda: not session._commands.empty()))
        controller = threading.Thread(target=session._drain_commands, args=(backend,))
        controller.start()
        try:
            self.assertTrue(entered.wait(2))
            request.join(2)
            self.assertEqual(len(errors), 1)
            self.assertTrue(errors[0].may_have_run)
        finally:
            release.set()
            controller.join(5)
            request.join(5)

    def test_failed_start_still_has_partial_report_and_explicit_outcome(self):
        session = Session.launch(self.root / "missing.exe", options=self.options())
        session.start()
        result = session.wait(8)
        self.assertEqual((result.reason, result.exit_code, result.startup), ("failed", 2, "failed"))
        self.assertTrue(result.errors)
        validate_report(session.report())

    def test_slow_callback_does_not_stall_samples_or_cleanup(self):
        release = threading.Event()
        entered = threading.Event()
        def callback(_event):
            entered.set()
            release.wait(20)
        session = Session.launch(self.target, options=self.options(timeout_seconds=1.2))
        subscription = session.subscribe(callback)
        try:
            session.start()
            self.assertTrue(entered.wait(3))
            result = session.wait(8)
            self.assertIsNotNone(result)
            self.assertEqual(result.cleanup, "complete")
            self.assertGreaterEqual(session.status.sample_count, 8)
            before = time.monotonic()
            subscription.close()
            self.assertLess(time.monotonic() - before, 0.1)
        finally:
            release.set()
            session.close(8)

    def test_all_callback_workers_can_block_without_stalling_monitor_cleanup(self):
        release = threading.Event()
        condition = threading.Condition()
        entered = []
        def callback(_event):
            with condition:
                entered.append(1)
                condition.notify_all()
            release.wait(20)
        session = Session.launch(self.target, options=self.options(timeout_seconds=1.2))
        subscriptions = [session.subscribe(callback) for _ in range(4)]
        try:
            session.start()
            with condition:
                self.assertTrue(condition.wait_for(lambda: len(entered) == 4, timeout=4))
            self.assertEqual(session.wait(8).cleanup, "complete")
            self.assertGreaterEqual(session.status.sample_count, 8)
            for subscription in subscriptions:
                subscription.close()
        finally:
            release.set()
            session.close(8)

    def test_closed_subscriptions_release_callback_and_do_not_exhaust_session_limit(self):
        session = Session.launch(self.target, options=self.options())
        for _ in range(100):
            subscription = session.subscribe(lambda _event: None)
            subscription.close()
            self.assertTrue(subscription.closed)
            self.assertIsNone(subscription._callback)
        session.close(5)

    def test_callback_backpressure_and_failures_are_observable(self):
        events = []
        subscription = Subscription(events.append)
        with mock.patch("src.api._CALLBACK_QUEUE.put_nowait"):
            for _ in range(100):
                subscription._offer(SessionEvent("snapshot", SessionStatus("running")))
        self.assertEqual(len(subscription._pending), 64)
        self.assertEqual(subscription.dropped, 36)
        failed = Subscription(mock.Mock(side_effect=RuntimeError("subscriber")))
        with mock.patch("src.api._CALLBACK_QUEUE.put_nowait"):
            failed._offer(SessionEvent("status", SessionStatus("running")))
        failed._deliver()
        self.assertEqual(failed.failures, 1)
        subscription.close()

    def test_html_export_failure_is_distinct_from_json_success(self):
        session = Session.launch(self.target, options=self.options())
        session.start()
        self.assertTrue(wait_until(lambda: session.status.sample_count >= 2))
        try:
            with mock.patch("src.session.write_html_report", side_effect=OSError("HTML write denied")):
                exported = session.export(8)
            self.assertTrue(exported.json_path.is_file())
            self.assertIsNone(exported.html_path)
            self.assertTrue(any("HTML write denied" in error for error in exported.errors))
        finally:
            session.close(8)

    def test_public_import_does_not_load_ui_or_install_signal_handlers(self):
        script = """import signal, sys
before = signal.getsignal(signal.SIGINT)
from src.api import Session, SessionOptions
assert signal.getsignal(signal.SIGINT) == before
assert not any(name in sys.modules for name in ('src.dashboard', 'src.main', 'src.file_picker', 'src.keys'))
"""
        subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
