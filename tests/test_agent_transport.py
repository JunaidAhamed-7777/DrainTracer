"""Native local-pipe ACLs, mutual authentication and bounded malformed-peer handling."""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401 - make the source checkout importable

from src.agent_transport import (
    AgentTransportError,
    Pipe,
    PipeListener,
    _api,
    _decode_frame,
    authenticate,
    connect,
    current_user_sid,
    load_secret,
    pipe_name,
    private_directory,
    process_user_sid,
    verify_private_data_path,
    verify_private_path,
)


class FrameTests(unittest.TestCase):
    def test_json_is_strict_and_structurally_bounded_before_decoding(self) -> None:
        self.assertEqual(_decode_frame(b'{"message":"[not nesting]","n":0}')["n"], 0)
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b'[]', b'\xff',
                    b'{"n":' + b'1' * 129 + b'}', b'{"a":' + b'[' * 33 + b'0' + b']' * 33 + b'}'):
            with self.subTest(raw=raw[:50]), self.assertRaises(AgentTransportError):
                _decode_frame(raw)
        with (mock.patch("src.agent_transport.json.loads") as decode,
              self.assertRaises(AgentTransportError)):
            _decode_frame(b'{"a":' + b'[' * 33)
        decode.assert_not_called()

    def test_oversized_frame_is_refused_before_payload_read(self) -> None:
        pipe = Pipe(None)
        with (mock.patch.object(pipe, "_transfer", return_value=struct.pack("!I", 1024 * 1024 + 1)) as transfer,
              self.assertRaises(AgentTransportError)):
            pipe.receive()
        transfer.assert_called_once_with(4)

    @unittest.skipIf(os.name == "nt", "Windows has the native transport")
    def test_nonwindows_fails_explicitly_without_network_fallback(self) -> None:
        with self.assertRaisesRegex(AgentTransportError, "requires Windows"):
            pipe_name()


@unittest.skipUnless(os.name == "nt", "native Windows local IPC")
class NativeTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".draintracer-agent-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = private_directory(Path(temporary.name) / "agent")
        self.secret = load_secret(self.directory, create=True)
        self.name = pipe_name("test-" + uuid.uuid4().hex[:20])
        self.listener = PipeListener(self.name, timeout=0.3)
        self.addCleanup(self.listener.close)

    def _serve(self, action):
        errors = []

        def run():
            try:
                deadline = time.monotonic() + 3
                while not self.listener.accept():
                    if time.monotonic() > deadline:
                        raise TimeoutError("test client did not connect")
                    time.sleep(0.005)
                action(self.listener)
            except (OSError, AssertionError, TimeoutError) as exc:
                errors.append(exc)
            finally:
                self.listener.disconnect()

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 5)
        return thread, errors

    def test_private_state_and_first_instance_are_enforced(self) -> None:
        self.assertEqual(process_user_sid(os.getpid()), current_user_sid())
        verify_private_path(self.directory)
        verify_private_path(self.directory / "credential")
        self.assertEqual(load_secret(self.directory), self.secret)
        self.assertEqual(len(self.secret), 32)
        with self.assertRaises(AgentTransportError):
            PipeListener(self.name)
        with self.assertRaises(AgentTransportError):
            verify_private_path(self.directory.parent)
        subprocess.run(["icacls", str(self.directory / "credential"), "/grant", "*S-1-1-0:(R)"],
                       check=True, capture_output=True)
        with self.assertRaisesRegex(AgentTransportError, "another principal"):
            load_secret(self.directory)

    def test_inherited_data_files_accept_only_verified_owner_and_privileged_principals(self) -> None:
        inherited = self.directory / "database.sqlite3"
        inherited.write_bytes(b"private database placeholder")
        verify_private_data_path(inherited)
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as temporary:
            temporary.write(b"private temporary or SQLite sidecar")
            protected = Path(temporary.name)
        verify_private_data_path(protected)
        subprocess.run(["icacls", str(inherited), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True)
        with self.assertRaisesRegex(AgentTransportError, "another principal"):
            verify_private_data_path(inherited)
        self.assertEqual(inherited.read_bytes(), b"private database placeholder")

    def test_public_parent_cannot_replace_a_private_state_leaf(self) -> None:
        parent = self.directory / "public-parent"
        parent.mkdir()
        subprocess.run(["icacls", str(parent), "/grant", "*S-1-1-0:(DC)"], check=True, capture_output=True)
        destination = parent / "new-state"
        kernel, _security = _api()
        with (mock.patch.object(kernel, "CreateDirectoryW") as create,
              self.assertRaisesRegex(AgentTransportError, "mutate or replace")):
            private_directory(destination)
        create.assert_not_called()
        self.assertFalse(destination.exists())

    def test_remote_state_is_rejected_before_directory_creation(self) -> None:
        kernel, _security = _api()
        with (mock.patch.object(kernel, "CreateDirectoryW") as create,
              self.assertRaisesRegex(AgentTransportError, "local drive")):
            private_directory(r"\\unreachable-server\share\agent")
        create.assert_not_called()
        with (mock.patch.object(kernel, "GetDriveTypeW", return_value=4),
              mock.patch.object(kernel, "CreateDirectoryW") as create,
              self.assertRaisesRegex(AgentTransportError, "local drive")):
            private_directory(self.directory / "mapped-remote")
        create.assert_not_called()

    def test_authenticated_roundtrip_and_client_server_role_separation(self) -> None:
        def server(pipe):
            authenticate(pipe, self.secret, server=True)
            pipe.send({"echo": pipe.receive()})
            self.assertEqual(pipe.receive(), {"ack": True})

        thread, errors = self._serve(server)
        client = connect(self.name)
        try:
            authenticate(client, self.secret, server=False)
            client.send({"request": "bounded status"})
            self.assertEqual(client.receive(), {"echo": {"request": "bounded status"}})
            client.send({"ack": True})
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        finally:
            client.close()

    def test_wrong_credential_cannot_read_authenticated_data(self) -> None:
        authenticated = []

        def server(pipe):
            authenticate(pipe, self.secret, server=True)
            authenticated.append(True)
            pipe.send({"private": "session data"})

        thread, errors = self._serve(server)
        client = connect(self.name, timeout=0.3)
        try:
            with self.assertRaises(AgentTransportError):
                authenticate(client, b"wrong" * 8, server=False)
            thread.join(3)
            self.assertEqual(authenticated, [])
            self.assertTrue(errors)
            self.assertIsInstance(errors[0], AgentTransportError)
        finally:
            client.close()

    def test_anonymous_principal_is_denied_by_the_pipe_acl(self) -> None:
        kernel, security = _api()
        kernel.GetCurrentThread.restype = ctypes.c_void_p
        security.ImpersonateAnonymousToken.argtypes = [ctypes.c_void_p]
        security.ImpersonateAnonymousToken.restype = ctypes.c_int
        security.RevertToSelf.argtypes = []
        security.RevertToSelf.restype = ctypes.c_int
        self.assertTrue(security.ImpersonateAnonymousToken(kernel.GetCurrentThread()))
        try:
            with self.assertRaises(AgentTransportError):
                connect(self.name, timeout=0.1)
        finally:
            self.assertTrue(security.RevertToSelf())

    def test_incomplete_peer_cannot_hold_the_reader(self) -> None:
        thread, errors = self._serve(lambda pipe: pipe.receive())
        client = connect(self.name)
        try:
            client._transfer(struct.pack("!I", 100) + b"{")
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIn("deadline", str(errors[0]))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
