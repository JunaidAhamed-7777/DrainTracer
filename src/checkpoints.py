"""Bounded, opt-in recovery evidence; checkpoints never authorize process control."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, TypeVar

import psutil

from .report import iso_timestamp, new_report_id
from .report_contract import (
    MAX_REPORT_BYTES,
    ReportContractError,
    read_report,
    validate_report,
)

CHECKPOINT_SCHEMA = 1
CHECKPOINT_SAMPLES = 128
_Store = TypeVar("_Store", bound="CheckpointStore")


class CheckpointError(ValueError):
    """A checkpoint cannot safely be written or recovered."""


def _version(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise CheckpointError("checkpoint versions must use vMAJOR.MINOR.PATCH")
    try:
        return tuple(int(part) for part in value[1:].split("."))
    except ValueError as exc:
        raise CheckpointError("checkpoint version components are too large") from exc


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise CheckpointError("checkpoint session identity must be a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise CheckpointError("checkpoint session identity must be a UUID") from exc


def _identity(value: Any, *, unknown: bool = False) -> dict[str, Any]:
    if (not isinstance(value, dict) or type(value.get("pid")) is not int or value["pid"] <= 0
            or "create_time" not in value):
        raise CheckpointError("checkpoint process identity requires a positive PID")
    created = value.get("create_time")
    if created is None and unknown:
        return value
    if type(created) not in (int, float) or not math.isfinite(created) or created <= 0:
        raise CheckpointError("checkpoint process identity requires a finite creation time")
    return value


def _encoded(report: dict[str, Any]) -> bytes:
    return json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(report: dict[str, Any]) -> str:
    checkpoint = dict(report["checkpoint"])
    checkpoint.pop("digest", None)
    return hashlib.sha256(_encoded({**report, "checkpoint": checkpoint})).hexdigest()


def read_checkpoint(path: str | Path, *, app_version: str, session_id: str | None = None) -> dict[str, Any]:
    """Validate bounded evidence and its internal identities, without reopening any PID."""
    try:
        report = read_report(path)
    except ReportContractError as exc:
        raise CheckpointError(str(exc)) from exc
    return _validate_checkpoint(report, app_version=app_version, session_id=session_id)


def _validate_checkpoint(report: dict[str, Any], *, app_version: str, session_id: str | None = None) -> dict[str, Any]:
    checkpoint = report.get("checkpoint")
    if not isinstance(checkpoint, dict) or type(checkpoint.get("schema")) is not int or checkpoint["schema"] != CHECKPOINT_SCHEMA:
        raise CheckpointError("unsupported or missing checkpoint schema")
    writer_version, current = _version(checkpoint.get("app_version")), _version(app_version)
    if writer_version[0] != current[0] or writer_version > current:
        raise CheckpointError("checkpoint needs a compatible DrainTracer version at least as new as its writer")
    if report.get("version") != checkpoint["app_version"]:
        raise CheckpointError("checkpoint and report versions disagree")
    identity = _uuid(checkpoint.get("session_id"))
    if report.get("session_id") != identity or (session_id is not None and _uuid(session_id) != identity):
        raise CheckpointError("checkpoint session identity does not match")
    _identity(checkpoint.get("writer_identity"))
    target = _identity(checkpoint.get("target_identity"), unknown=True)
    if type(report.get("root_pid")) is not int or report["root_pid"] != target["pid"]:
        raise CheckpointError("checkpoint target identity does not match the report")
    _uuid(report.get("report_id"))
    try:
        captured = datetime.fromisoformat(checkpoint.get("captured_at", ""))
    except (TypeError, ValueError) as exc:
        raise CheckpointError("checkpoint cutoff is not an ISO timestamp") from exc
    if captured.tzinfo is None:
        raise CheckpointError("checkpoint cutoff requires a UTC offset")
    if type(checkpoint.get("sample_limit")) is not int or checkpoint["sample_limit"] != CHECKPOINT_SAMPLES:
        raise CheckpointError("unsupported checkpoint sample bound")
    if len(report["snapshots"]) > CHECKPOINT_SAMPLES:
        raise CheckpointError("checkpoint contains too many retained chart samples")
    facts = report.get("session_facts")
    if (not isinstance(facts, dict) or type(facts.get("schema")) is not int or facts["schema"] != 1
            or not isinstance(facts.get("samples"), dict)
            or type(facts["samples"].get("taken")) is not int or facts["samples"]["taken"] < 1):
        raise CheckpointError("checkpoint requires exact session facts through its cutoff")
    stamp = facts.get("last_sample_at")
    if (type(stamp) not in (int, float) or not math.isfinite(stamp)
            or abs(captured.timestamp() - stamp) > 0.0011):
        raise CheckpointError("checkpoint cutoff disagrees with the last recorded observation")
    for sample in report["snapshots"]:
        for process in sample.get("processes", ()):
            created = process.get("create_time")
            if (process.get("pid") == target["pid"] and created is not None
                    and target["create_time"] is not None and created != target["create_time"]):
                raise CheckpointError("checkpoint target creation time disagrees with sampled identity")
    digest = checkpoint.get("digest")
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest) or not hmac.compare_digest(digest, _digest(report)):
        raise CheckpointError("checkpoint checksum does not match; use an intact checkpoint")
    return report


def _lock(path: Path) -> BinaryIO:
    if path.is_symlink():
        raise CheckpointError("checkpoint lock cannot be a symbolic link")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1:
            raise CheckpointError("checkpoint lock is not an owned lock file")
        if not info.st_size:
            handle.write(b"\0")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BaseException:
        handle.close()
        raise


class CheckpointStore:
    """One writer, one bounded atomic file; OS locks release after a crash."""

    def __init__(self, path: str | Path, app_version: str) -> None:
        _version(app_version)
        self.path = Path(path)
        self.app_version = app_version
        self.session_id = str(uuid.uuid4())
        self.writer_identity = {"pid": os.getpid(), "create_time": psutil.Process().create_time()}
        self._handle: BinaryIO | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = _lock(self.path.with_name(self.path.name + ".lock"))
            if self.path.exists() or self.path.is_symlink():
                raise CheckpointError("checkpoint already exists; recover it or choose a new path")
        except BaseException:
            self.close()
            raise

    def write(self, report: dict[str, Any], target_identity: dict[str, Any]) -> None:
        if self._handle is None:
            raise CheckpointError("checkpoint writer is closed")
        _identity(target_identity, unknown=True)
        if len(report["snapshots"]) > CHECKPOINT_SAMPLES:
            raise CheckpointError("checkpoint contains too many retained chart samples")
        # Runtime report models contain tuples; validate the same JSON-shaped
        # document that readers receive, within the file's byte bound.
        encoded = _encoded(report)
        if len(encoded) > MAX_REPORT_BYTES:
            raise CheckpointError("checkpoint exceeds the 64 MiB limit; previous checkpoint retained")
        report = validate_report(json.loads(encoded))
        if report.get("version") != self.app_version or report.get("root_pid") != target_identity["pid"]:
            raise CheckpointError("checkpoint writer and report identity disagree")
        if self.path.exists() or self.path.is_symlink():
            read_checkpoint(self.path, app_version=self.app_version, session_id=self.session_id)
        try:
            captured_at = iso_timestamp(report["session_facts"]["last_sample_at"])
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
            raise CheckpointError("checkpoint needs a representable last-observation timestamp") from exc
        checkpoint = {
            "schema": CHECKPOINT_SCHEMA, "app_version": self.app_version, "session_id": self.session_id,
            "captured_at": captured_at, "saved_at": iso_timestamp(), "writer_identity": self.writer_identity,
            "target_identity": dict(target_identity), "sample_limit": CHECKPOINT_SAMPLES,
        }
        saved = {**report, "session_id": self.session_id, "checkpoint": checkpoint,
                 "recovery": {"partial": True, "exact_through_cutoff": True, "final_outcome_confirmed": False}}
        checkpoint["digest"] = _digest(saved)
        _validate_checkpoint(saved, app_version=self.app_version, session_id=self.session_id)
        raw = _encoded(saved)
        if len(raw) > MAX_REPORT_BYTES:
            raise CheckpointError("checkpoint exceeds the 64 MiB limit; previous checkpoint retained")
        descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()  # Closing releases the OS-owned lock, including after failed startup.
            self._handle = None

    def __enter__(self: _Store) -> _Store:  # noqa: PYI019 - Self needs Python 3.11 or another dependency
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()


def recovered_report(path: str | Path, *, app_version: str, session_id: str | None = None) -> dict[str, Any]:
    """Recover an intact prefix as a fresh, explicitly partial report; never control a target."""
    report = read_checkpoint(path, app_version=app_version, session_id=session_id)
    checkpoint = report.pop("checkpoint")
    report["recovery"] = {
        "partial": True, "exact_through_cutoff": True, "final_outcome_confirmed": False,
        "captured_at": checkpoint["captured_at"], "recovered_at": iso_timestamp(),
        "tool_version": app_version,
        "session_id": checkpoint["session_id"], "source_report_id": report.get("report_id"),
    }
    report["report_id"] = new_report_id()
    report["created_at"] = iso_timestamp()
    report["session_ended_at"] = None
    headless = report.get("headless")
    if isinstance(headless, dict):
        headless["decision_as_of_checkpoint"] = headless.get("exit_code")
        headless["exit_code"] = None
    return report
