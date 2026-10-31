"""Capability, identity, scheduling bounds and native agent/client acceptance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import helpers  # noqa: F401
import psutil

from src.agent_command import AgentClient, AgentServer
from src.agent_service import AgentError, AgentPolicy, AgentRegistry
from src.api import CommandTimeoutError


def request(operation, params=None, identity=None):
    return {"protocol": 1, "id": identity or str(uuid.uuid4()), "operation": operation, "params": params or {}}


def wait_for(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("agent acceptance drill exceeded its bounded wait")
        time.sleep(0.02)


class FakeSession:
    created: ClassVar[list] = []

    def __init__(self, options, mode):
        self.options, self.mode = options, mode
        self.status = SimpleNamespace(state="running", sample_count=0, measurement="inconclusive", full_coverage=False)
        self.result = None
        self.cancelled = False
        self.created.append(self)

    @classmethod
    def attach(cls, _pid, *, create_time, options):
        result = cls(options, "attach")
        result.create_time = create_time
        return result

    @classmethod
    def launch(cls, path, args=(), *, options, _manager_factory):
        result = cls(options, "launch")
        result.path, result.args, result.factory = path, args, _manager_factory
        return result

    def start(self):
        pass

    def wait(self, _timeout):
        return self.result

    def cancel(self):
        self.cancelled = True

    def finish(self, reason="cancelled", cleanup="complete"):
        self.result = SimpleNamespace(reason=reason, exit_code=130, startup="started", cleanup=cleanup,
                                      measurement="inconclusive", full_coverage=False,
                                      target_exit_code=None, errors=(), exports=None)

    def close(self, timeout):
        self.cancel()
        self.finish()
        return self.result

    def pause(self, timeout):
        return True

    resume = pause
    kill = pause

    def export(self, timeout):
        return SimpleNamespace(json_path=self.options.report_dir / "evidence.json", html_path=None, errors=())


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.script = self.root / "workload.py"
        self.script.write_text("pass")
        FakeSession.created = []
        self.owner = mock.patch("src.agent_service.process_user_sid", return_value="owner")
        self.current = mock.patch("src.agent_service.current_user_sid", return_value="owner")
        self.owner.start()
        self.current.start()
        self.addCleanup(self.owner.stop)
        self.addCleanup(self.current.stop)
        self.registry = AgentRegistry(self.root, AgentPolicy(max_sessions=2, max_records=2), session_type=FakeSession)

    def attach(self, **extra):
        return self.registry.handle(request("start", {"mode": "attach", "pid": 987654, "create_time": 123.5, **extra}))

    def test_attachment_identity_and_independent_capabilities_are_required(self):
        self.assertFalse(self.registry.handle(request("start", {"mode": "attach", "pid": 987654}))["ok"])
        with mock.patch("src.agent_service.process_user_sid", return_value="other-user"):
            denied = self.attach()
        self.assertEqual(denied["error"]["code"], "forbidden")
        self.assertEqual(FakeSession.created, [])
        started = self.attach()
        session_id = started["result"]["session_id"]
        self.assertEqual(FakeSession.created[0].create_time, 123.5)
        self.assertFalse(FakeSession.created[0].options.allow_pause)
        self.assertFalse(FakeSession.created[0].options.allow_kill)
        for operation in ("pause", "resume", "kill"):
            self.assertEqual(self.registry.handle(request(operation, {"session_id": session_id}))["error"]["code"], "forbidden")
        self.assertTrue(self.registry.handle(request("cancel", {"session_id": session_id}))["ok"])
        self.assertTrue(FakeSession.created[0].cancelled)

    def test_launch_is_confined_to_explicit_local_roots_and_literal_arguments(self):
        params = {"mode": "launch", "target": str(self.script), "args": ["x&y", "--pid", "literal :smile:"]}
        self.assertEqual(self.registry.handle(request("start", params))["error"]["code"], "forbidden")
        self.registry = AgentRegistry(self.root, AgentPolicy(allow_launch=True, launch_roots=(self.root,), allow_pause=True),
                                      session_type=FakeSession)
        self.assertTrue(self.registry.handle(request("start", params))["ok"])
        self.assertEqual(FakeSession.created[-1].args, params["args"])
        self.assertTrue(FakeSession.created[-1].options.allow_pause)
        with tempfile.TemporaryDirectory() as elsewhere:
            other = Path(elsewhere) / "other.py"
            other.write_text("pass")
            params["target"] = str(other)
            self.assertEqual(self.registry.handle(request("start", params))["error"]["code"], "forbidden")
        with self.assertRaises(ValueError):
            AgentPolicy(allow_launch=True)

    def test_request_replay_is_bounded_and_conflicting_identity_cannot_relaunch(self):
        original = request("start", {"mode": "attach", "pid": 987654, "create_time": 123.5})
        first = self.registry.handle(original)
        self.assertEqual(first, self.registry.handle(original))
        self.assertEqual(len(FakeSession.created), 1)
        original["params"]["create_time"] = 456
        self.assertEqual(self.registry.handle(original)["error"]["code"], "conflict")
        self.assertEqual(len(FakeSession.created), 1)
        for _ in range(300):
            self.registry.handle(request("status"))
        self.assertEqual(len(self.registry.replies), 256)

    def test_capacity_retention_and_failed_request_do_not_evict_unrelated_sessions(self):
        first, second = self.attach(), self.attach()
        self.assertEqual(self.attach()["error"]["code"], "capacity")
        FakeSession.created[0].finish(reason="failed")
        self.registry.refresh()
        self.assertEqual(self.registry.records[first["result"]["session_id"]].state, "failed")
        bad = self.attach(timeout_seconds=float("nan"))
        self.assertFalse(bad["ok"])
        self.assertIn(first["result"]["session_id"], self.registry.records)
        third = self.attach()
        self.assertTrue(third["ok"])
        self.assertNotIn(first["result"]["session_id"], self.registry.records)
        self.assertIn(second["result"]["session_id"], self.registry.records)
        self.assertFalse(FakeSession.created[1].cancelled)

    def test_event_cursor_discloses_evictions_and_export_rejects_output_paths(self):
        session_id = self.attach()["result"]["session_id"]
        for number in range(350):
            FakeSession.created[0].status.sample_count = number
            self.registry.refresh()
        result = self.registry.handle(request("events", {"session_id": session_id, "cursor": 0, "limit": 100}))["result"]
        self.assertEqual(result["retained"], 200)
        self.assertEqual(len(result["events"]), 100)
        self.assertGreater(result["dropped"], 100)
        self.assertEqual(result["cursor_gap"], result["dropped"])
        denied = self.registry.handle(request("export", {"session_id": session_id, "directory": "elsewhere"}))
        self.assertEqual(denied["error"]["code"], "invalid_input")
        exported = self.registry.handle(request("export", {"session_id": session_id}))["result"]
        self.assertEqual(exported["reports"], ["evidence.json"])
        self.assertEqual(exported["directory"], str(self.root))

    def test_input_bounds_and_command_timeout_uncertainty_are_explicit(self):
        invalid = [{"protocol": True}, {"id": "../wrong"}, {"extra": "unsupported"}]
        for overrides in invalid:
            value = request("status") | overrides
            self.assertFalse(self.registry.handle(value)["ok"])
        self.assertEqual(self.registry.handle(request("missing"))["error"]["code"], "unknown_operation")
        session_id = self.attach()["result"]["session_id"]
        with mock.patch.object(FakeSession.created[0], "export", side_effect=CommandTimeoutError(True)):
            response = self.registry.handle(request("export", {"session_id": session_id}))
        self.assertEqual(response["error"]["code"], "deadline")
        self.assertTrue(response["error"]["may_have_run"])
        self.assertTrue(self.registry.close())
        self.assertEqual(self.attach()["error"]["code"], "unavailable")


@unittest.skipUnless(os.name == "nt", "authenticated native Windows agent")
class NativeAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = "accept-" + uuid.uuid4().hex[:16]
        self.server = AgentServer(self.root / "agent", profile=self.profile)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.server.serve, args=(self.stop,))
        self.thread.start()
        self.addCleanup(self._stop)
        self.client = AgentClient(self.root / "agent", profile=self.profile)

    def _stop(self):
        self.stop.set()
        self.thread.join(20)
        self.assertFalse(self.thread.is_alive())

    def test_authenticated_observation_export_and_cancel_leave_attached_target_running(self):
        target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(target.wait, 5)
        self.addCleanup(target.kill)
        created = psutil.Process(target.pid).create_time()
        accepted = self.client.request("start", {"mode": "attach", "pid": target.pid, "create_time": created,
                                                "timeout_seconds": 10, "sample_interval_ms": 100,
                                                "windows_seconds": [1]})
        session_id = accepted["session_id"]
        wait_for(lambda: self.client.request("status", {"session_id": session_id})["sample_count"] >= 2)
        exported = self.client.request("export", {"session_id": session_id})
        self.assertEqual({Path(name).suffix for name in exported["reports"]}, {".json", ".html"})
        report = json.loads((Path(exported["directory"]) / next(name for name in exported["reports"] if name.endswith(".json"))).read_text())
        self.assertEqual(report["root_pid"], target.pid)
        self.client.request("cancel", {"session_id": session_id})
        wait_for(lambda: self.client.request("status", {"session_id": session_id})["state"] == "completed")
        final = self.client.request("status", {"session_id": session_id})
        self.assertEqual(final["result"]["cleanup"], "complete")
        self.assertIsNone(target.poll())
        self.assertGreater(self.client.request("events", {"session_id": session_id})["observed"], 0)

    def test_permitted_launch_pause_resume_and_cancel_clean_up_only_owned_tree(self):
        self._stop()
        script = self.root / "tree.py"
        child_file = self.root / "child.pid"
        script.write_text("import pathlib,subprocess,sys,time\n"
                          "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                          f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid))\n"
                          "time.sleep(60)\n", encoding="utf-8")
        self.server = AgentServer(self.root / "agent", profile=self.profile,
                                  policy=AgentPolicy(allow_launch=True, allow_pause=True, launch_roots=(self.root,)))
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.server.serve, args=(self.stop,))
        self.thread.start()
        accepted = self.client.request("start", {"mode": "launch", "target": str(script),
                                                "timeout_seconds": 10, "sample_interval_ms": 100,
                                                "windows_seconds": [1]})
        session_id = accepted["session_id"]
        wait_for(child_file.exists)
        child_pid = int(child_file.read_text())
        wait_for(lambda: self.client.request("status", {"session_id": session_id})["sample_count"] >= 2)
        self.assertTrue(self.client.request("pause", {"session_id": session_id})["completed"])
        self.assertEqual(psutil.Process(child_pid).status(), psutil.STATUS_STOPPED)
        self.assertTrue(self.client.request("resume", {"session_id": session_id})["completed"])
        self.assertNotEqual(psutil.Process(child_pid).status(), psutil.STATUS_STOPPED)
        with self.assertRaises(AgentError) as denied:
            self.client.request("kill", {"session_id": session_id})
        self.assertEqual(denied.exception.code, "forbidden")
        self.client.request("cancel", {"session_id": session_id})
        wait_for(lambda: self.client.request("status", {"session_id": session_id})["state"] == "completed")
        from src.process_runner import _windows_process_exited
        wait_for(lambda: not psutil.pid_exists(child_pid) or _windows_process_exited(child_pid))

    def test_reused_pid_identity_fails_without_controlling_target(self):
        target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(target.wait, 5)
        self.addCleanup(target.kill)
        accepted = self.client.request("start", {"mode": "attach", "pid": target.pid,
                                                "create_time": psutil.Process(target.pid).create_time() - 1})
        wait_for(lambda: self.client.request("status", {"session_id": accepted["session_id"]})["state"] == "failed")
        self.assertIsNone(target.poll())


if __name__ == "__main__":
    unittest.main()
