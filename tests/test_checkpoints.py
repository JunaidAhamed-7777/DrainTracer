"""Checkpoint bounds, corruption, writer exclusion and process-free recovery."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

from helpers import make_console, make_process, make_snapshot

from src.checkpoints import (
    CheckpointError,
    CheckpointStore,
    read_checkpoint,
    recovered_report,
)
from src.config import MonitorSettings
from src.session import MonitorSession


class _Session(MonitorSession):
    VERSION = "v5.4.0"


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "session.checkpoint.json"
        session = _Session("worker.exe", console=make_console())
        session._accept_snapshot(make_snapshot(processes=(make_process(100),), timestamp=1000))
        self.report = session.build_report()
        self.identity = {"pid": 100, "create_time": None}

    def _write(self):
        store = CheckpointStore(self.path, "v5.4.0")
        self.addCleanup(store.close)
        store.write(self.report, self.identity)
        return store

    def test_atomic_replacement_and_recovery_preserve_exact_prefix_facts(self) -> None:
        store = self._write()
        first = read_checkpoint(self.path, app_version="v5.4.1", session_id=store.session_id)
        self.assertTrue(first["recovery"]["partial"])
        self.report["event_counts"] = {"observed": 2000, "retained": 0, "dropped": 2000}
        store.write(self.report, self.identity)
        with mock.patch("src.checkpoints.psutil.Process", side_effect=AssertionError("no PID reopening")):
            recovered = recovered_report(self.path, app_version="v5.4.0", session_id=store.session_id)
        self.assertEqual(recovered["session_facts"], self.report["session_facts"])
        self.assertEqual(recovered["event_counts"]["dropped"], 2000)
        self.assertNotEqual(recovered["report_id"], first["report_id"])
        self.assertTrue(recovered["recovery"]["exact_through_cutoff"])
        self.assertFalse(recovered["recovery"]["final_outcome_confirmed"])
        self.assertIsNone(recovered["session_ended_at"])
        self.assertNotIn("checkpoint", recovered)

    def test_second_writer_and_unrelated_existing_files_are_refused(self) -> None:
        store = self._write()
        with self.assertRaises((CheckpointError, OSError)):
            CheckpointStore(self.path, "v5.4.0")
        store.close()
        with self.assertRaisesRegex(CheckpointError, "already exists"):
            CheckpointStore(self.path, "v5.4.0")
        unrelated = self.path.parent / "user.txt"
        unrelated.write_text("preserve this", encoding="utf-8")
        with self.assertRaises(CheckpointError):
            CheckpointStore(unrelated, "v5.4.0")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve this")

    def test_failed_replace_leaves_the_previous_checkpoint_intact(self) -> None:
        store = self._write()
        previous = self.path.read_bytes()
        with (mock.patch("src.checkpoints.os.replace", side_effect=OSError("disk full")),
              self.assertRaisesRegex(OSError, "disk full")):
            store.write(self.report, self.identity)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_input_byte_bounds_precede_decoding_and_replacement(self) -> None:
        store = self._write()
        previous = self.path.read_bytes()
        with (mock.patch("src.report_contract.MAX_REPORT_BYTES", 16),
              mock.patch("src.report_contract.json.loads") as decode,
              self.assertRaises(CheckpointError)):
            read_checkpoint(self.path, app_version="v5.4.0")
        decode.assert_not_called()
        with (mock.patch("src.checkpoints.MAX_REPORT_BYTES", 16),
              mock.patch("src.checkpoints.os.replace") as replace,
              self.assertRaises(CheckpointError)):
            store.write(self.report, self.identity)
        replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), previous)

    def test_a_crashed_writer_releases_its_os_lock(self) -> None:
        ready = self.path.parent / "writer.ready"
        script = (
            "import sys,time; from pathlib import Path; "
            "from src.checkpoints import CheckpointStore; "
            "store=CheckpointStore(sys.argv[1],'v5.4.0'); "
            "Path(sys.argv[2]).write_text('ready'); time.sleep(30)"
        )
        child = subprocess.Popen([sys.executable, "-c", script, str(self.path), str(ready)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "checkpoint writer did not become ready")
            with self.assertRaises((CheckpointError, OSError)):
                CheckpointStore(self.path, "v5.4.0")
            child.kill()
            child.wait(timeout=10)
            with CheckpointStore(self.path, "v5.4.0"):
                self.assertFalse(self.path.exists())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_version_session_process_and_checksum_validation_fail_closed(self) -> None:
        self._write()
        original = json.loads(self.path.read_text(encoding="utf-8"))
        for mutate in (
            lambda data: data["checkpoint"].update(schema=2),
            lambda data: data["checkpoint"].update(app_version="v6.0.0"),
            lambda data: data.update(version="v5.3.0"),
            lambda data: data["checkpoint"].update(session_id=str(uuid.uuid4())),
            lambda data: data["checkpoint"]["target_identity"].update(pid=True),
            lambda data: data["checkpoint"]["target_identity"].update(pid=200),
            lambda data: data["checkpoint"]["writer_identity"].update(create_time=None),
            lambda data: data["checkpoint"].update(captured_at="yesterday"),
            lambda data: data["checkpoint"].update(captured_at="2000-01-01T00:00:00+00:00"),
            lambda data: data["checkpoint"].update(digest="0" * 64),
        ):
            damaged = json.loads(json.dumps(original))
            mutate(damaged)
            self.path.write_text(json.dumps(damaged), encoding="utf-8")
            with self.assertRaises(CheckpointError):
                read_checkpoint(self.path, app_version="v5.4.0")
        self.path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(CheckpointError, "session identity"):
            read_checkpoint(self.path, app_version="v5.4.0", session_id=str(uuid.uuid4()))
        with self.assertRaises(CheckpointError):
            read_checkpoint(self.path, app_version="v5.3.9")

    def test_reader_enforces_the_chart_bound_before_checksum_validation(self) -> None:
        self._write()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        saved["snapshots"] *= 129
        self.path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(CheckpointError, "too many retained"):
            read_checkpoint(self.path, app_version="v5.4.0")

    def test_corruption_oversized_input_and_missing_creation_time_are_rejected(self) -> None:
        self.path.write_text('{"snapshots": [], "checkpoint":', encoding="utf-8")
        with self.assertRaises(CheckpointError):
            read_checkpoint(self.path, app_version="v5.4.0")
        with CheckpointStore(self.path.parent / "other.json", "v5.4.0") as store:
            with self.assertRaises(CheckpointError):
                store.write(self.report, {"pid": 100})
            self.report["snapshots"] *= 129
            with self.assertRaisesRegex(CheckpointError, "too many"):
                store.write(self.report, self.identity)


class CheckpointSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "session.json"

    def _session(self, enabled=True):
        settings = replace(MonitorSettings(), checkpoint=str(self.path) if enabled else None,
                           checkpoint_interval=1.0)
        session = _Session("worker.exe", console=make_console(), settings=settings)
        session.manager._root_pid = 100
        session.manager._identities[100] = 10.0
        return session

    def test_cadence_is_opt_in_and_updates_a_single_checkpoint(self) -> None:
        session = self._session()
        clock = [0.0]
        with (
            mock.patch.object(session.manager, "start"),
            mock.patch.object(session.detector, "sample_once", return_value=make_snapshot(timestamp=1000)),
            mock.patch.object(session.detector, "start"),
            mock.patch("src.session.time.monotonic", side_effect=lambda: clock[0]),
        ):
            session.start()
            self.addCleanup(session._close_checkpoint)
            first = self.path.read_bytes()
            clock[0] = 0.5
            session.poll()
            self.assertEqual(self.path.read_bytes(), first)
            session.detector.stream.publish(make_snapshot(timestamp=1001))
            clock[0] = 1.1
            session.poll()
            saved = read_checkpoint(self.path, app_version="v5.4.0")
            self.assertEqual(saved["sample_count"], 2)
            self.assertNotEqual(self.path.read_bytes(), first)
            session.shutdown()
        self.assertIsNone(session._checkpoint_store)
        session = self._session(enabled=False)
        with (mock.patch("src.session.CheckpointStore") as store,
              mock.patch.object(session.manager, "start"),
              mock.patch.object(session.detector, "sample_once", return_value=make_snapshot()),
              mock.patch.object(session.detector, "start")):
            session.start()
        store.assert_not_called()

    def test_existing_files_are_refused_before_launching_a_target(self) -> None:
        self.path.write_text("unrelated user data", encoding="utf-8")
        session = self._session()
        with mock.patch.object(session.manager, "start") as launch, self.assertRaises(CheckpointError):
            session.start()
        launch.assert_not_called()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "unrelated user data")

    def test_long_run_chart_retention_is_bounded_but_prefix_facts_remain_exact(self) -> None:
        session = self._session(enabled=False)
        for index in range(1000):
            session._accept_snapshot(make_snapshot(timestamp=1000 + index, processes=(
                make_process(100, private=(index + 1) * 1024),
            )))
        report = session.build_report(snapshot_limit=32)
        self.assertEqual(len(report["snapshots"]), 32)
        self.assertEqual(report["snapshots"][0]["timestamp"], 1000)
        self.assertEqual(report["snapshots"][-1]["timestamp"], 1999)
        self.assertEqual(report["sample_count"], 1000)
        self.assertEqual(report["session_facts"]["samples"]["taken"], 1000)
        self.assertEqual(report["session_facts"]["peaks"]["private_bytes"]["value"], 1000 * 1024)
        with CheckpointStore(self.path, "v5.4.0") as store:
            store.write(report, {"pid": 100, "create_time": 10.0})
        recovered = recovered_report(self.path, app_version="v5.4.0")
        self.assertEqual(recovered["session_facts"], report["session_facts"])

    def test_recovery_command_writes_json_html_without_reopening_a_process(self) -> None:
        from src import main as cli

        session = self._session(enabled=False)
        session._accept_snapshot(make_snapshot(timestamp=1000))
        with CheckpointStore(self.path, "v5.4.0") as store:
            store.write(session.build_report(), {"pid": 100, "create_time": 10.0})
            identity = store.session_id
        console = make_console()
        with (mock.patch("src.checkpoints.psutil.Process") as process,
              mock.patch.object(cli, "check_for_update") as update):
            code = cli._main(console, ["recover", str(self.path), "--expect-session", identity,
                                      "--report-dir", str(self.path.parent / "recovered"), "--json"])
        self.assertEqual(code, 0)
        process.assert_not_called()
        update.assert_not_called()
        result = json.loads(console.file.getvalue())
        self.assertEqual(result["recovery_result_schema"], 1)
        self.assertTrue(Path(result["json_path"]).is_file())
        self.assertTrue(Path(result["html_path"]).is_file())
        self.assertIn("Partial checkpoint evidence", Path(result["html_path"]).read_text(encoding="utf-8"))

    def test_a_killed_monitor_leaves_recoverable_evidence_without_reattachment(self) -> None:
        import psutil

        from src.dashboard import DashboardApp

        target = self.path.parent / "bounded.py"
        target.write_text("import time\ntime.sleep(6)\n", encoding="utf-8")
        command = [sys.executable, "src/main.py", "--headless", "--target", str(target), "--no-config",
                   "--checkpoint", str(self.path), "--checkpoint-interval", "1s",
                   "--report-dir", str(self.path.parent / "reports")]
        monitor = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        identity = None
        try:
            deadline = time.monotonic() + 10
            while not self.path.exists() and monitor.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.path.exists(), "monitor did not persist its first checkpoint")
            saved = read_checkpoint(self.path, app_version=DashboardApp.VERSION)
            identity = saved["checkpoint"]["target_identity"]
            monitor.kill()
            monitor.wait(timeout=10)
            with mock.patch("src.checkpoints.psutil.Process", side_effect=AssertionError("no reattachment")):
                report = recovered_report(self.path, app_version=DashboardApp.VERSION)
            self.assertEqual(report["session_facts"], saved["session_facts"])
            self.assertTrue(report["recovery"]["partial"])
            self.assertIsNone(report["headless"]["exit_code"])
        finally:
            if monitor.poll() is None:
                monitor.kill()
            monitor.communicate(timeout=10)
            if identity is not None:
                try:
                    process = psutil.Process(identity["pid"])
                    if process.create_time() == identity["create_time"]:
                        process.kill()  # This test's bounded target, validated before cleanup.
                        process.wait(timeout=10)
                except psutil.NoSuchProcess:
                    pass


if __name__ == "__main__":
    unittest.main()
