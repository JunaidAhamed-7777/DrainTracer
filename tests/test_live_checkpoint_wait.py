"""Live-writer polling tolerates OS replacement contention, never corrupt evidence."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from helpers import make_console, make_process, make_snapshot
from live_checkpoint_wait import wait_for_checkpoint

from src.api import _version
from src.checkpoints import CheckpointError, CheckpointStore, read_checkpoint
from src.session import MonitorSession


class LiveCheckpointWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "checkpoint.json"
        self.version = _version()
        session = MonitorSession("worker.exe", console=make_console())
        session.VERSION = self.version
        for timestamp in (1000, 1000.1):
            session._accept_snapshot(make_snapshot(processes=(make_process(100),), timestamp=timestamp))
        with CheckpointStore(self.path, self.version) as store:
            store.write(session.build_report(), {"pid": 100, "create_time": None})
            self.session_id = store.session_id
        self.raw = self.path.read_bytes()
        self.writer = mock.Mock()
        self.writer.poll.return_value = None
        version = mock.patch("live_checkpoint_wait._version", return_value=self.version)
        version.start()
        self.addCleanup(version.stop)

    def wait(self):
        return wait_for_checkpoint(self.path, writer=self.writer, samples=2, session_id=self.session_id)

    def test_actual_transient_open_errors_then_fully_validated_checkpoint(self):
        for error in (PermissionError(13, "Permission denied"), FileNotFoundError(2, "missing")):
            with self.subTest(error=type(error).__name__):
                with (mock.patch("src.report_contract.Path.open", side_effect=[error, io.BytesIO(self.raw)]) as opened,
                      mock.patch("live_checkpoint_wait.time.sleep") as sleep):
                    report = self.wait()
                self.assertEqual(report["session_id"], self.session_id)
                self.assertEqual(report["session_facts"]["samples"]["taken"], 2)
                self.assertEqual(opened.call_count, 2)
                sleep.assert_called_once_with(0.02)

    def test_permanent_permission_denial_keeps_original_deadline_and_os_cause(self):
        error = PermissionError(13, "Permission denied")
        with (mock.patch("src.report_contract.Path.open", side_effect=error) as opened,
              mock.patch("live_checkpoint_wait.time.monotonic", side_effect=[0, 1, 6]),
              mock.patch("live_checkpoint_wait.time.sleep") as sleep,
              self.assertRaises(AssertionError) as caught):
            self.wait()
        self.assertEqual(opened.call_count, 2)
        sleep.assert_called_once_with(0.02)
        self.assertIsInstance(caught.exception.__cause__, CheckpointError)
        self.assertIs(caught.exception.__cause__.__cause__.__context__, error)

    def test_open_error_after_writer_stop_is_immediately_strict(self):
        self.writer.poll.return_value = 1
        with (mock.patch("src.report_contract.Path.open", side_effect=PermissionError(13, "Permission denied")) as opened,
              mock.patch("live_checkpoint_wait.time.sleep") as sleep,
              self.assertRaises(CheckpointError)):
            self.wait()
        opened.assert_called_once()
        sleep.assert_not_called()
        with (mock.patch("src.report_contract.Path.open", side_effect=PermissionError(13, "Permission denied")),
              self.assertRaises(CheckpointError)):
            read_checkpoint(self.path, app_version=self.version, session_id=self.session_id)

    def test_schema_digest_identity_json_and_unrelated_os_errors_never_retry(self):
        digest = json.loads(self.raw)
        digest["checkpoint"]["digest"] = "0" * 64
        schema = json.loads(self.raw)
        schema["report_schema"] = True
        cases = [(json.dumps(digest).encode(), self.session_id),
                 (json.dumps(schema).encode(), self.session_id),
                 (self.raw, str(uuid.uuid4())), (b"{invalid", self.session_id)]
        for raw, identity in cases:
            with self.subTest(identity=identity, size=len(raw)):
                with (mock.patch("src.report_contract.Path.open", return_value=io.BytesIO(raw)) as opened,
                      mock.patch("live_checkpoint_wait.time.sleep") as sleep,
                      self.assertRaises(CheckpointError)):
                    wait_for_checkpoint(self.path, writer=self.writer, samples=2, session_id=identity)
                opened.assert_called_once()
                sleep.assert_not_called()
        with (mock.patch("src.report_contract.Path.open", side_effect=OSError(5, "I/O fault")) as opened,
              mock.patch("live_checkpoint_wait.time.sleep") as sleep,
              self.assertRaises(CheckpointError)):
            self.wait()
        opened.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.raw)


if __name__ == "__main__":
    unittest.main()
