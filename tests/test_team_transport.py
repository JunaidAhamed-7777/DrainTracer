"""Real TLS, framing limits, certificate refusals and admitted-worker bounds."""

from __future__ import annotations

import socket
import ssl
import struct
import threading
import time
import unittest
from pathlib import Path

from src.team_transport import (
    MAX_FRAME,
    Endpoint,
    SharingError,
    SharingServer,
    client_context,
    decode_message,
    receive_frame,
    request,
)

FIXTURES = Path(__file__).parent / "fixtures" / "team-tls"


class DecoderTests(unittest.TestCase):
    def test_untrusted_shapes_duplicate_keys_numbers_and_depth_are_refused(self):
        for raw in (b'[]', b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":1e999}',
                    b'{"x":' + b'1' * 129 + b'}', b'{"x":' + b'[' * 33 + b'0' + b']' * 33 + b'}',
                    b'\xff', b'{"x":'):
            with self.subTest(raw=raw[:40]), self.assertRaises(SharingError):
                decode_message(raw)
        self.assertEqual(decode_message(b'{"known":0,"unknown":null}'), {"known": 0, "unknown": None})

    def test_endpoint_does_not_silently_enable_cleartext_credentials_or_dns(self):
        for value in ("http://localhost:443", "tls://u:p@localhost:443", "tls://localhost:443/path",
                      "tls://localhost:443?token=x", "tls://example.org:443", "tls://localhost", None):
            with self.subTest(value=value), self.assertRaises(SharingError):
                Endpoint.parse(value)
        self.assertEqual(Endpoint.parse("tls://example.org:8443", "127.0.0.1").hostname, "example.org")
        with self.assertRaises(SharingError):
            Endpoint.parse("tls://localhost:8443", "unresolved.example.org")

    def test_client_context_always_checks_chain_and_hostname(self):
        context = client_context(FIXTURES / "cert.pem")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertIsNone(context.keylog_filename)


class TLSRoundTripTests(unittest.TestCase):
    def setUp(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(FIXTURES / "cert.pem", FIXTURES / "key.pem")
        self.received = []

        def dispatch(message):
            self.received.append(message)
            return {"team_schema": 1, "ok": True, "result": message}

        self.server = SharingServer(("127.0.0.1", 0), context, dispatch, timeout=1)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.endpoint = Endpoint.parse(f"tls://localhost:{self.port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_verified_transport_uses_one_logical_request_and_no_cleartext_fallback(self):
        message = {"operation": "whoami", "token": "test-only", "project_id": "project"}
        response = request(self.endpoint, message, cafile=FIXTURES / "cert.pem")
        self.assertEqual(response["result"], message)
        self.assertEqual(self.received, [message])
        with self.assertRaises(SharingError) as refused:
            request(self.endpoint, message)
        self.assertEqual(refused.exception.code, "certificate")
        wrong = Endpoint.parse(f"tls://wrong.invalid:{self.port}", "127.0.0.1")
        with self.assertRaises(SharingError) as refused:
            request(wrong, message, cafile=FIXTURES / "cert.pem")
        self.assertEqual(refused.exception.code, "certificate")
        self.assertEqual(len(self.received), 1)

    def test_declared_oversize_and_duplicate_payload_never_reach_dispatch(self):
        for raw in (struct.pack("!I", MAX_FRAME + 1),
                    struct.pack("!I", 13) + b'{"x":1,"x":2}'):
            with (socket.create_connection(("127.0.0.1", self.port), timeout=1) as plain,
                  client_context(FIXTURES / "cert.pem").wrap_socket(plain, server_hostname="localhost") as secure):
                secure.sendall(raw)
                reply = receive_frame(secure, time.monotonic() + 2)
                self.assertFalse(reply["ok"])
        self.assertEqual(self.received, [])

    def test_four_stalled_handshakes_cannot_create_unbounded_workers(self):
        stalled = [socket.create_connection(("127.0.0.1", self.port), timeout=1) for _ in range(4)]
        try:
            deadline = time.monotonic() + 0.5
            while self.server._slots._value and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(self.server._slots._value, 0)
            with self.assertRaises(SharingError):
                request(self.endpoint, {"operation": "whoami"}, cafile=FIXTURES / "cert.pem", timeout=0.5)
        finally:
            for connection in stalled:
                connection.close()
        deadline = time.monotonic() + 2
        while self.server._slots._value != 4 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.server._slots._value, 4)
        self.assertTrue(request(self.endpoint, {}, cafile=FIXTURES / "cert.pem")["ok"])

    def test_slow_partial_body_has_an_absolute_deadline(self):
        with (socket.create_connection(("127.0.0.1", self.port), timeout=1) as plain,
              client_context(FIXTURES / "cert.pem").wrap_socket(plain, server_hostname="localhost") as secure):
            secure.sendall(struct.pack("!I", 100) + b'{')
            started = time.monotonic()
            secure.settimeout(2)
            header = secure.recv(4)
            if header:
                self.assertEqual(len(header), 4)
                size = struct.unpack("!I", header)[0]
                self.assertLess(size, 1000)
                body = bytearray()
                while len(body) < size:
                    body.extend(secure.recv(size - len(body)))
                self.assertEqual(decode_message(bytes(body))["error"]["code"], "unavailable")
            self.assertLess(time.monotonic() - started, 1.8)
        self.assertEqual(self.received, [])


if __name__ == "__main__":
    unittest.main()
