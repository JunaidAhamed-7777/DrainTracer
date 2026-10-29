"""Checkpoint bounds, corruption, writer exclusion and process-free recovery."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from helpers import make_console, make_process, make_snapshot

from src.checkpoints import (
    CheckpointError,
    CheckpointStore,
    read_checkpoint,
    recovered_report,
)
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


if __name__ == "__main__":
    unittest.main()
