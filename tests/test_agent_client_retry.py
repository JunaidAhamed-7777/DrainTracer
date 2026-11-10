"""Retry-safe agent client: request uncertainty, idempotent identities, one deadline.

The native transport is Windows-only; these tests use scripted pipes and a
mocked kernel so they also run on Linux.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401 - make the source checkout importable
from test_agent_service import FakeSession

from src import agent_transport
from src.agent_command import AgentClient, main
from src.agent_service import AgentPolicy, AgentRegistry
from src.agent_transport import AgentTransportError, Pipe

ATTACH = ["start", "--pid", "987654", "--create-time", "123.5"]


class ScriptedPipe:
    """Answers request frames from a registry; failure modes are scripted per exchange."""

    def __init__(self, registry, *, fail=None, step=0.0, deadline=None):
        self.registry, self.fail, self.step, self.deadline = registry, fail, step, deadline
        self.frames, self.pending, self.closed = [], None, False

    def _wait(self):
        # Mirror Pipe._transfer: each step may wait up to its own timeout,
        # but never past the shared exchange deadline.
        if self.deadline is not None and time.monotonic() + self.step >= self.deadline:
            time.sleep(max(0.0, self.deadline - time.monotonic()))
            raise AgentTransportError("local agent request exceeded its I/O deadline")
        time.sleep(self.step)

    def send(self, value):
        self._wait()
        if "ack" in value:
            if self.fail == "ack":
                raise AgentTransportError("local agent connection ended")
            self.frames.append(value)
            return
        if self.fail == "send":
            raise AgentTransportError("local agent connection ended")
        self.frames.append(value)
        self.pending = self.registry.handle(value) if self.registry is not None else {}

    def receive(self):
        self._wait()
        if self.fail == "reply":
            raise AgentTransportError("local agent request exceeded its I/O deadline")
        return self.pending

    def close(self):
        self.closed = True


class AgentClientRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="draintracer-agent-retry-")
        self.addCleanup(temporary.cleanup)
        FakeSession.created = []
        for patcher in (mock.patch("src.agent_service.process_user_sid", return_value="owner"),
                        mock.patch("src.agent_service.current_user_sid", return_value="owner"),
                        mock.patch("src.agent_command.load_secret", return_value=b"x" * 32),
                        mock.patch("src.agent_command.pipe_name", return_value="test-endpoint"),
                        mock.patch("src.agent_command.authenticate")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.registry = AgentRegistry(Path(temporary.name), AgentPolicy(), session_type=FakeSession)
        self.pipes, self.connects = [], []

    def script(self, *modes, step=0.0, refuse=False):
        modes = list(modes)

        def connect(name, *, timeout, deadline=None):
            self.connects.append((timeout, deadline))
            if refuse:
                raise AgentTransportError("local agent is unavailable or access was denied")
            pipe = ScriptedPipe(self.registry, fail=modes.pop(0) if modes else None, step=step, deadline=deadline)
            self.pipes.append(pipe)
            return pipe

        patcher = mock.patch("src.agent_command.connect", side_effect=connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, json.loads(output.getvalue())

    def test_lost_reply_after_send_reports_may_have_run_and_retry_replays(self):
        self.script("reply", None)
        code, first = self.run_cli(ATTACH)
        self.assertEqual(code, 2)
        self.assertTrue(first["error"]["may_have_run"])
        request_id = first["error"]["request_id"]
        self.assertEqual(str(uuid.UUID(request_id)), request_id)
        self.assertEqual(self.pipes[0].frames[0]["id"], request_id)
        self.assertEqual(len(FakeSession.created), 1)
        # Retrying with the reported identity returns the original reply
        # instead of starting a second session.
        code, retried = self.run_cli(["--request-id", request_id, *ATTACH])
        self.assertEqual(code, 0)
        self.assertTrue(retried["accepted"])
        self.assertEqual(len(FakeSession.created), 1)
        self.assertEqual(self.pipes[1].frames[0]["id"], request_id)
        self.assertTrue(all(pipe.closed for pipe in self.pipes))

    def test_reused_request_id_with_different_parameters_is_a_conflict(self):
        self.script()
        request_id = str(uuid.uuid4())
        self.assertEqual(self.run_cli(["--request-id", request_id, *ATTACH])[0], 0)
        code, conflict = self.run_cli(["--request-id", request_id, "start", "--pid", "987654",
                                       "--create-time", "456"])
        self.assertEqual(code, 2)
        self.assertEqual(conflict["error"]["code"], "conflict")
        self.assertFalse(conflict["error"]["may_have_run"])
        self.assertEqual(conflict["error"]["request_id"], request_id)
        self.assertEqual(len(FakeSession.created), 1)

    def test_failure_before_send_reports_not_run(self):
        for refuse, mode in ((True, None), (False, "send")):
            with self.subTest(refuse=refuse, mode=mode):
                FakeSession.created = []
                self.script(mode, refuse=refuse)
                code, failure = self.run_cli(ATTACH)
                self.assertEqual(code, 2)
                self.assertFalse(failure["error"]["may_have_run"])
                self.assertEqual(FakeSession.created, [])

    def test_ack_failure_after_valid_reply_returns_the_result(self):
        self.script("ack")
        code, result = self.run_cli(ATTACH)
        self.assertEqual(code, 0)
        self.assertTrue(result["accepted"])
        self.assertTrue(self.pipes[0].closed)

    def test_invalid_reply_identity_after_send_may_have_run(self):
        self.script()
        client = AgentClient("state")
        with mock.patch.object(self.registry, "handle", return_value={"protocol": 1, "id": "other", "ok": True}):
            with self.assertRaises(AgentTransportError) as raised:
                client.request("status", request_id=str(uuid.uuid4()))
        self.assertTrue(raised.exception.may_have_run)

    def test_request_id_is_validated_and_normalized(self):
        self.script()
        for value in ("not-a-uuid", "", "1234"):
            with self.subTest(value=value):
                code, failure = self.run_cli(["--request-id", value, "status"])
                self.assertEqual(code, 2)
                self.assertEqual(failure["error"]["code"], "invalid_input")
        for command in (["serve"], ["diagnose"], ["recover", str(uuid.uuid4())]):
            with self.subTest(command=command):
                code, failure = self.run_cli(["--request-id", str(uuid.uuid4()), *command])
                self.assertEqual(code, 2)
                self.assertEqual(failure["error"]["code"], "invalid_input")
        self.assertEqual(self.pipes, [])
        identity = uuid.uuid4()
        self.assertEqual(self.run_cli(["--request-id", str(identity).upper(), "status"])[0], 0)
        self.assertEqual(self.pipes[0].frames[0]["id"], str(identity))

    def test_one_deadline_bounds_the_whole_exchange(self):
        timeout = 0.3
        # Each of the five steps finishes just inside a per-step timeout; a
        # per-step deadline would let the exchange run for about 4x timeout.
        self.script(step=timeout * 0.8)
        started = time.monotonic()
        code, failure = self.run_cli(["--request-timeout", str(timeout), *ATTACH])
        elapsed = time.monotonic() - started
        self.assertEqual(code, 2)
        self.assertLess(elapsed, timeout * 1.8)
        connect_timeout, deadline = self.connects[0]
        self.assertEqual(connect_timeout, timeout)
        self.assertLessEqual(deadline - started, timeout + 0.05)
        # The request frame was written before the deadline expired.
        self.assertTrue(failure["error"]["may_have_run"])
        self.assertIn("request_id", failure["error"])


class PipeDeadlineTests(unittest.TestCase):
    """Pipe._transfer with a mocked kernel whose reads never produce data."""

    def setUp(self):
        kernel = mock.Mock()
        kernel.ReadFile.return_value = True  # nonblocking pipe: success with zero bytes
        patcher = mock.patch.object(agent_transport, "_api", return_value=(kernel, None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def elapsed(self, pipe):
        started = time.monotonic()
        with self.assertRaises(AgentTransportError):
            pipe.receive()
        return time.monotonic() - started

    def test_shared_deadline_caps_a_longer_per_step_timeout(self):
        self.assertLess(self.elapsed(Pipe(None, timeout=10, deadline=time.monotonic() + 0.1)), 1.0)

    def test_per_step_timeout_still_applies_without_a_deadline(self):
        self.assertGreaterEqual(self.elapsed(Pipe(None, timeout=0.1)), 0.1)


if __name__ == "__main__":
    unittest.main()
