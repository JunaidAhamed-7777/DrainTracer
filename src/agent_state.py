"""Bounded, exclusive and atomic local-agent scheduling state, never process actions."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from .agent_transport import (
    AgentTransportError,
    _decode_frame,
    verify_private_data_path,
    verify_trusted_path,
)
from .checkpoints import _lock

MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_INVENTORY = 4096
STATES = {"accepted", "starting", "running", "stopping", "completed", "failed", "interrupted"}


class AgentStateError(ValueError):
    """Scheduling storage is missing, incompatible, malformed or exhausted."""


def _identity(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise AgentStateError("agent state contains an invalid identity")


def _number(value, *, nullable=False):
    if nullable and value is None:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise AgentStateError("agent state contains an invalid numeric value")


def _version(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", value):
        raise AgentStateError("agent state needs a full semantic version")
    return tuple(map(int, value[1:].split(".")))


def relative_report(value):
    if not isinstance(value, str) or not value or len(value) > 512 or "\0" in value:
        raise AgentStateError("agent report path is malformed")
    path = Path(value)
    if path.is_absolute() or path.drive or ".." in path.parts or path.suffix not in {".json", ".html"}:
        raise AgentStateError("agent report path must stay within its report directory")
    if not re.fullmatch(r"leak_report_[0-9a-f-]{36}\.(json|html)", path.name):
        raise AgentStateError("agent report filename is malformed")
    _identity(path.stem.removeprefix("leak_report_"))
    return path


def _validate(data, *, app_version, owner):
    required = {"schema", "protocol", "version", "owner", "state_id", "updated_at", "sessions", "tombstones"}
    if not isinstance(data, dict) or set(data) != required or type(data["schema"]) is not int or data["schema"] != 1:
        raise AgentStateError("unsupported agent scheduling state schema")
    if type(data["protocol"]) is not int or data["protocol"] != 1 or data["owner"] != owner:
        raise AgentStateError("agent scheduling state has a different protocol or owner")
    writer, current = _version(data["version"]), _version(app_version)
    if writer[0] != current[0] or writer > current:
        raise AgentStateError("agent state needs a compatible version at least as new as its writer")
    _identity(data["state_id"])
    _number(data["updated_at"])
    if not isinstance(data["sessions"], list) or len(data["sessions"]) > 256:
        raise AgentStateError("agent scheduling state exceeds its record limit")
    if not isinstance(data["tombstones"], list) or len(data["tombstones"]) > 256:
        raise AgentStateError("agent scheduling state exceeds its request limit")
    seen = set()
    fields = {"session_id", "mode", "state", "request_id", "request_digest", "requested_at", "updated_at",
              "pid", "create_time", "deadline", "checkpoint", "writer_identity", "sample_count", "measurement",
              "full_coverage", "result", "reports", "identity_status"}
    for record in data["sessions"]:
        if not isinstance(record, dict) or set(record) != fields:
            raise AgentStateError("agent session record has unsupported fields")
        _identity(record["session_id"])
        _identity(record["request_id"])
        if record["session_id"] in seen:
            raise AgentStateError("agent session identities are duplicated")
        seen.add(record["session_id"])
        if not isinstance(record["request_digest"], str) or not re.fullmatch("[0-9a-f]{64}", record["request_digest"]):
            raise AgentStateError("agent session request digest is malformed")
        if record["mode"] not in {"launch", "attach"} or record["state"] not in STATES:
            raise AgentStateError("agent session state or ownership is unsupported")
        for key in ("requested_at", "updated_at", "deadline"):
            _number(record[key])
        if record["pid"] is not None and (type(record["pid"]) is not int or not 1 <= record["pid"] < 2**32):
            raise AgentStateError("agent target PID is malformed")
        _number(record["create_time"], nullable=True)
        checkpoint = record["checkpoint"]
        if checkpoint != f"{record['session_id']}.json":
            raise AgentStateError("agent checkpoint must match its assigned session path")
        writer = record["writer_identity"]
        if not isinstance(writer, dict) or set(writer) != {"pid", "create_time"}:
            raise AgentStateError("agent writer identity is malformed")
        if type(writer["pid"]) is not int or not 1 <= writer["pid"] < 2**32:
            raise AgentStateError("agent writer PID is malformed")
        _number(writer["create_time"])
        if type(record["sample_count"]) is not int or record["sample_count"] < 0:
            raise AgentStateError("agent sample count is malformed")
        if record["measurement"] not in {"conclusive", "partial", "inconclusive"} or type(record["full_coverage"]) is not bool:
            raise AgentStateError("agent measurement status is malformed")
        if record["identity_status"] not in {"not_checked", "identity_matches", "reused", "exited", "unknown", "unavailable"}:
            raise AgentStateError("agent identity diagnostic is malformed")
        result = record["result"]
        if (record["state"] in {"completed", "failed", "interrupted"}) != (result is not None):
            raise AgentStateError("agent result and terminal state disagree")
        if result is not None:
            keys = {"reason", "exit_code", "startup", "cleanup", "measurement", "full_coverage", "target_exit_code", "errors", "error_count"}
            if not isinstance(result, dict) or set(result) != keys:
                raise AgentStateError("agent result is malformed")
            for key in ("reason", "startup", "cleanup", "measurement"):
                if not isinstance(result[key], str) or len(result[key]) > 64:
                    raise AgentStateError("agent result status is malformed")
            for key in ("exit_code", "target_exit_code"):
                if result[key] is not None and (type(result[key]) is not int or not -(2**32) <= result[key] < 2**32):
                    raise AgentStateError("agent result exit code is malformed")
            if type(result["full_coverage"]) is not bool or type(result["error_count"]) is not int or result["error_count"] < 0:
                raise AgentStateError("agent result coverage/error count is malformed")
            if (not isinstance(result["errors"], list) or len(result["errors"]) > 8
                    or any(not isinstance(error, str) or len(error) > 512 for error in result["errors"])):
                raise AgentStateError("agent result errors exceed their bounds")
        if not isinstance(record["reports"], list) or len(record["reports"]) > 2:
            raise AgentStateError("agent report paths exceed their bound")
        for report in record["reports"]:
            relative_report(report)
    seen = set()
    for item in data["tombstones"]:
        if not isinstance(item, dict) or set(item) != {"request_id", "digest", "session_id", "expires_at"}:
            raise AgentStateError("agent request tombstone is malformed")
        _identity(item["request_id"])
        _identity(item["session_id"])
        if item["request_id"] in seen or not isinstance(item["digest"], str) or not re.fullmatch("[0-9a-f]{64}", item["digest"]):
            raise AgentStateError("agent request tombstone identity/digest is malformed")
        seen.add(item["request_id"])
        _number(item["expires_at"])
    return data


def read_agent_state(directory: Path, *, app_version: str, owner: str):
    """Read-only, bounded inspection; never acquire writer ownership or rewrite state."""
    path = verify_trusted_path(directory / "agent-state.json")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise AgentStateError("agent scheduling state exceeds its byte limit")
        return _validate(_decode_frame(raw), app_version=app_version, owner=owner)
    except AgentStateError:
        raise
    except (AgentTransportError, ValueError, TypeError, OverflowError) as error:
        raise AgentStateError("agent scheduling state is malformed; prior data preserved") from error


class AgentJournal:
    def __init__(self, directory: Path, *, app_version: str, owner: str,
                 retention_seconds=7 * 86400, max_storage_bytes=512 * 1024 * 1024):
        if (type(retention_seconds) not in (int, float) or not math.isfinite(retention_seconds)
                or not 1 <= retention_seconds <= 90 * 86400):
            raise AgentStateError("agent record retention must be from one second through 90 days")
        if type(max_storage_bytes) is not int or not 1024 * 1024 <= max_storage_bytes <= 64 * 1024**3:
            raise AgentStateError("agent storage admission limit must be from 1 MiB through 64 GiB")
        self.directory = directory
        self.path = directory / "agent-state.json"
        self.app_version, self.owner = app_version, owner
        self.retention_seconds = retention_seconds
        self.max_storage_bytes = max_storage_bytes
        self._lock = _lock(directory / "agent-state.lock")
        try:
            verify_trusted_path(self.path)
            if self.path.exists():
                if os.name == "nt":
                    verify_private_data_path(self.path)
                self.data = read_agent_state(directory, app_version=app_version, owner=owner)
            else:
                self.data = {"schema": 1, "protocol": 1, "version": app_version, "owner": owner,
                             "state_id": str(uuid.uuid4()), "updated_at": time.time(), "sessions": [], "tombstones": []}
        except AgentStateError:
            self.close()
            raise
        except (AgentTransportError, ValueError, TypeError, OverflowError) as error:
            self.close()
            raise AgentStateError("agent scheduling state is malformed; prior data preserved") from error
        except BaseException:
            self.close()
            raise

    def snapshot(self):
        return copy.deepcopy(self.data)

    def write(self, records, tombstones):
        if self._lock is None:
            raise AgentStateError("agent scheduling writer is closed")
        data = {**self.data, "version": self.app_version, "updated_at": time.time(),
                "sessions": records, "tombstones": tombstones}
        _validate(data, app_version=self.app_version, owner=self.owner)
        raw = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_STATE_BYTES:
            raise AgentStateError("agent scheduling state exceeds its byte limit; prior state retained")
        decoded = _decode_frame(raw)
        descriptor, filename = tempfile.mkstemp(prefix=".agent-state-", suffix=".tmp", dir=self.directory)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.data = decoded
        finally:
            temporary.unlink(missing_ok=True)

    def inventory(self):
        return storage_inventory(self.directory, self.max_storage_bytes)

    def require_capacity(self):
        inventory = self.inventory()
        if not inventory["admission_available"]:
            raise AgentStateError("agent storage reached its admission high-water limit")
        return inventory

    def close(self):
        if self._lock is not None:
            self._lock.close()
            self._lock = None


def storage_inventory(directory: Path, max_storage_bytes=None):
    """Bounded, non-following local inventory; no automatic file deletion."""
    count = total = 0
    pending = [(directory, 0)]
    while pending:
        directory, depth = pending.pop()
        if depth > 4:
            raise AgentStateError("agent storage has an unexpected directory depth")
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_INVENTORY:
                    raise AgentStateError("agent storage exceeds its inventory bound")
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                    raise AgentStateError("agent storage cannot contain redirected entries")
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    total += metadata.st_size
                else:
                    raise AgentStateError("agent storage contains an unsupported entry")
    return {"bytes": total, "entries": count, "limit_bytes": max_storage_bytes,
            "admission_available": total < max_storage_bytes if max_storage_bytes is not None else None}
