"""Durable replay, crash recovery, disconnect isolation and explicit storage failures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
import psutil
from live_checkpoint_wait import wait_for_checkpoint
from test_agent_service import FakeSession, request, wait_for

from src.agent_command import AgentClient
from src.agent_service import AgentError, AgentPolicy, AgentRegistry
from src.agent_state import AgentJournal
from src.agent_transport import (
    authenticate,
    connect,
    current_user_sid,
    private_directory,
)
from src.api import _version
from src.checkpoints import read_checkpoint
from src.process_runner import _windows_process_exited


class DurableRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".draintracer-agent-recovery-test-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.directory = private_directory(root / "agent") if os.name == "nt" else root
        self.reports = self.directory / "reports"
        self.reports.mkdir()
        self.owner = current_user_sid() if os.name == "nt" else "test-owner"
        self.journal = AgentJournal(self.directory, app_version="v5.4.0", owner=self.owner)
        self.addCleanup(lambda: self.journal.close())
        self.registry = AgentRegistry(self.reports, AgentPolicy(), session_type=FakeSession, journal=self.journal)
        FakeSession.created = []
        self.patch_owner = mock.patch("src.agent_service.process_user_sid", return_value=self.owner)
        self.patch_current = mock.patch("src.agent_service.current_user_sid", return_value=self.owner)
        self.patch_owner.start()
        self.patch_current.start()
        self.addCleanup(self.patch_owner.stop)
        self.addCleanup(self.patch_current.stop)

    def start_request(self):
        return request("start", {"mode": "attach", "pid": 987654, "create_time": 123.5})

    def restart(self, *, retention_seconds=7 * 86400):
        self.journal.close()
        self.journal = AgentJournal(self.directory, app_version="v5.4.0", owner=self.owner, retention_seconds=retention_seconds)
        self.registry = AgentRegistry(self.reports, AgentPolicy(), session_type=FakeSession, journal=self.journal)

    def test_intent_is_durable_before_start_and_replayed_after_restart_without_new_backend(self):
        original = self.start_request()

        def verify_intent():
            saved = json.loads(self.journal.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["sessions"][0]["state"], "accepted")
            self.assertEqual(saved["sessions"][0]["request_id"], original["id"])

        with mock.patch.object(FakeSession, "start", side_effect=verify_intent):
            first = self.registry.handle(original)
        self.assertTrue(first["ok"])
        self.restart()
        self.assertEqual(self.registry.handle(original), first)
        self.assertEqual(len(FakeSession.created), 1)
        restored = self.registry.records[first["result"]["session_id"]]
        self.assertIsNone(restored.session)
        self.assertEqual(restored.state, "interrupted")
        self.assertEqual(restored.result["cleanup"], "unknown")
        self.assertEqual(restored.identity_status, "exited")
        changed = dict(original, params={**original["params"], "create_time": 456})
        self.assertEqual(self.registry.handle(changed)["error"]["code"], "conflict")

    def test_failed_intent_write_refuses_start_and_preserves_previous_state(self):
        prior = self.journal.path.read_bytes()
        with (mock.patch.object(self.journal, "write", side_effect=OSError("controlled full disk")),
              mock.patch.object(FakeSession, "start") as start):
            failed = self.registry.handle(self.start_request())
        start.assert_not_called()
        self.assertEqual(failed["error"]["code"], "storage")
        self.assertEqual(self.journal.path.read_bytes(), prior)
        self.assertEqual(len(self.registry.records), 0)
        self.assertIsNotNone(self.registry.storage_error)
        self.assertTrue(self.registry.handle(self.start_request())["ok"])

    def test_repeated_restarts_do_not_extend_interrupted_record_retention(self):
        self.journal.retention_seconds = 10
        with mock.patch("src.agent_service.time.time", return_value=1000):
            started = self.registry.handle(self.start_request())
        identity = started["result"]["session_id"]
        for clock in (1005, 1010, 1014):
            with mock.patch("src.agent_service.time.time", return_value=clock):
                self.restart(retention_seconds=10)
            record = self.registry.records[identity]
            self.assertEqual(record.state, "interrupted")
            self.assertEqual(record.updated_at, 1005)
            self.assertEqual(self.journal.snapshot()["sessions"][0]["updated_at"], 1005)
        with mock.patch("src.agent_service.time.time", return_value=1016):
            self.restart(retention_seconds=10)
        self.assertNotIn(identity, self.registry.records)
        self.assertEqual(self.journal.snapshot()["sessions"], [])
        self.assertEqual(len(FakeSession.created), 1)

    def test_reused_pid_is_diagnostic_only_and_never_restores_control(self):
        started = self.registry.handle(self.start_request())
        with mock.patch("src.agent_service.psutil.Process", return_value=mock.Mock(create_time=lambda: 999)):
            self.restart()
        restored = self.registry.records[started["result"]["session_id"]]
        self.assertEqual(restored.identity_status, "reused")
        self.assertIsNone(restored.session)
        for operation in ("pause", "resume", "kill"):
            response = self.registry.handle(request(operation, {"session_id": restored.identity}))
            self.assertEqual(response["error"]["code"], "session_finished")
        self.assertEqual(len(FakeSession.created), 1)

    def test_recovery_render_failure_is_storage_error_and_preserves_saved_json_metadata(self):
        started = self.registry.handle(self.start_request())
        self.restart()
        checkpoint_id, report_id = str(uuid.uuid4()), str(uuid.uuid4())
        with (mock.patch.object(self.registry, "_checkpoint_report", return_value=(self.directory / "prefix", {"session_id": checkpoint_id})),
              mock.patch("src.agent_service.recovered_report", return_value={"report_id": report_id}) as recover,
              mock.patch("src.agent_service.report_directory", return_value=self.reports),
              mock.patch("src.agent_service.write_html_report", side_effect=OSError("controlled full disk"))):
            response = self.registry.handle(request("export", {"session_id": started["result"]["session_id"]}))
        self.assertEqual(response["error"]["code"], "storage")
        self.assertEqual(recover.call_args.kwargs["session_id"], checkpoint_id)
        self.assertTrue((self.reports / f"leak_report_{report_id}.json").is_file())
        self.assertEqual(self.journal.snapshot()["sessions"][0]["reports"], [f"leak_report_{report_id}.json"])

    def test_retention_drops_old_metadata_and_tombstones_without_deleting_evidence(self):
        started = self.registry.handle(self.start_request())
        FakeSession.created[0].finish()
        self.registry.refresh()
        self.registry._persist(force=True)
        saved = self.journal.snapshot()
        saved["sessions"][0]["updated_at"] = 1
        saved["tombstones"][0]["expires_at"] = 1
        preserved = self.reports / "unrelated-user-note.txt"
        preserved.write_text("keep me", encoding="utf-8")
        self.journal.write(saved["sessions"], saved["tombstones"])
        self.restart()
        self.assertNotIn(started["result"]["session_id"], self.registry.records)
        self.assertEqual(len(self.registry.tombstones), 0)
        self.assertEqual(preserved.read_text(encoding="utf-8"), "keep me")


@unittest.skipUnless(os.name == "nt", "native crash/restart ownership drill")
class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".draintracer-agent-crash-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "agent"
        self.profile = "recover-" + uuid.uuid4().hex[:16]
        self.processes = []
        self.addCleanup(self.stop_all)

    def start_agent(self, allow_launch=False, storage_limit=512 * 1024 * 1024):
        marker = self.root / (str(uuid.uuid4()) + ".ready")
        stop = self.root / (str(uuid.uuid4()) + ".stop")
        code = ("from pathlib import Path; import threading,time; "
                "from src.agent_command import AgentServer; from src.agent_service import AgentPolicy; "
                f"server=AgentServer({str(self.state)!r},profile={self.profile!r},max_storage_bytes={storage_limit!r},"
                f"policy=AgentPolicy(allow_launch={allow_launch!r},launch_roots=(Path({str(self.root)!r}),) if {allow_launch!r} else ())); "
                "event=threading.Event(); "
                f"stop=Path({str(stop)!r}); "
                "watch=threading.Thread(target=lambda: ([(time.sleep(.02)) for _ in iter(lambda: stop.exists(),True)],event.set()),daemon=True); "
                f"watch.start(); Path({str(marker)!r}).touch(); server.serve(event)")
        process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append((process, stop))
        wait_for(lambda: marker.exists() or process.poll() is not None)
        if process.poll() is not None:
            self.fail(process.communicate(timeout=5)[1].decode(errors="replace"))
        return process, AgentClient(self.state, profile=self.profile)

    def stop_all(self):
        for process, stop in reversed(self.processes):
            if process.poll() is None:
                stop.touch()
                try:
                    process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
                    self.fail("ordinary agent shutdown exceeded its shared cleanup deadline")
            else:
                process.communicate(timeout=5)

    def test_launch_crash_restart_recovers_partial_prefix_and_never_relaunches(self):
        child_file = self.root / "child.pid"
        script = self.root / "tree.py"
        script.write_text("import pathlib,subprocess,sys,time\n"
                          "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                          f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid))\n"
                          "time.sleep(60)\n", encoding="utf-8")
        process, client = self.start_agent(allow_launch=True)
        identity = str(uuid.uuid4())
        params = {"mode": "launch", "target": str(script), "timeout_seconds": 20,
                  "sample_interval_ms": 100, "windows_seconds": [1]}
        started = client.request("start", params, request_id=identity)
        session_id = started["session_id"]
        checkpoint = self.state / "checkpoints" / (session_id + ".json")
        wait_for(lambda: child_file.exists() and checkpoint.exists())
        wait_for_checkpoint(checkpoint, writer=process, samples=5, session_id=session_id)
        target = client.request("status", {"session_id": session_id})["target_identity"]
        child_pid = int(child_file.read_text())
        process.kill()
        process.communicate(timeout=5)
        saved = read_checkpoint(checkpoint, app_version=_version(), session_id=session_id)
        self.assertGreaterEqual(saved["session_facts"]["samples"]["taken"], 5)
        wait_for(lambda: not psutil.pid_exists(target["pid"]) or _windows_process_exited(target["pid"]))
        wait_for(lambda: not psutil.pid_exists(child_pid) or _windows_process_exited(child_pid))
        _new_process, client = self.start_agent(allow_launch=True)
        restored = client.request("status", {"session_id": session_id})
        self.assertEqual(restored["state"], "interrupted")
        self.assertEqual(restored["result"]["cleanup"], "unknown")
        self.assertEqual(client.request("start", params, request_id=identity), started)
        exported = client.request("export", {"session_id": session_id})
        json_path = next(Path(exported["directory"]) / item for item in exported["reports"] if item.endswith(".json"))
        report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertTrue(report["recovery"]["partial"])
        self.assertFalse(report["recovery"]["final_outcome_confirmed"])
        self.assertIsNone(report["session_ended_at"])
        self.assertEqual(report["root_pid"], target["pid"])
        self.assertEqual(client.request("status")["active_sessions"], 0)

    def test_same_writer_checkpoint_swap_is_refused_when_pending_launch_identity_is_unknown(self):
        script = self.root / "steady.py"
        script.write_text("import time; time.sleep(60)", encoding="utf-8")
        process, client = self.start_agent(allow_launch=True)
        params = {"mode": "launch", "target": str(script), "timeout_seconds": 20,
                  "sample_interval_ms": 100, "windows_seconds": [1]}
        first, second = (client.request("start", params)["session_id"] for _ in range(2))
        first_path = self.state / "checkpoints" / (first + ".json")
        second_path = self.state / "checkpoints" / (second + ".json")
        wait_for(lambda: first_path.exists() and second_path.exists())
        original = wait_for_checkpoint(first_path, writer=process, samples=1, session_id=first)
        other = wait_for_checkpoint(second_path, writer=process, samples=1, session_id=second)
        self.assertEqual(original["checkpoint"]["writer_identity"], other["checkpoint"]["writer_identity"])
        process.kill()
        process.communicate(timeout=5)
        for path, identity in ((first_path, first), (second_path, second)):
            read_checkpoint(path, app_version=_version(), session_id=identity)
        journal = self.state / "agent-state.json"
        saved = json.loads(journal.read_text(encoding="utf-8"))
        first_record = next(item for item in saved["sessions"] if item["session_id"] == first)
        first_record.update(pid=None, create_time=None, state="accepted", result=None)
        journal.write_text(json.dumps(saved), encoding="utf-8")
        replacement = second_path.read_bytes()
        first_path.write_bytes(replacement)
        _new_process, client = self.start_agent(allow_launch=True)
        restored = client.request("status", {"session_id": first})
        self.assertEqual(restored["state"], "interrupted")
        self.assertIsNone(restored["target_identity"]["pid"])
        self.assertEqual(restored["identity_status"], "unknown")
        with self.assertRaises(AgentError) as refused:
            client.request("export", {"session_id": first})
        self.assertEqual(refused.exception.code, "evidence_unavailable")
        self.assertEqual(first_path.read_bytes(), replacement)
        self.assertEqual(client.request("status")["active_sessions"], 0)

    def test_failed_workload_and_storage_exhaustion_do_not_stop_another_session(self):
        good, bad = self.root / "good.py", self.root / "bad.py"
        good.write_text("import time; time.sleep(60)", encoding="utf-8")
        bad.write_text("raise SystemExit(7)", encoding="utf-8")
        _process, client = self.start_agent(allow_launch=True, storage_limit=1024 * 1024)
        healthy = client.request("start", {"mode": "launch", "target": str(good), "timeout_seconds": 20,
                                            "sample_interval_ms": 100, "windows_seconds": [1]})["session_id"]
        failed = client.request("start", {"mode": "launch", "target": str(bad)})["session_id"]
        wait_for(lambda: client.request("status", {"session_id": failed})["state"] == "completed")
        failed_status = client.request("status", {"session_id": failed})
        self.assertEqual(failed_status["result"]["target_exit_code"], 7)
        before = client.request("status", {"session_id": healthy})["sample_count"]
        preserved = self.state / "unrelated-preserved.bin"
        preserved.write_bytes(b"x" * (1024 * 1024))
        with self.assertRaises(AgentError) as refused:
            client.request("start", {"mode": "launch", "target": str(good)})
        self.assertEqual(refused.exception.code, "storage")
        with self.assertRaises(AgentError) as export_refused:
            client.request("export", {"session_id": healthy})
        self.assertEqual(export_refused.exception.code, "storage")
        self.assertFalse(client.request("status")["ready"])
        wait_for(lambda: client.request("status", {"session_id": healthy})["sample_count"] >= before + 3)
        client.request("cancel", {"session_id": healthy})
        wait_for(lambda: client.request("status", {"session_id": healthy})["state"] == "completed")
        self.assertEqual(client.request("status", {"session_id": healthy})["result"]["cleanup"], "complete")
        self.assertEqual(preserved.stat().st_size, 1024 * 1024)

    def test_disconnected_client_and_agent_crash_leave_observed_target_running(self):
        target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(target.wait, 5)
        self.addCleanup(target.kill)
        process, client = self.start_agent()
        pipe = connect(client.name)
        try:
            authenticate(pipe, client.secret, server=False)
            pipe.send(request("start", {"mode": "attach", "pid": target.pid,
                                        "create_time": psutil.Process(target.pid).create_time(),
                                        "timeout_seconds": 20, "sample_interval_ms": 100, "windows_seconds": [1]}))
            started = pipe.receive()["result"]
            # Deliberately omit the receipt acknowledgement and disconnect.
        finally:
            pipe.close()
        session_id = started["session_id"]
        wait_for(lambda: client.request("status", {"session_id": session_id})["sample_count"] >= 3)
        process.kill()
        process.communicate(timeout=5)
        self.assertIsNone(target.poll())
        _new_process, client = self.start_agent()
        restored = client.request("status", {"session_id": session_id})
        self.assertEqual(restored["state"], "interrupted")
        self.assertEqual(restored["identity_status"], "identity_matches")
        self.assertIsNone(target.poll())


if __name__ == "__main__":
    unittest.main()
