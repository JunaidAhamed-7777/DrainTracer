"""Bounded local-agent sessions composed through the supported Python API."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from .agent_jobs import AgentProcessManager
from .agent_state import AgentStateError
from .agent_transport import (
    AgentTransportError,
    current_user_sid,
    process_user_sid,
    verify_private_data_path,
    verify_trusted_path,
)
from .api import CommandTimeoutError, Session, SessionOptions, _version
from .checkpoints import read_checkpoint, recovered_report
from .html_report import write_html_report
from .report import report_directory, write_json_report


class AgentError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise AgentError("invalid_input", f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _number(value, name, minimum, maximum):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise AgentError("invalid_input", f"{name} must be finite and from {minimum} through {maximum}")
    return float(value)


def _keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise AgentError("invalid_input", "request contains missing or unsupported fields")


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as error:
        raise AgentError("invalid_input", "identities must be canonical UUID strings") from error
    return value


def _local_path(value):
    if not isinstance(value, str) or not value or len(value) > 32767 or "\0" in value:
        raise AgentError("invalid_input", "target path must be a local file path")
    try:
        path = verify_trusted_path(value)
    except AgentTransportError as error:
        raise AgentError("forbidden", str(error)) from error
    for part in (path, *path.parents):
        if part.exists() and getattr(part.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
            raise AgentError("forbidden", "target paths cannot traverse reparse points")
    return path.resolve()


@dataclass(frozen=True)
class AgentPolicy:
    allow_launch: bool = False
    allow_pause: bool = False
    allow_kill: bool = False
    launch_roots: tuple[Path, ...] = ()
    max_sessions: int = 4
    max_records: int = 64
    max_timeout: float = 3600.0

    def __post_init__(self):
        for name in ("allow_launch", "allow_pause", "allow_kill"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a bool")
        _integer(self.max_sessions, "max_sessions", 1, 8)
        _integer(self.max_records, "max_records", self.max_sessions, 256)
        _number(self.max_timeout, "max_timeout", 1, 86400)
        roots = tuple(_local_path(str(path)) for path in self.launch_roots)
        if any(not path.is_dir() for path in roots):
            raise ValueError("launch roots must be existing local directories")
        if self.allow_launch and not roots:
            raise ValueError("launch authorization requires at least one explicit launch root")
        object.__setattr__(self, "launch_roots", roots)


@dataclass
class _Record:
    identity: str
    mode: str
    session: Any
    state: str = "accepted"
    result: dict | None = None
    exports: tuple[str, ...] = ()
    events: deque = field(default_factory=lambda: deque(maxlen=200))
    event_count: int = 0
    last_samples: int = -1
    last_api_state: str = ""
    last_measurement: str = "inconclusive"
    last_full_coverage: bool = False
    request_id: str = ""
    request_digest: str = ""
    requested_at: float = 0.0
    updated_at: float = 0.0
    target_pid: int | None = None
    create_time: float | None = None
    deadline: float = 0.0
    checkpoint: str = ""
    writer_identity: dict = field(default_factory=dict)
    identity_status: str = "not_checked"
    last_progress_clock: float = field(default_factory=time.monotonic)

    def event(self, kind, **values):
        self.event_count += 1
        self.events.append({"sequence": self.event_count, "kind": kind, **values})


class AgentRegistry:
    """One IPC owner, bounded sessions, and no dashboard or user callbacks.

    The API owns each backend's controller and sampler. The registry observes
    detached immutable status values; requests never touch sampling internals.
    """

    def __init__(self, reports: Path, policy: AgentPolicy, *, session_type=Session, journal=None, checkpoints=None):
        self.reports = reports
        self.policy = policy
        self.session_type = session_type
        self.records: OrderedDict[str, _Record] = OrderedDict()
        self.replies: OrderedDict[str, tuple[str, dict]] = OrderedDict()
        self.started = time.monotonic()
        self.stopping = False
        self.journal, self.checkpoints = journal, checkpoints
        self.storage_error = None
        self.storage_inventory = None
        self._persist_next = 0.0
        self._dirty = False
        self.tombstones: OrderedDict[str, dict] = OrderedDict()
        self._writer_identity = {"pid": os.getpid(), "create_time": psutil.Process().create_time()}
        if journal is not None:
            self._restore()

    @staticmethod
    def _interrupted_result():
        return {"reason": "interrupted", "exit_code": None, "startup": "unknown", "cleanup": "unknown",
                "measurement": "inconclusive", "full_coverage": False, "target_exit_code": None,
                "errors": [], "error_count": 0}

    def _check_target_identity(self, record):
        if record.target_pid is None or record.create_time is None:
            return "unknown"
        try:
            target = psutil.Process(record.target_pid)
            if target.create_time() != record.create_time:
                return "reused"
            if not target.is_running() or target.status() == psutil.STATUS_ZOMBIE:
                return "exited"
            if process_user_sid(record.target_pid) != current_user_sid():
                return "unavailable"
            return "identity_matches"
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return "exited"
        except (psutil.AccessDenied, OSError):
            return "unavailable"

    def _checkpoint_report(self, record):
        if self.checkpoints is None:
            raise AgentError("evidence_unavailable", "no recovery checkpoint directory is configured")
        source = self.checkpoints / record.checkpoint
        verify_private_data_path(source)
        raw = read_checkpoint(source, app_version=_version(), session_id=record.identity)
        metadata = raw["checkpoint"]
        if metadata["writer_identity"] != record.writer_identity:
            raise AgentError("evidence_unavailable", "checkpoint writer identity does not match the session")
        target = metadata["target_identity"]
        if record.target_pid is not None and (target["pid"] != record.target_pid or target["create_time"] != record.create_time):
            raise AgentError("evidence_unavailable", "checkpoint target identity does not match the session")
        return source, raw

    def _restore(self):
        saved = self.journal.snapshot()
        now = time.time()
        for item in saved["tombstones"]:
            if item["expires_at"] > now:
                self.tombstones[item["request_id"]] = item
        for item in saved["sessions"]:
            if (item["state"] in {"completed", "failed", "interrupted"}
                    and item["updated_at"] < now - self.journal.retention_seconds):
                continue
            record = _Record(item["session_id"], item["mode"], None, state=item["state"], result=item["result"],
                             exports=tuple(str(self.reports / path) for path in item["reports"]),
                             last_samples=item["sample_count"], last_measurement=item["measurement"],
                             last_full_coverage=item["full_coverage"], request_id=item["request_id"],
                             request_digest=item["request_digest"], requested_at=item["requested_at"],
                             updated_at=item["updated_at"], target_pid=item["pid"], create_time=item["create_time"],
                             deadline=item["deadline"], checkpoint=item["checkpoint"],
                             writer_identity=item["writer_identity"], identity_status=item["identity_status"])
            if record.state in {"accepted", "starting", "running", "stopping", "interrupted"}:
                already_interrupted = record.state == "interrupted"
                record.state, record.result = "interrupted", self._interrupted_result()
                # A first atomic checkpoint can precede the next journal flush.
                # Only its assigned path and exact writer identity may fill in
                # missing launch identity; it never restores process authority.
                if record.target_pid is None:
                    try:
                        _source, checkpoint = self._checkpoint_report(record)
                        identity = checkpoint["checkpoint"]["target_identity"]
                        record.target_pid, record.create_time = identity["pid"], identity["create_time"]
                    except (OSError, ValueError):
                        pass  # An absent/corrupt prefix is disclosed by export.
                record.identity_status = self._check_target_identity(record)
                if not already_interrupted:
                    record.updated_at = now
                record.event("interrupted", identity_status=record.identity_status)
            self.records[record.identity] = record
        while len(self.records) > self.policy.max_records:
            self.records.popitem(last=False)
        if not self._persist(force=True):
            raise AgentError("storage", "agent could not save interruption state; prior journal preserved")

    def _saved_record(self, record):
        return {"session_id": record.identity, "mode": record.mode, "state": record.state,
                "request_id": record.request_id, "request_digest": record.request_digest,
                "requested_at": record.requested_at, "updated_at": record.updated_at,
                "pid": record.target_pid, "create_time": record.create_time, "deadline": record.deadline,
                "checkpoint": record.checkpoint, "writer_identity": dict(record.writer_identity),
                "sample_count": max(0, record.last_samples), "measurement": record.last_measurement,
                "full_coverage": record.last_full_coverage, "result": record.result,
                "reports": self._report_names(record.exports), "identity_status": record.identity_status}

    def _persist(self, *, force=False):
        if self.journal is None:
            return True
        if not force and (not self._dirty or time.monotonic() < self._persist_next):
            return True
        now = time.time()
        self.tombstones = OrderedDict((key, value) for key, value in self.tombstones.items() if value["expires_at"] > now)
        try:
            self.journal.write([self._saved_record(record) for record in self.records.values()], list(self.tombstones.values()))
        except (OSError, ValueError, TypeError) as error:
            self.storage_error = str(error)[:512]
            self._persist_next = time.monotonic() + 1
            return False
        self.storage_error = None
        self._persist_next, self._dirty = time.monotonic() + 1, False
        return True

    def _require_storage(self):
        if self.journal is not None:
            try:
                self.storage_inventory = self.journal.inventory()
                if not self.storage_inventory["admission_available"]:
                    raise AgentStateError("agent storage reached its admission high-water limit")
            except (OSError, AgentStateError) as error:
                self.storage_error = str(error)[:512]
                raise AgentError("storage", "agent storage cannot admit new writes; existing evidence preserved") from error

    def _export_paths(self, value):
        if value is None:
            return ()
        paths = tuple(Path(path).resolve() for path in (value.json_path, value.html_path) if path is not None)
        if any(not path.is_relative_to(self.reports.resolve()) for path in paths):
            raise AgentError("storage", "export returned evidence outside the agent report directory")
        return tuple(map(str, paths))

    def _report_names(self, paths):
        return [Path(path).relative_to(self.reports.resolve()).as_posix() for path in paths]

    def refresh(self):
        for record in self.records.values():
            if record.result is not None:
                continue
            status = record.session.status
            if status.state != record.last_api_state or status.sample_count != record.last_samples:
                record.event("progress", state=status.state, sample_count=status.sample_count,
                             measurement=status.measurement, full_coverage=status.full_coverage)
                record.last_samples, record.last_api_state = status.sample_count, status.state
                record.last_measurement, record.last_full_coverage = status.measurement, status.full_coverage
                if status.snapshot is not None:
                    record.target_pid = status.snapshot.root_pid
                    record.create_time = status.snapshot.root_create_time
                record.updated_at, self._dirty = time.time(), True
                record.last_progress_clock = time.monotonic()
            result = record.session.wait(0)
            if result is not None:
                record.state = "failed" if result.reason == "failed" else "completed"
                record.exports = self._export_paths(result.exports)
                record.result = {"reason": result.reason, "exit_code": result.exit_code,
                                 "startup": result.startup, "cleanup": result.cleanup,
                                 "measurement": result.measurement, "full_coverage": result.full_coverage,
                                 "target_exit_code": result.target_exit_code,
                                 "errors": [str(error)[:512] for error in result.errors[:8]],
                                 "error_count": len(result.errors)}
                record.event("finished", **record.result)
                # The bounded registry keeps tiny final metadata, not the API
                # session's complete report and retained immutable snapshot.
                record.session = None
                record.updated_at, self._dirty = time.time(), True
            elif status.state in {"starting", "running", "stopping"}:
                record.state = status.state
        self._persist()

    def _status(self, record):
        status = record.session.status if record.session is not None else None
        return {"session_id": record.identity, "mode": record.mode, "state": record.state,
                "sample_count": status.sample_count if status else max(0, record.last_samples),
                "measurement": status.measurement if status else record.last_measurement,
                "full_coverage": status.full_coverage if status else record.last_full_coverage, "result": record.result,
                "identity_status": record.identity_status, "last_observed_at": record.updated_at,
                "target_identity": {"pid": record.target_pid, "create_time": record.create_time},
                "observation_age_seconds": (max(0, time.monotonic() - record.last_progress_clock)
                                            if status else max(0, time.time() - record.updated_at)),
                "reports": self._report_names(record.exports)}

    def handle(self, request):
        identity = request.get("id") if isinstance(request, dict) else None
        digest = None
        try:
            _keys(request, {"protocol", "id", "operation", "params"}, {"protocol", "id", "operation", "params"})
            if type(request["protocol"]) is not int or request["protocol"] != 1:
                raise AgentError("protocol", "unsupported local agent protocol")
            _uuid(identity)
            operation, params = request["operation"], request["params"]
            digest = hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if identity in self.replies:
                previous, response = self.replies[identity]
                if previous != digest:
                    raise AgentError("conflict", "request identity was already used for different parameters")
                return response
            if not isinstance(operation, str) or not isinstance(params, dict):
                raise AgentError("invalid_input", "operation and parameters have invalid types")
            if operation == "start":
                prior = self.tombstones.get(identity)
                retained = next((value for value in self.records.values() if value.request_id == identity), None)
                if prior is not None or retained is not None:
                    previous = prior["digest"] if prior is not None else retained.request_digest
                    session_id = prior["session_id"] if prior is not None else retained.identity
                    if previous != digest:
                        raise AgentError("conflict", "start identity was already used for different parameters")
                    return self._remember(identity, digest, {"protocol": 1, "id": identity, "ok": True,
                                                           "result": {"accepted": True, "session_id": session_id}})
            self.refresh()
            result = self._dispatch(operation, params, identity=identity, digest=digest)
            response = {"protocol": 1, "id": identity, "ok": True, "result": result}
            return self._remember(identity, digest, response)
        except AgentError as error:
            code, message, may_have_run = error.code, str(error), False
        except CommandTimeoutError as error:
            code, message, may_have_run = "deadline", str(error), error.may_have_run
        except PermissionError:
            code, message, may_have_run = "forbidden", "the requested operation was denied", False
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            code, message, may_have_run = "target_unavailable", "the requested target is unavailable", False
        except (OSError, RuntimeError, ValueError, TypeError):
            code, message, may_have_run = "operation_failed", "the operation failed; inspect session status and evidence", False
        response = {"protocol": 1, "id": identity, "ok": False,
                    "error": {"code": code, "message": message, "may_have_run": may_have_run}}
        # An uncertain control/export is still a completed request identity.
        # Do not overwrite the original cached reply on conflicting parameters.
        if digest is not None and (identity not in self.replies or self.replies[identity][0] == digest):
            return self._remember(identity, digest, response)
        return response

    def _remember(self, identity, digest, response):
        self.replies[identity] = digest, response
        while len(self.replies) > 256:
            self.replies.popitem(last=False)
        return response

    def _dispatch(self, operation, params, *, identity, digest):
        if operation == "start":
            return self._start(params, request_id=identity, digest=digest)
        if operation == "shutdown":
            _keys(params, set())
            self.stopping = True
            return {"accepted": True, "cleanup_pending": True}
        if operation == "diagnostics":
            _keys(params, set())
            active = [record for record in self.records.values() if record.result is None]
            stale = []
            for record in active:
                interval = record.session.options.sample_interval_ms / 1000
                if time.monotonic() - record.last_progress_clock > max(10, interval * 3):
                    stale.append(record.identity)
            try:
                self._require_storage()
            except AgentError:
                pass  # The diagnostics expose the failed check below.
            return {"diagnostics_schema": 1, "version": _version(), "responsive": True,
                    "ready": not self.stopping and self.storage_error is None and not stale
                             and len(active) < self.policy.max_sessions,
                    "fresh": not stale, "stale_sessions": stale, "queue_depth": 0,
                    "active_sessions": len(active), "session_limit": self.policy.max_sessions,
                    "retained_records": len(self.records), "storage": self.storage_inventory,
                    "storage_error": self.storage_error, "uptime_seconds": time.monotonic() - self.started}
        if operation == "status":
            _keys(params, {"session_id", "offset", "limit"})
            if "session_id" in params:
                _keys(params, {"session_id"})
                return self._status(self._record(params))
            offset = _integer(params.get("offset", 0), "offset", 0, self.policy.max_records)
            limit = _integer(params.get("limit", 32), "limit", 1, 32)
            records = list(self.records.values())[offset:offset + limit]
            return {"ready": (not self.stopping and self.storage_error is None
                              and (self.storage_inventory is None or self.storage_inventory["admission_available"])),
                    "storage_error": self.storage_error, "storage": self.storage_inventory, "uptime_seconds": round(time.monotonic() - self.started, 3),
                    "active_sessions": sum(record.result is None for record in self.records.values()),
                    "session_limit": self.policy.max_sessions, "queue_depth": 0,
                    "retained_records": len(self.records),
                    "next_offset": offset + len(records) if offset + len(records) < len(self.records) else None,
                    "sessions": [self._status(record) for record in records]}
        if operation not in {"events", "cancel", "export", "pause", "resume", "kill"}:
            raise AgentError("unknown_operation", "unsupported local agent operation")
        _keys(params, {"session_id", "cursor", "limit"} if operation == "events" else {"session_id"}, {"session_id"})
        record = self._record(params)
        if operation == "events":
            cursor = _integer(params.get("cursor", 0), "cursor", 0, 2**63 - 1)
            limit = _integer(params.get("limit", 100), "limit", 1, 100)
            events = [event for event in record.events if event["sequence"] > cursor][:limit]
            oldest = record.events[0]["sequence"] if record.events else record.event_count + 1
            return {"events": events, "next_cursor": events[-1]["sequence"] if events else cursor,
                    "observed": record.event_count, "retained": len(record.events),
                    "dropped": record.event_count - len(record.events), "cursor_gap": max(0, oldest - cursor - 1)}
        if operation == "cancel":
            if record.session is None:
                return {"accepted": False, "finished": True, "session_id": record.identity}
            record.session.cancel()
            return {"accepted": True, "session_id": record.identity}
        if operation == "export":
            if record.state == "interrupted":
                self._require_storage()
                try:
                    source, checkpoint = self._checkpoint_report(record)
                    report = recovered_report(source, app_version=_version(), session_id=checkpoint["session_id"])
                except (OSError, ValueError) as error:
                    if isinstance(error, AgentError):
                        raise
                    raise AgentError("evidence_unavailable", "no intact matching checkpoint is available; interrupted state preserved") from error
                try:
                    directory = report_directory(report, self.reports)
                    json_path = write_json_report(report, directory)
                    record.exports = (str(json_path),)
                    html_path = write_html_report(report, directory)
                    record.exports = tuple(map(str, (json_path, html_path)))
                except (OSError, ValueError, TypeError) as error:
                    self._dirty = True
                    self._persist(force=True)
                    raise AgentError("storage", "recovered report export failed; checkpoint and any saved JSON were preserved") from error
                self._dirty = True
                if not self._persist(force=True):
                    raise AgentError("storage", "recovered evidence saved but journal update failed")
            elif record.result is None:
                self._require_storage()
                exported = record.session.export(timeout=5)
                record.exports = self._export_paths(exported)
                record.updated_at, self._dirty = time.time(), True
                if not self._persist(force=True):
                    raise AgentError("storage", "evidence saved but the journal update failed")
                if exported.errors:
                    raise AgentError("storage", "report export failed; inspect session status and storage")
            return {"directory": str(self.reports), "reports": self._report_names(record.exports)}
        if operation in {"pause", "resume", "kill"}:
            if record.session is None:
                raise AgentError("session_finished", "the session has already finished")
            if operation in {"pause", "resume"} and (not self.policy.allow_pause or record.mode == "attach"):
                raise AgentError("forbidden", "pause/resume is authorized only for launched sessions")
            if operation == "kill" and not self.policy.allow_kill:
                raise AgentError("forbidden", "explicit kill capability is disabled")
            return {"completed": bool(getattr(record.session, operation)(timeout=5))}
        raise AgentError("unknown_operation", "unsupported local agent operation")

    def _record(self, params):
        identity = _uuid(params.get("session_id"))
        try:
            return self.records[identity]
        except KeyError as error:
            raise AgentError("unknown_session", "session identity is not retained by this agent") from error

    def _start(self, params, *, request_id, digest):
        if self.stopping:
            raise AgentError("unavailable", "the agent is stopping")
        _keys(params, {"mode", "pid", "create_time", "target", "args", "timeout_seconds", "sample_interval_ms",
                       "windows_seconds", "warmup_seconds"}, {"mode"})
        if sum(record.result is None for record in self.records.values()) >= self.policy.max_sessions:
            raise AgentError("capacity", "the agent's active-session limit has been reached")
        retired = None
        if len(self.records) >= self.policy.max_records:
            retired = next((key for key, value in self.records.items() if value.result is not None), None)
            if retired is None:
                raise AgentError("capacity", "the retained-session limit has been reached")
        timeout = _number(params.get("timeout_seconds", min(60, self.policy.max_timeout)),
                          "timeout_seconds", 1, self.policy.max_timeout)
        interval = _integer(params.get("sample_interval_ms", 500), "sample_interval_ms", 100, 60000)
        windows = params.get("windows_seconds", [10, 30, 60])
        if not isinstance(windows, list) or not 1 <= len(windows) <= 8:
            raise AgentError("invalid_input", "windows_seconds must have 1-8 durations")
        windows = tuple(_number(value, "window", 1, 3600) for value in windows)
        warmup = _number(params.get("warmup_seconds", 0), "warmup_seconds", 0, 3600)
        mode = params["mode"]
        identity = str(uuid.uuid4())
        self._require_storage()
        options = SessionOptions(sample_interval_ms=interval, windows_seconds=windows,
                                 warmup_seconds=warmup, timeout_seconds=timeout, report_dir=self.reports,
                                 allow_pause=self.policy.allow_pause and mode == "launch",
                                 allow_kill=self.policy.allow_kill,
                                 checkpoint=str(self.checkpoints / (identity + ".json")) if self.checkpoints else None,
                                 checkpoint_interval_seconds=1.0)
        if mode == "attach":
            if "target" in params or "args" in params:
                raise AgentError("invalid_input", "attach does not accept launch fields")
            pid = _integer(params.get("pid"), "pid", 1, 2**32 - 1)
            created = _number(params.get("create_time"), "create_time", 0.000001, 2**53)
            if pid == os.getpid() or process_user_sid(pid) != current_user_sid():
                raise AgentError("forbidden", "attachment requires another process owned by the current user")
            session = self.session_type.attach(pid, create_time=created, options=options, _checkpoint_session_id=identity)
        elif mode == "launch":
            if "pid" in params or "create_time" in params:
                raise AgentError("invalid_input", "launch does not accept attachment fields")
            if not self.policy.allow_launch:
                raise AgentError("forbidden", "launch capability is disabled")
            path = _local_path(params.get("target"))
            if not path.is_file() or path.suffix.lower() not in {".exe", ".py"}:
                raise AgentError("invalid_input", "agent launch requires an existing .exe or .py file")
            if not any(path.is_relative_to(root) for root in self.policy.launch_roots):
                raise AgentError("forbidden", "target is outside the authorized launch roots")
            args = params.get("args", [])
            if (not isinstance(args, list) or len(args) > 64
                    or any(not isinstance(value, str) or len(value) > 4096 or "\0" in value for value in args)):
                raise AgentError("invalid_input", "target arguments must be at most 64 bounded literal strings")
            session = self.session_type.launch(path, args=args, options=options, _manager_factory=AgentProcessManager,
                                               _checkpoint_session_id=identity)
        else:
            raise AgentError("invalid_input", "mode must be attach or launch")
        retired_record = self.records.pop(retired) if retired is not None else None
        now = time.time()
        record = _Record(identity, mode, session, request_id=request_id, request_digest=digest,
                         requested_at=now, updated_at=now, target_pid=params.get("pid"),
                         create_time=params.get("create_time"), deadline=now + timeout,
                         checkpoint=identity + ".json", writer_identity=dict(self._writer_identity))
        self.records[identity] = record
        record.event("accepted", mode=mode)
        retention = self.journal.retention_seconds if self.journal is not None else 7 * 86400
        previous_tombstones = self.tombstones.copy()
        self.tombstones[request_id] = {"request_id": request_id, "digest": digest,
                                       "session_id": identity, "expires_at": now + retention}
        while len(self.tombstones) > 256:
            self.tombstones.popitem(last=False)
        self._dirty = True
        if not self._persist(force=True):
            del self.records[identity]
            self.tombstones = previous_tombstones
            if retired_record is not None:
                self.records[retired] = retired_record
                self.records.move_to_end(retired, last=False)
            raise AgentError("storage", "start was refused before launch because admission state could not be saved")
        try:
            session.start()
        except (OSError, RuntimeError, ValueError) as error:
            record.state, record.result = "failed", self._interrupted_result()
            record.result.update(reason="failed", exit_code=2, startup="failed", cleanup="not_attempted",
                                 errors=[str(error)[:512]], error_count=1)
            record.session = None
            self._dirty = True
            self._persist(force=True)
        return {"accepted": True, "session_id": identity}

    def close(self, timeout=10):
        self.stopping = True
        for record in self.records.values():
            if record.result is None:
                record.session.cancel()
        deadline = time.monotonic() + timeout
        for record in self.records.values():
            if record.result is None:
                record.session.close(timeout=max(0, deadline - time.monotonic()))
        self.refresh()
        self._persist(force=True)
        return all(record.result is not None and record.result["cleanup"] in {"complete", "not_attempted"}
                   for record in self.records.values())
