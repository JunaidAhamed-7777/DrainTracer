"""Atomic admission state, incompatible-state preservation and OS-released locks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

from src.agent_state import AgentJournal, AgentStateError
from src.agent_transport import private_directory
from src.checkpoints import CheckpointError


def record():
    identity = str(uuid.uuid4())
    return {"session_id": identity, "mode": "attach", "state": "running", "request_id": str(uuid.uuid4()),
            "request_digest": "a" * 64, "requested_at": 1.0, "updated_at": 2.0, "pid": 42,
            "create_time": 123.5, "deadline": 62.0, "checkpoint": identity + ".json",
            "writer_identity": {"pid": 43, "create_time": 122.0}, "sample_count": 5,
            "measurement": "partial", "full_coverage": False, "result": None, "reports": [],
            "identity_status": "not_checked"}


class JournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".draintracer-agent-state-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = private_directory(self.root / "agent") if os.name == "nt" else self.root
        self.journal = AgentJournal(self.directory, app_version="v5.4.0", owner="test-owner")
        self.addCleanup(self.journal.close)

    def test_round_trip_and_failed_atomic_replacement_preserve_prior_state(self):
        saved = record()
        self.journal.write([saved], [])
        original = self.journal.path.read_bytes()
        saved["sample_count"] = 100
        for operation in ("fsync", "replace"):
            with (mock.patch(f"src.agent_state.os.{operation}", side_effect=OSError("controlled full disk")),
                  self.assertRaises(OSError)):
                self.journal.write([saved], [])
            self.assertEqual(self.journal.path.read_bytes(), original)
            self.assertEqual(self.journal.snapshot()["sessions"][0]["sample_count"], 5)
            self.assertEqual(list(self.directory.glob(".agent-state-*.tmp")), [])
        self.journal.close()
        restored = AgentJournal(self.directory, app_version="v5.4.1", owner="test-owner")
        self.addCleanup(restored.close)
        self.assertEqual(restored.snapshot()["sessions"][0]["sample_count"], 5)
        self.assertEqual(restored.snapshot()["state_id"], json.loads(original)["state_id"])

    def test_exclusive_writer_lock_is_released_after_a_process_crash(self):
        self.journal.close()
        ready = self.root / "writer-ready"
        code = ("from pathlib import Path; import time; from src.agent_state import AgentJournal; "
                f"j=AgentJournal(Path({str(self.directory)!r}),app_version='v5.4.0',owner='test-owner'); "
                f"Path({str(ready)!r}).touch(); time.sleep(60)")
        writer = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists():
                if time.monotonic() >= deadline:
                    self.fail("the journal writer failed to acquire its lock")
                time.sleep(0.01)
            with self.assertRaises((CheckpointError, OSError)):
                AgentJournal(self.directory, app_version="v5.4.0", owner="test-owner")
            writer.kill()
            writer.communicate(timeout=5)
            restored = AgentJournal(self.directory, app_version="v5.4.0", owner="test-owner")
            restored.close()
        finally:
            if writer.poll() is None:
                writer.kill()
            writer.communicate(timeout=5)

    def test_future_malformed_and_unrelated_state_are_preserved(self):
        self.journal.write([record()], [])
        valid = self.journal.snapshot()
        self.journal.close()
        for mutate in (lambda value: value.update(version="v5.99.0"), lambda value: value.update(owner="another-owner"),
                       lambda value: value.update(schema=True), lambda value: value["sessions"][0].update(reports=["../leak_report_" + str(uuid.uuid4()) + ".json"])):
            value = json.loads(json.dumps(valid))
            mutate(value)
            raw = json.dumps(value).encode()
            self.journal.path.write_bytes(raw)
            with self.assertRaises((AgentStateError, ValueError)):
                AgentJournal(self.directory, app_version="v5.4.0", owner="test-owner")
            self.assertEqual(self.journal.path.read_bytes(), raw)
        raw = b'{"schema":1,"schema":1}'
        self.journal.path.write_bytes(raw)
        with self.assertRaises(ValueError):
            AgentJournal(self.directory, app_version="v5.4.0", owner="test-owner")
        self.assertEqual(self.journal.path.read_bytes(), raw)

    def test_storage_high_water_and_inventory_limits_are_explicit_without_deletion(self):
        extra = self.directory / "preserved.bin"
        extra.write_bytes(b"x" * (1024 * 1024))
        self.journal.max_storage_bytes = 1024 * 1024
        self.assertFalse(self.journal.inventory()["admission_available"])
        with self.assertRaisesRegex(AgentStateError, "high-water"):
            self.journal.require_capacity()
        self.assertTrue(extra.exists())
        with (mock.patch("src.agent_state.MAX_INVENTORY", 1), self.assertRaisesRegex(AgentStateError, "inventory")):
            self.journal.inventory()
        self.assertEqual(extra.stat().st_size, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
