"""Opt-in bounded TLS frames; no monitoring code depends on this module."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
import socketserver
import ssl
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_FRAME = 16 * 1024 * 1024
MAX_VALUES = 100_000
MAX_DEPTH = 32
ALPN = "draintracer-team/1"


class SharingError(ValueError):
    """A fixed machine code, without server exceptions or private payloads."""

    def __init__(self, code: str, message: str = "Sharing request could not complete"):
        self.code = code
        super().__init__(message)


def encode_message(value: dict) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    except (ValueError, TypeError, RecursionError, OverflowError) as error:
        raise SharingError("invalid") from error
    if len(raw) > MAX_FRAME:
        raise SharingError("quota")
    return raw


def decode_message(raw: bytes) -> dict:
    """Bound nesting/tokens/numeric scalars before allocating decoded objects."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_FRAME:
        raise SharingError("invalid")
    try:
        text = raw.decode("utf-8")
        depth = tokens = scalar_length = 0
        quoted = escaped = scalar = False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
                scalar = False
                tokens += 1
            elif character in "[{":
                depth += 1
                tokens += 1
                scalar = False
            elif character in "]}":
                depth -= 1
                scalar = False
            elif character.isspace() or character in ",:":
                scalar = False
            else:
                if not scalar:
                    tokens += 1
                    scalar_length = 0
                scalar = True
                scalar_length += 1
                if scalar_length > 128:
                    raise SharingError("invalid")
            if depth > MAX_DEPTH or tokens > MAX_VALUES:
                raise SharingError("quota")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise SharingError("invalid")
                result[key] = value
            return result

        def number(value):
            converted = float(value)
            if not math.isfinite(converted):
                raise SharingError("invalid")
            return converted

        result = json.loads(text, object_pairs_hook=pairs, parse_float=number,
                            parse_constant=lambda _value: (_ for _ in ()).throw(SharingError("invalid")))
        if not isinstance(result, dict):
            raise SharingError("invalid")
        return result
    except (UnicodeError, ValueError, RecursionError, OverflowError) as error:
        if isinstance(error, SharingError):
            raise
        raise SharingError("invalid") from error


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SharingError("unavailable")
    return remaining


def _receive(connection: socket.socket, count: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        connection.settimeout(_remaining(deadline))
        part = connection.recv(min(65536, count - len(chunks)))
        if not part:
            raise SharingError("unavailable")
        chunks.extend(part)
    return bytes(chunks)


def receive_frame(connection: socket.socket, deadline: float) -> dict:
    size = struct.unpack("!I", _receive(connection, 4, deadline))[0]
    if not 1 <= size <= MAX_FRAME:
        raise SharingError("quota")
    message = decode_message(_receive(connection, size, deadline))
    _remaining(deadline)
    return message


def send_frame(connection: socket.socket, value: dict, deadline: float) -> None:
    raw = encode_message(value)
    connection.settimeout(_remaining(deadline))
    connection.sendall(struct.pack("!I", len(raw)) + raw)


@dataclass(frozen=True)
class Endpoint:
    hostname: str
    port: int
    connect_ip: str

    @classmethod
    def parse(cls, value: str, connect_ip: str | None = None) -> Endpoint:
        try:
            if not isinstance(value, str) or len(value) > 512 or any(ord(c) <= 32 for c in value):
                raise ValueError
            parts = urlsplit(value)
            host = parts.hostname
            port = parts.port
            if (parts.scheme != "tls"
                    or not host or not port or parts.username is not None or parts.password is not None
                    or parts.path or parts.query or parts.fragment or not 1 <= port <= 65535
                    or not re.fullmatch(r"[A-Za-z0-9:.\-]{1,253}", host)):
                raise ValueError
            if connect_ip is None:
                connect_ip = "127.0.0.1" if host == "localhost" else str(ipaddress.ip_address(host))
            address = str(ipaddress.ip_address(connect_ip))
            return cls(host, port, address)
        except (TypeError, ValueError) as error:
            raise SharingError("invalid", "Use tls://host:port and an explicit IP for DNS hostnames") from error


def client_context(cafile=None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_default_certs()
    if cafile is not None:
        context.load_verify_locations(cafile=str(cafile))
    # Never enable TLS session-key logging from ambient SSLKEYLOGFILE.
    context.keylog_filename = None
    context.set_alpn_protocols([ALPN])
    return context


def request(endpoint: Endpoint, message: dict, *, cafile=None, timeout: float = 10) -> dict:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise SharingError("invalid")
    raw = encode_message(message)
    try:
        context = client_context(cafile)
    except (OSError, ssl.SSLError) as error:
        raise SharingError("certificate", "TLS trust configuration could not be loaded") from error
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((endpoint.connect_ip, endpoint.port), timeout=_remaining(deadline)) as plain:
            plain.settimeout(_remaining(deadline))
            with context.wrap_socket(plain, server_hostname=endpoint.hostname) as connection:
                if connection.selected_alpn_protocol() != ALPN:
                    raise SharingError("certificate", "Server did not negotiate the team protocol")
                connection.settimeout(_remaining(deadline))
                connection.sendall(struct.pack("!I", len(raw)) + raw)
                return receive_frame(connection, deadline)
    except ssl.SSLCertVerificationError as error:
        raise SharingError("certificate", "Server certificate or hostname verification failed") from error
    except (OSError, ssl.SSLError) as error:
        raise SharingError("unavailable") from error


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        deadline = time.monotonic() + self.server.request_timeout
        try:
            message = receive_frame(self.request, deadline)
            response = self.server.dispatch(message)
        except SharingError as error:
            response = {"team_schema": 1, "ok": False, "error": {"code": error.code}}
        except TimeoutError:
            response = {"team_schema": 1, "ok": False, "error": {"code": "unavailable"}}
        except (OSError, ValueError, TypeError, RuntimeError):
            response = {"team_schema": 1, "ok": False, "error": {"code": "storage"}}
        send_frame(self.request, response, deadline)


class SharingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Four admitted TLS handshakes/requests; no unbounded thread queue."""

    daemon_threads = True
    block_on_close = False
    request_queue_size = 8
    allow_reuse_address = False

    def __init__(self, address, context: ssl.SSLContext, dispatch: Callable[[dict], dict], *, timeout=10):
        host, port = address
        parsed = ipaddress.ip_address(host)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise SharingError("invalid")
        if context.minimum_version < ssl.TLSVersion.TLSv1_2:
            raise SharingError("invalid", "TLS 1.2 or newer is required")
        self.address_family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
        self.context, self.dispatch, self.request_timeout = context, dispatch, timeout
        self._slots = threading.BoundedSemaphore(4)
        super().__init__((host, port), _Handler)

    def process_request(self, connection, address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(connection)
            return
        try:
            super().process_request(connection, address)
        except BaseException:
            self._slots.release()
            self.shutdown_request(connection)
            raise

    def process_request_thread(self, connection, address):
        try:
            connection.settimeout(self.request_timeout)
            with self.context.wrap_socket(connection, server_side=True) as secured:
                if secured.selected_alpn_protocol() != ALPN:
                    return
                super().process_request_thread(secured, address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(connection)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Never copy tracebacks, request payloads or bearer tokens into logs.
        return
