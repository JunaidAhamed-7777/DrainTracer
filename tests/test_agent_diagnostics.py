"""Agent support privacy, offline recovery and stopped-only nonrecursive removal."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
import psutil
import test_agent_recovery as recovery_helpers
import test_privacy as privacy_helpers

from src.agent_command import main
from src.agent_diagnostics import (
    inspect_agent,
    recover_session,
    remove_control_files,
    support_document,
    support_reports,
    write_support_bundle,
)
from src.agent_service import AgentPolicy, AgentRegistry
from src.agent_transport import authenticate, connect
from src.api import _version
from src.checkpoints import read_checkpoint
from src.process_runner import _windows_process_exited

SECRET = "PrivateDiagnosticSeed987654"
NUMERIC_SECRET = 9988776655


class SupportPrivacyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".draintracer-support-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def diagnostics(self):
        return {"mode": "live", "version": "v5.4.0", "ready": False, "responsive": True, "fresh": True,
                "storage_error": SECRET, "error": {"value": NUMERIC_SECRET},
                "credential": SECRET, "owner": SECRET, "metadata": {"value": NUMERIC_SECRET},
                "sessions": [{"session_id": SECRET, "target_identity": {"pid": NUMERIC_SECRET},
                              "mode": "launch", "state": "failed", "sample_count": 17,
                              "measurement": "partial", "full_coverage": False, "last_observed_at": time.time() - 3,
                              "errors": [SECRET], "metadata": {"value": NUMERIC_SECRET},
                              "result": {"reason": "failed", "startup": "failed", "cleanup": "incomplete",
                                         "error_count": 1, "errors": [SECRET], "path": SECRET}}]}

    def test_preview_and_archive_use_typed_diagnostics_and_the_same_transformed_report(self):
        diagnostics, report = self.diagnostics(), privacy_helpers.private_fixture()
        original = copy.deepcopy(report)
        document = support_document(diagnostics, [report])
        self.assertEqual(report, original)
        self.assertEqual(document["diagnostics"]["sessions"][0]["sample_count"], 17)
        self.assertEqual(document["diagnostics"]["sessions"][0]["result"]["cleanup"], "incomplete")
        self.assertEqual(document["reports"][0]["privacy"]["profile"], "minimal")
        output = self.root / "support.zip"
        write_support_bundle(document, output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {"support.json", "report-1.json", "report-1.html"})
            self.assertTrue(all(member.date_time == (2000, 1, 1, 0, 0, 0) for member in archive.infolist()))
            self.assertTrue(all(not member.extra and not member.comment for member in archive.infolist()))
            transformed = json.loads(archive.read("report-1.json"))
            self.assertEqual(transformed, document["reports"][0])
            rendered = archive.read("report-1.html").decode("utf-8")
            self.assertTrue("Privacy" in rendered and "review before sharing" in rendered)
            for content in [json.dumps(document), *(archive.read(name).decode("utf-8") for name in archive.namelist())]:
                for seed in (SECRET, str(NUMERIC_SECRET), privacy_helpers.SECRET, "543219", "1700000123.5"):
                    self.assertNotIn(seed, content)

    def test_freshness_is_monotonic_even_when_wall_clock_moves_backwards(self):
        registry = AgentRegistry(self.root, AgentPolicy(), session_type=recovery_helpers.FakeSession)
        with (mock.patch("src.agent_service.process_user_sid", return_value="owner"),
              mock.patch("src.agent_service.current_user_sid", return_value="owner")):
            started = registry.handle(recovery_helpers.request("start", {"mode": "attach", "pid": 987654, "create_time": 123.5}))
        registry.refresh()
        registry.records[started["result"]["session_id"]].last_progress_clock -= 30
        with mock.patch("src.agent_service.time.time", return_value=1):
            result = registry.handle(recovery_helpers.request("diagnostics"))["result"]
        self.assertFalse(result["fresh"])
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["stale_sessions"]), 1)

    def test_exclusive_size_checked_atomic_bundle_preserves_existing_and_failed_outputs(self):
        document = support_document(self.diagnostics())
        existing = self.root / "existing.zip"
        existing.write_bytes(b"preserve unrelated contents")
        with self.assertRaises(FileExistsError):
            write_support_bundle(document, existing)
        self.assertEqual(existing.read_bytes(), b"preserve unrelated contents")
        output = self.root / "must-not-exist.zip"
        with (mock.patch("src.agent_diagnostics.os.fsync", side_effect=OSError("controlled disk failure")),
              self.assertRaises(OSError)):
            write_support_bundle(document, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".agent-support-*.tmp")), [])
        with (mock.patch("src.agent_diagnostics.MAX_BUNDLE_CONTENT", 16), self.assertRaises(ValueError)):
            write_support_bundle(document, output)
        self.assertFalse(output.exists())


@unittest.skipUnless(os.name == "nt", "native local agent operations")
class NativeOperationsTests(unittest.TestCase):
    setUp = recovery_helpers.NativeRecoveryTests.setUp
    start_agent = recovery_helpers.NativeRecoveryTests.start_agent
    stop_all = recovery_helpers.NativeRecoveryTests.stop_all

    def test_diagnose_stop_and_removal_preserve_attached_target_and_unrelated_data(self):
        script = self.root / (SECRET + ".py")
        script.write_text("import time; time.sleep(60)", encoding="utf-8")
        attached = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(attached.wait, 5)
        self.addCleanup(attached.kill)
        process, client = self.start_agent(allow_launch=True)
        launched = client.request("start", {"mode": "launch", "target": str(script), "timeout_seconds": 20,
                                             "sample_interval_ms": 100, "windows_seconds": [1]})["session_id"]
        client.request("start", {"mode": "attach", "pid": attached.pid,
                                  "create_time": psutil.Process(attached.pid).create_time(), "timeout_seconds": 20})
        recovery_helpers.wait_for(lambda: client.request("status", {"session_id": launched})["sample_count"] >= 2)
        health = inspect_agent(self.state, client)
        self.assertEqual(health["mode"], "live")
        self.assertTrue(health["responsive"])
        self.assertTrue(health["fresh"])
        self.assertEqual(health["queue_depth"], 0)
        self.assertEqual(health["active_sessions"], 2)
        with self.assertRaises(ValueError):
            remove_control_files(self.state, apply=True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--state-dir", str(self.state), "--profile", self.profile, "stop"]), 0)
        self.assertTrue(json.loads(output.getvalue())["cleanup_pending"])
        process.communicate(timeout=20)
        self.assertIsNone(attached.poll())
        before = (self.state / "agent-state.json").read_bytes()
        with mock.patch("src.agent_diagnostics.os.fsync", side_effect=AssertionError("offline inspection must not write")):
            offline = inspect_agent(self.state)
        self.assertEqual(offline["mode"], "offline")
        self.assertFalse(offline["ready"])
        self.assertEqual((self.state / "agent-state.json").read_bytes(), before)
        reports = support_reports(self.state, [launched])
        document = support_document(offline, reports)
        write_support_bundle(document, self.root / "safe-support.zip")
        note = self.state / "unrelated-note.txt"
        note.write_text(SECRET, encoding="utf-8")
        evidence = set((self.state / "reports").rglob("*.json")) | set((self.state / "checkpoints").glob("*.json"))
        self.assertTrue(evidence)
        preview = remove_control_files(self.state)
        self.assertFalse(preview["applied"])
        self.assertTrue((self.state / "credential").exists())
        with (mock.patch.object(psutil.Process, "kill", side_effect=AssertionError("removal must not kill")),
              mock.patch.object(psutil.Process, "terminate", side_effect=AssertionError("removal must not terminate")),
              mock.patch.object(psutil.Process, "resume", side_effect=AssertionError("removal must not resume")),
              mock.patch.object(psutil.Process, "suspend", side_effect=AssertionError("removal must not suspend"))):
            original_unlink = Path.unlink

            def controlled_failure(path, *args, **kwargs):
                if path.name == "agent-state.json":
                    raise OSError("controlled transient journal removal failure")
                return original_unlink(path, *args, **kwargs)

            with (mock.patch.object(Path, "unlink", controlled_failure), self.assertRaises(OSError)):
                remove_control_files(self.state, apply=True)
            self.assertTrue((self.state / "agent-state.json").exists())
            self.assertFalse((self.state / "credential").exists())
            self.assertFalse((self.state / "agent-state.lock").exists())
            removed = remove_control_files(self.state, apply=True)
        self.assertTrue(removed["applied"])
        self.assertTrue(remove_control_files(self.state, apply=True)["already_removed"])
        self.assertFalse((self.state / "credential").exists())
        self.assertFalse((self.state / "agent-state.json").exists())
        self.assertFalse((self.state / "agent-state.lock").exists())
        self.assertEqual(note.read_text(encoding="utf-8"), SECRET)
        self.assertTrue(all(path.exists() for path in evidence))
        self.assertIsNone(attached.poll())

    def test_accepted_shutdown_survives_a_disconnected_client(self):
        process, client = self.start_agent()
        pipe = connect(client.name)
        try:
            authenticate(pipe, client.secret, server=False)
            pipe.send(recovery_helpers.request("shutdown"))
            self.assertTrue(pipe.receive()["result"]["accepted"])
        finally:
            pipe.close()  # Deliberately omit acknowledgement.
        process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0)

    def test_offline_recovery_reads_matching_prefix_without_pid_access_or_agent_start(self):
        script = self.root / "checkpoint.py"
        script.write_text("import time; time.sleep(60)", encoding="utf-8")
        process, client = self.start_agent(allow_launch=True)
        session_id = client.request("start", {"mode": "launch", "target": str(script), "timeout_seconds": 20,
                                               "sample_interval_ms": 100, "windows_seconds": [1]})["session_id"]
        source = self.state / "checkpoints" / (session_id + ".json")
        recovery_helpers.wait_for(source.exists)
        recovery_helpers.wait_for(lambda: read_checkpoint(source, app_version=_version())["session_facts"]["samples"]["taken"] >= 2)
        root_pid = client.request("status", {"session_id": session_id})["target_identity"]["pid"]
        process.kill()
        process.communicate(timeout=5)
        recovery_helpers.wait_for(lambda: not psutil.pid_exists(root_pid) or _windows_process_exited(root_pid))
        output = self.root / "recovered"
        output.mkdir()
        with mock.patch("src.agent_diagnostics.psutil.Process", side_effect=AssertionError("recovery must not reopen a PID")):
            preview = recover_session(self.state, session_id)
            written = recover_session(self.state, session_id, output)
        self.assertTrue(preview["recovery"]["partial"])
        self.assertFalse(preview["recovery"]["final_outcome_confirmed"])
        self.assertIsNone(preview["session_ended_at"])
        self.assertTrue(Path(written["json"]).is_file())
        self.assertTrue(Path(written["html"]).is_file())


if __name__ == "__main__":
    unittest.main()
