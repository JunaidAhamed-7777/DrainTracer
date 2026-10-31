"""Windows per-user local IPC: private ACLs, bounded JSON and mutual authentication.

This module never imports the dashboard, executes requests, or opens a network
socket. Named-pipe I/O is nonblocking so incomplete peers have a deadline.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import struct
import time
from contextlib import contextmanager
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
from typing import Any

PROTOCOL = 1
MAX_FRAME = 1024 * 1024
IO_TIMEOUT = 3.0
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class AgentTransportError(OSError):
    """IPC is unavailable, unauthorized, malformed or past its deadline."""


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID),
                ("inherit", wintypes.BOOL)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]


@lru_cache(maxsize=1)
def _api():
    if os.name != "nt":
        raise AgentTransportError("the local agent requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    signatures = {
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "GetDriveTypeW": ([wintypes.LPCWSTR], wintypes.UINT),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "LocalFree": ([wintypes.LPVOID], wintypes.LPVOID),
        "CreateDirectoryW": ([wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)], wintypes.BOOL),
        "CreateNamedPipeW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(_SecurityAttributes)], wintypes.HANDLE),
        "ConnectNamedPipe": ([wintypes.HANDLE, wintypes.LPVOID], wintypes.BOOL),
        "DisconnectNamedPipe": ([wintypes.HANDLE], wintypes.BOOL),
        "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                         ctypes.POINTER(_SecurityAttributes), wintypes.DWORD,
                         wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
        "SetNamedPipeHandleState": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                      wintypes.LPVOID, wintypes.LPVOID], wintypes.BOOL),
        "ReadFile": ([wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID], wintypes.BOOL),
        "WriteFile": ([wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                       ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    signatures = {
        "OpenProcessToken": ([wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
        "GetTokenInformation": ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "ConvertSidToStringSidW": ([wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)], wintypes.BOOL),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
            [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID], wintypes.BOOL),
        "GetNamedSecurityInfoW": ([wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
                                   ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
                                   ctypes.POINTER(wintypes.LPVOID)], wintypes.DWORD),
        "GetSecurityDescriptorControl": ([wintypes.LPVOID, ctypes.POINTER(wintypes.WORD),
                                           ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "GetAce": ([wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(security, name)
        function.argtypes, function.restype = arguments, result
    return kernel, security


def _check(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def _sid_string(pointer) -> str:
    kernel, security = _api()
    value = wintypes.LPWSTR()
    _check(security.ConvertSidToStringSidW(pointer, ctypes.byref(value)))
    try:
        return value.value
    finally:
        kernel.LocalFree(value)


@lru_cache(maxsize=1)
def current_user_sid() -> str:
    """The process token's user, rather than a username or environment variable."""
    kernel, security = _api()
    token = wintypes.HANDLE()
    _check(security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)))
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        _check(security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)))
        return _sid_string(ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.sid)
    finally:
        kernel.CloseHandle(token)


def process_user_sid(pid: int) -> str:
    """Read the target token owner without acquiring process-control rights."""
    kernel, security = _api()
    process = _check(kernel.OpenProcess(0x1000, False, pid))  # QUERY_LIMITED_INFORMATION
    token = wintypes.HANDLE()
    try:
        _check(security.OpenProcessToken(process, 0x0008, ctypes.byref(token)))
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        _check(security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)))
        return _sid_string(ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.sid)
    finally:
        if token.value:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


@contextmanager
def _attributes(*, inherit_children: bool = False):
    kernel, security = _api()
    descriptor = wintypes.LPVOID()
    sid = current_user_sid()
    flags = "OICI" if inherit_children else ""
    _check(security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"O:{sid}D:P(A;{flags};FA;;;{sid})(A;{flags};FA;;;SY)", 1, ctypes.byref(descriptor), None))
    try:
        yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    finally:
        kernel.LocalFree(descriptor)


def local_drive_path(path: str | Path) -> Path:
    """Reject UNC/device paths and mapped remote drives before any file mutation."""
    path = Path(path).absolute()
    if str(path).startswith("\\\\") or (os.name == "nt" and _api()[0].GetDriveTypeW(path.anchor) == 4):
        raise AgentTransportError("agent paths must be on a local drive")
    return path


def verify_trusted_path(path: str | Path) -> Path:
    """Refuse replacement/content authority held by another non-admin principal.

    A protected child DACL alone does not prevent a parent's DELETE_CHILD
    right from replacing it. Inspect each existing ancestor as well as the leaf.
    Read-only/traverse and create-new-child rights alone do not grant authority
    to replace an existing protected path. Inherit-only ACEs do not apply to
    the ancestor itself. Deny entries are conservatively not used to broaden
    the trusted set.
    """
    path = local_drive_path(path)
    if os.name != "nt":
        return path
    kernel, security = _api()
    trusted = {current_user_sid(), "S-1-5-18", "S-1-5-32-544", "S-1-3-4",
               "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}
    for part in (path, *path.parents):
        if not part.exists():
            continue
        metadata = part.stat(follow_symlinks=False)
        if getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise AgentTransportError("agent paths cannot traverse a reparse point")
        owner, dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()
        result = security.GetNamedSecurityInfoW(str(part), 1, 0x00000005, ctypes.byref(owner),
                                              None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if result:
            raise ctypes.WinError(result)
        try:
            # OWNER RIGHTS ACEs apply only to the object's owner; its SID
            # is trusted only after independently validating that owner.
            if _sid_string(owner) not in trusted - {"S-1-3-4"} or not dacl.value:
                raise AgentTransportError("agent path has an untrusted owner or unrestricted ancestor ACL")
            # DELETE, WRITE_DAC, WRITE_OWNER, DELETE_CHILD, WRITE_ATTRIBUTES,
            # WRITE_EA, GENERIC_WRITE and GENERIC_ALL can replace/mutate a path.
            unsafe = 0x500D0150
            if not part.is_dir():
                unsafe |= 0x00000006  # file WRITE_DATA / APPEND_DATA
            count = ctypes.c_ushort.from_address(dacl.value + 4).value
            for index in range(count):
                ace = wintypes.LPVOID()
                _check(security.GetAce(dacl, index, ctypes.byref(ace)))
                kind = ctypes.c_ubyte.from_address(ace.value).value
                flags = ctypes.c_ubyte.from_address(ace.value + 1).value
                if flags & 0x08 or kind == 1:  # INHERIT_ONLY or ACCESS_DENIED
                    continue
                if kind != 0:
                    raise AgentTransportError("agent path has an unsupported ancestor ACL entry")
                mask = wintypes.DWORD.from_address(ace.value + 4).value
                if mask & unsafe and _sid_string(ace.value + 8) not in trusted:
                    raise AgentTransportError("another principal can mutate or replace the agent path")
        finally:
            kernel.LocalFree(descriptor)
    return path


def _verify_private_acl(path: Path, *, inherited: bool = False) -> None:
    """Shared confidentiality check; inherited data files retain trusted principals only."""
    kernel, security = _api()
    path = verify_trusted_path(path)
    for part in (path, *path.parents):
        if part.exists() and getattr(part.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
            raise AgentTransportError("agent state cannot traverse a reparse point")
    owner, dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()
    result = security.GetNamedSecurityInfoW(str(path), 1, 0x00000005, ctypes.byref(owner),
                                          None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        if _sid_string(owner) != current_user_sid() or not dacl.value:
            raise AgentTransportError("agent state has an unexpected owner or unrestricted ACL")
        control, revision = wintypes.WORD(), wintypes.DWORD()
        _check(security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
        if not inherited and not control.value & 0x1000:  # SE_DACL_PROTECTED
            raise AgentTransportError("agent state ACL must not inherit permissions")
        # ACL header: revision, reserved, size, ACE count, reserved.
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        allowed = {current_user_sid(), "S-1-5-18"}
        if inherited:
            allowed |= {"S-1-5-32-544", "S-1-3-4",
                        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}
        owner_allowed = False
        for index in range(count):
            ace = wintypes.LPVOID()
            _check(security.GetAce(dacl, index, ctypes.byref(ace)))
            if ctypes.c_ubyte.from_address(ace.value).value != 0:  # ACCESS_ALLOWED_ACE_TYPE
                raise AgentTransportError("agent state ACL has an unsupported entry")
            sid = _sid_string(ace.value + 8)
            if sid not in allowed:
                raise AgentTransportError("agent state grants another principal access")
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            owner_allowed |= sid in {current_user_sid(), "S-1-3-4"} and not flags & 0x08
        if not owner_allowed:
            raise AgentTransportError("agent state does not grant its owner access")
    finally:
        kernel.LocalFree(descriptor)


def verify_private_path(path: Path) -> None:
    """Require a protected current-user/SYSTEM state or credential object."""
    _verify_private_acl(path)


def verify_private_data_path(path: Path) -> None:
    """Validate inherited SQLite/report sidecars without granting public read.

    The owner must remain the current user and all ancestors must be trusted.
    Inherited ACLs and OWNER RIGHTS are accepted only for that verified owner;
    SYSTEM, Administrators and TrustedInstaller remain the explicit privileged
    OS boundary. No ACL is changed, and any other principal's ACE is refused.
    """
    _verify_private_acl(path, inherited=True)


def private_directory(path: str | Path) -> Path:
    """Create only the requested leaf; preserve and validate any existing data."""
    kernel, _security = _api()
    path = local_drive_path(path)
    if not path.parent.is_dir():
        raise AgentTransportError("agent state parent directory must already exist")
    # Check mutation rights before creation; a private child ACL cannot
    # override an untrusted parent's authority to replace that child.
    verify_trusted_path(path.parent)
    # Check ancestors before CreateDirectory, and the final object afterward.
    for part in path.parents:
        if getattr(part.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
            raise AgentTransportError("agent state cannot traverse a reparse point")
    with _attributes(inherit_children=True) as attributes:
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)) and ctypes.get_last_error() != 183:
            raise ctypes.WinError(ctypes.get_last_error())
    verify_private_path(path)
    if not path.is_dir():
        raise AgentTransportError("agent state must be a directory")
    return path


def load_secret(directory: Path, *, create: bool = False) -> bytes:
    """A 256-bit local credential, protected by owner/SYSTEM ACLs and never logged."""
    kernel, _security = _api()
    verify_private_path(directory)
    path = directory / "credential"
    if create and not path.exists():
        with _attributes() as attributes:
            handle = kernel.CreateFileW(str(path), 0x40000000, 0, ctypes.byref(attributes), 1, 0x80, None)
        if handle == _INVALID_HANDLE:
            if ctypes.get_last_error() != 80:
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            try:
                raw = secrets.token_hex(32).encode("ascii")
                written = wintypes.DWORD()
                _check(kernel.WriteFile(handle, raw, len(raw), ctypes.byref(written), None))
                if written.value != len(raw):
                    raise AgentTransportError("could not save the complete agent credential")
            finally:
                kernel.CloseHandle(handle)
    verify_private_path(path)
    with path.open("rb") as handle:
        raw = handle.read(65)
    if not re.fullmatch(b"[0-9a-f]{64}", raw):
        raise AgentTransportError("agent credential is missing or malformed")
    return bytes.fromhex(raw.decode("ascii"))


def pipe_name(profile: str = "default") -> str:
    if not re.fullmatch("[a-z0-9][a-z0-9-]{0,31}", profile):
        raise AgentTransportError("agent profile must contain 1-32 lowercase letters, digits or hyphens")
    identity = hashlib.sha256(current_user_sid().encode("ascii")).hexdigest()[:24]
    return f"\\\\.\\pipe\\DrainTracer-{identity}-{profile}"


def _decode_frame(raw: bytes) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("non-finite number")

    def finite_float(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value

    quoted = escaped = False
    depth = tokens = scalar = 0
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted, scalar = True, 0
            tokens += 1
        elif byte in (91, 123):
            depth += 1
            tokens += 1
            scalar = 0
        elif byte in (93, 125):
            depth -= 1
            scalar = 0
        elif byte in (44, 58, 9, 10, 13, 32):
            scalar = 0
            tokens += 1
        else:
            scalar += 1
        if depth > 32 or tokens > 50000 or scalar > 128:
            raise AgentTransportError("local agent JSON exceeds its structural limits")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant, parse_float=finite_float)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AgentTransportError("malformed local agent JSON frame") from exc
    if not isinstance(value, dict):
        raise AgentTransportError("local agent frame must be an object")
    return value


class Pipe:
    """A single bounded exchange; stalled/truncated peers cannot hold a reader forever."""

    def __init__(self, handle, *, timeout: float = IO_TIMEOUT) -> None:
        self.handle = handle
        self.timeout = timeout

    def close(self) -> None:
        if self.handle is not None:
            _api()[0].CloseHandle(self.handle)
            self.handle = None

    def _transfer(self, data: bytes | int) -> bytes:
        kernel, _security = _api()
        reading = isinstance(data, int)
        size = data if reading else len(data)
        buffer = ctypes.create_string_buffer(size) if reading else ctypes.create_string_buffer(data)
        offset, deadline = 0, time.monotonic() + self.timeout
        while offset < size:
            transferred = wintypes.DWORD()
            function = kernel.ReadFile if reading else kernel.WriteFile
            ok = function(self.handle, ctypes.byref(buffer, offset), size - offset, ctypes.byref(transferred), None)
            if not ok and ctypes.get_last_error() not in (232, 536):  # empty/listening nonblocking pipe
                raise AgentTransportError("local agent connection ended")
            offset += transferred.value
            if offset < size:
                if time.monotonic() >= deadline:
                    raise AgentTransportError("local agent request exceeded its I/O deadline")
                time.sleep(0.005)
        return buffer.raw[:size] if reading else b""

    def send(self, value: dict[str, Any]) -> None:
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > MAX_FRAME:
            raise AgentTransportError("local agent frame exceeds its limit")
        self._transfer(struct.pack("!I", len(raw)) + raw)

    def receive(self) -> dict[str, Any]:
        size = struct.unpack("!I", self._transfer(4))[0]
        if not 1 <= size <= MAX_FRAME:
            raise AgentTransportError("local agent frame exceeds its limit")
        return _decode_frame(self._transfer(size))


class PipeListener(Pipe):
    def __init__(self, name: str, *, timeout: float = IO_TIMEOUT) -> None:
        kernel, _security = _api()
        with _attributes() as attributes:
            # FIRST_PIPE_INSTANCE refuses impersonation by an already-bound endpoint;
            # REJECT_REMOTE_CLIENTS prevents SMB clients even with valid credentials.
            handle = kernel.CreateNamedPipeW(name, 0x00080003, 0x00000009, 1,
                                             65536, 65536, 0, ctypes.byref(attributes))
        if handle == _INVALID_HANDLE:
            raise AgentTransportError("local agent endpoint is already in use or unavailable")
        super().__init__(handle, timeout=timeout)

    def accept(self) -> bool:
        kernel, _security = _api()
        if kernel.ConnectNamedPipe(self.handle, None) or ctypes.get_last_error() == 535:
            return True
        if ctypes.get_last_error() in (232, 536):
            return False
        raise AgentTransportError("could not accept a local agent connection")

    def disconnect(self) -> None:
        _api()[0].DisconnectNamedPipe(self.handle)


def connect(name: str, *, timeout: float = IO_TIMEOUT) -> Pipe:
    kernel, _security = _api()
    deadline = time.monotonic() + timeout
    while True:
        # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION prevents a server
        # from impersonating a connecting client to gain its file/process rights.
        handle = kernel.CreateFileW(name, 0xC0000000, 0, None, 3, 0x00110000, None)
        if handle != _INVALID_HANDLE:
            break
        error = ctypes.get_last_error()
        if error not in (2, 231) or time.monotonic() >= deadline:
            raise AgentTransportError("local agent is unavailable or access was denied")
        time.sleep(0.01)
    mode = wintypes.DWORD(1)  # byte mode, nonblocking
    if not kernel.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
        kernel.CloseHandle(handle)
        raise AgentTransportError("could not configure local agent connection")
    return Pipe(handle, timeout=timeout)


def _proof(secret: bytes, side: str, server: str, client: str) -> str:
    return hmac.new(secret, f"DrainTracer:{PROTOCOL}:{side}:{server}:{client}".encode("ascii"),
                    hashlib.sha256).hexdigest()


def authenticate(pipe: Pipe, secret: bytes, *, server: bool) -> None:
    """Fresh nonces, role separation, fixed protocol and no reusable credential on wire."""
    if server:
        nonce = secrets.token_hex(32)
        pipe.send({"protocol": PROTOCOL, "challenge": nonce})
        response = pipe.receive()
        client = response.get("challenge")
        if (response.get("protocol") != PROTOCOL or not isinstance(client, str)
                or not re.fullmatch("[0-9a-f]{64}", client)
                or not hmac.compare_digest(str(response.get("proof")), _proof(secret, "client", nonce, client))):
            raise AgentTransportError("local agent authentication failed")
        pipe.send({"protocol": PROTOCOL, "proof": _proof(secret, "server", nonce, client)})
    else:
        challenge = pipe.receive()
        nonce = challenge.get("challenge")
        if (challenge.get("protocol") != PROTOCOL or not isinstance(nonce, str)
                or not re.fullmatch("[0-9a-f]{64}", nonce)):
            raise AgentTransportError("local agent authentication failed")
        client = secrets.token_hex(32)
        pipe.send({"protocol": PROTOCOL, "challenge": client, "proof": _proof(secret, "client", nonce, client)})
        response = pipe.receive()
        if (response.get("protocol") != PROTOCOL
                or not hmac.compare_digest(str(response.get("proof")), _proof(secret, "server", nonce, client))):
            raise AgentTransportError("local agent authentication failed")
