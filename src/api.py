"""Supported, presentation-free Python monitoring boundary.

See ``docs/python-api.md`` for ownership, callback and compatibility guarantees.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import math
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Full, Queue
from types import MappingProxyType
from typing import Any

from rich.console import Console

from .budgets import check_budgets
from .config import _parse_layer, resolve_settings
from .events import severity_rank
from .report import snapshot_to_dict
from .session import MonitorSession, session_exit_code


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _version() -> str:
    """Read the canonical runtime version without importing the dashboard."""
    tree = ast.parse(Path(__file__).with_name("dashboard.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DashboardApp":
            for statement in node.body:
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "VERSION" for target in statement.targets
                ):
                    value = ast.literal_eval(statement.value)
                    if isinstance(value, str):
                        return value
    raise RuntimeError("canonical runtime version is missing")


@dataclass(frozen=True)
class SessionOptions:
    """Explicit monitor settings; durations are seconds unless named otherwise."""

    sample_interval_ms: int = 500
    windows_seconds: tuple[float, ...] = (10.0, 30.0, 60.0)
    warmup_seconds: float = 0.0
    timeout_seconds: float | None = None
    report_dir: str | Path = "leak_reports/reports"
    export_on_close: bool = True
    allow_pause: bool = False
    allow_kill: bool = False
    fail_on: str = "high"
    fail_on_inconclusive: bool = False
    fail_on_target_error: bool = False
    require_full_coverage: bool = False
    budgets: Mapping[str, float] = field(default_factory=dict)
    thresholds: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    checkpoint: str | None = None
    checkpoint_interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in ("export_on_close", "allow_pause", "allow_kill", "fail_on_inconclusive",
                     "fail_on_target_error", "require_full_coverage"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a bool")
        if type(self.sample_interval_ms) is not int:
            raise ValueError("sample_interval_ms must be an integer")
        object.__setattr__(self, "windows_seconds", tuple(self.windows_seconds))
        object.__setattr__(self, "budgets", _freeze(dict(self.budgets)))
        object.__setattr__(self, "thresholds", _freeze({key: dict(value) for key, value in self.thresholds.items()}))
        settings = self._settings()
        # Capture relative destinations before another thread can change cwd.
        object.__setattr__(self, "report_dir", Path(settings.report_dir).resolve())
        if settings.checkpoint is not None:
            object.__setattr__(self, "checkpoint", str(Path(settings.checkpoint).resolve()))

    def _settings(self):
        table = {
            "sample_interval": self.sample_interval_ms / 1000,
            "windows": list(self.windows_seconds), "warmup": self.warmup_seconds,
            "report_dir": str(self.report_dir), "fail_on": self.fail_on,
            "fail_on_inconclusive": self.fail_on_inconclusive,
            "fail_on_target_error": self.fail_on_target_error,
            "require_full_coverage": self.require_full_coverage,
            "thresholds": {key: dict(value) for key, value in self.thresholds.items()},
            "checkpoint_interval": self.checkpoint_interval_seconds,
        }
        if self.timeout_seconds is not None:
            table["timeout"] = self.timeout_seconds
        if self.checkpoint is not None:
            table["checkpoint"] = self.checkpoint
        names = {"private_bytes": "budget_private", "handles": "budget_handles", "threads": "budget_threads"}
        for key, value in self.budgets.items():
            if key not in names:
                raise ValueError(f"unknown budget metric: {key}")
            table[names[key]] = value
        if "\x00" in str(self.report_dir) or (self.checkpoint is not None and "\x00" in self.checkpoint):
            raise ValueError("paths must not contain NUL")
        return resolve_settings(discover=False, overrides=_parse_layer(table, "api.", Path.cwd()))


@dataclass(frozen=True)
class Snapshot:
    """A deeply immutable detached snapshot; unknown counters remain qualified."""

    timestamp: float
    root_pid: int | None
    data: Mapping[str, Any]
    root_create_time: float | None = None


@dataclass(frozen=True)
class SessionStatus:
    state: str
    snapshot: Snapshot | None = None
    sample_count: int = 0
    measurement: str = "inconclusive"
    full_coverage: bool = False
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportResult:
    json_path: Path | None
    html_path: Path | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionResult:
    reason: str
    exit_code: int
    startup: str
    cleanup: str
    measurement: str
    full_coverage: bool
    target_exit_code: int | None
    errors: tuple[str, ...]
    exports: ExportResult | None
    summary: Mapping[str, Any]


@dataclass(frozen=True)
class SessionEvent:
    kind: str
    status: SessionStatus


class CommandTimeoutError(TimeoutError):
    """The deadline expired; ``may_have_run`` distinguishes an in-flight command."""

    def __init__(self, may_have_run: bool):
        super().__init__("session command deadline expired")
        self.may_have_run = may_have_run


_CALLBACK_QUEUE: Queue = Queue(maxsize=256)
_CALLBACK_LOCK = threading.Lock()
_CALLBACK_STARTED = False


def _callback_worker() -> None:
    while True:
        subscription = _CALLBACK_QUEUE.get()
        subscription._deliver()


def _start_callbacks() -> None:
    global _CALLBACK_STARTED
    with _CALLBACK_LOCK:
        if not _CALLBACK_STARTED:
            for index in range(4):
                threading.Thread(target=_callback_worker, name=f"DrainTracerCallback-{index}", daemon=True).start()
            _CALLBACK_STARTED = True


class Subscription:
    """Bounded ordered notifications; closing never waits on user code."""

    def __init__(self, callback: Callable[[SessionEvent], None]):
        self._callback = callback
        self._lock = threading.Lock()
        self._pending: deque[SessionEvent] = deque(maxlen=64)
        self._scheduled = False
        self._closed = False
        self._dropped = 0
        self._failures = 0

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._dropped += len(self._pending)
            self._pending.clear()
            self._callback = None

    def _offer(self, event: SessionEvent) -> None:
        with self._lock:
            if self._closed:
                return
            if len(self._pending) == self._pending.maxlen:
                self._dropped += 1
            self._pending.append(event)
            if not self._scheduled:
                try:
                    _CALLBACK_QUEUE.put_nowait(self)
                except Full:
                    self._dropped += len(self._pending)
                    self._pending.clear()
                else:
                    self._scheduled = True

    def _deliver(self) -> None:
        # Deliver one event per turn so a busy subscription cannot monopolize
        # a worker. Keep scheduled true until callback completion.
        with self._lock:
            if self._closed or not self._pending:
                self._scheduled = False
                return
            event = self._pending.popleft()
            callback = self._callback
        try:
            callback(event)
        except BaseException:  # noqa: BLE001 - user callbacks cannot kill shared workers
            with self._lock:
                self._failures += 1
        finally:
            with self._lock:
                self._scheduled = False
                if self._pending and not self._closed:
                    try:
                        _CALLBACK_QUEUE.put_nowait(self)
                    except Full:
                        self._dropped += len(self._pending)
                        self._pending.clear()
                    else:
                        self._scheduled = True


class Session:
    """A single-use monitor whose controller owns all backend mutations."""

    def __init__(self, target_path: str | None, args: Sequence[str] = (), *,
                 attach_pid: int | None = None, create_time: float | None = None,
                 options: SessionOptions | None = None, _manager_factory=None):
        self._options = options or SessionOptions()
        if not isinstance(self.options, SessionOptions):
            raise TypeError("options must be SessionOptions")
        if attach_pid is not None:
            if type(attach_pid) is not int or attach_pid <= 0 or target_path is not None or args:
                raise ValueError("attach requires a positive PID and no launch target/arguments")
            if create_time is not None and (isinstance(create_time, bool) or not isinstance(create_time, (int, float))
                                            or not math.isfinite(create_time) or create_time <= 0):
                raise ValueError("create_time must be a positive finite timestamp")
        elif not isinstance(target_path, str) or not target_path or "\x00" in target_path:
            raise ValueError("launch requires a target path")
        if isinstance(args, (str, bytes)) or len(args) > 1024 or any(
            not isinstance(item, str) or "\x00" in item for item in args
        ):
            raise ValueError("args must contain at most 1024 strings without NUL")
        self._target = str(Path(target_path).resolve()) if target_path is not None else None
        self._args = tuple(args)
        self._attach_pid = attach_pid
        self._create_time = create_time
        self._manager_factory = _manager_factory
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._commands: Queue = Queue(maxsize=32)
        self._thread: threading.Thread | None = None
        self._status = SessionStatus("created")
        self._result: SessionResult | None = None
        self._final_report: dict[str, Any] | None = None
        self._subscribers: list[Subscription] = []

    @property
    def options(self) -> SessionOptions:
        return self._options

    @classmethod
    def launch(cls, path: str | Path, args: Sequence[str] = (), *, options: SessionOptions | None = None, **private):
        return cls(str(path), args, options=options, **private)

    @classmethod
    def attach(cls, pid: int, *, create_time: float | None = None,
               options: SessionOptions | None = None, **private):
        return cls(None, attach_pid=pid, create_time=create_time, options=options, **private)

    @property
    def status(self) -> SessionStatus:
        with self._lock:
            return self._status

    @property
    def snapshot(self) -> Snapshot | None:
        return self.status.snapshot

    def subscribe(self, callback: Callable[[SessionEvent], None]) -> Subscription:
        if not callable(callback):
            raise TypeError("callback must be callable")
        _start_callbacks()
        with self._lock:
            self._subscribers = [item for item in self._subscribers if not item.closed]
            if self._done.is_set() or len(self._subscribers) >= 64:
                raise RuntimeError("session has ended or subscription limit reached")
            subscription = Subscription(callback)
            self._subscribers.append(subscription)
            return subscription

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._done.is_set():
                raise RuntimeError("session is single-use")
            self._thread = threading.Thread(target=self._run, name="DrainTracerSession", daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> SessionResult | None:
        self._check_timeout(timeout)
        if self._done.wait(timeout):
            with self._lock:
                return self._result
        return None

    def close(self, timeout: float = 10.0) -> SessionResult | None:
        self.cancel()
        with self._lock:
            if self._thread is None and not self._done.is_set():
                self.start()
        return self.wait(timeout)

    @staticmethod
    def _check_timeout(timeout: float | None) -> None:
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and non-negative")

    def _command(self, name: str, timeout: float):
        self._check_timeout(timeout)
        future: Future = Future()
        with self._lock:
            if self._thread is None or self._done.is_set():
                raise RuntimeError("session is not running")
            try:
                self._commands.put_nowait((name, future))
            except Full:
                raise RuntimeError("session command queue is full") from None
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            if future.done():
                raise  # Preserve a TimeoutError raised by the command itself.
            raise CommandTimeoutError(may_have_run=not future.cancel()) from None

    def pause(self, timeout: float = 5.0) -> bool:
        if not self.options.allow_pause:
            raise PermissionError("pause capability is disabled")
        return self._command("pause", timeout)

    def resume(self, timeout: float = 5.0) -> bool:
        if not self.options.allow_pause:
            raise PermissionError("pause capability is disabled")
        return self._command("resume", timeout)

    def kill(self, timeout: float = 5.0) -> bool:
        if not self.options.allow_kill:
            raise PermissionError("kill capability is disabled")
        return self._command("kill", timeout)

    def report(self, timeout: float = 10.0) -> dict[str, Any]:
        with self._lock:
            if self._final_report is not None:
                return copy.deepcopy(self._final_report)
        return self._command("report", timeout)

    def export(self, timeout: float = 10.0) -> ExportResult:
        return self._command("export", timeout)

    def _publish(self, backend, state: str, errors: list[str], *, kind: str = "status") -> None:
        raw = backend.snapshot if backend is not None else None
        snapshot = None if raw is None else Snapshot(raw.timestamp, raw.root_pid,
                                                     _freeze(snapshot_to_dict(raw)), backend._target_created)
        current_errors = tuple(dict.fromkeys([*errors, *(backend.monitor_errors if backend else ())]))
        status = SessionStatus(state, snapshot, backend._sample_count if backend else 0,
                               backend.measurement_outcome if backend else "inconclusive",
                               backend.full_coverage if backend else False, current_errors)
        with self._lock:
            self._status = status
            subscriptions = tuple(self._subscribers)
        event = SessionEvent(kind, status)
        for subscription in subscriptions:
            subscription._offer(event)

    @staticmethod
    def _export(backend) -> ExportResult:
        before = set(backend.monitor_errors)
        try:
            path = backend.export_log()
            return ExportResult(path, backend._last_html_report,
                                tuple(item for item in backend.monitor_errors if item not in before))
        except Exception as error:  # noqa: BLE001 - export failures belong in the typed result
            return ExportResult(None, None, (str(error) or type(error).__name__,))

    @staticmethod
    def _report_data(backend) -> dict[str, Any]:
        # The in-memory backend models contain tuples and string enums. Return
        # the same JSON-native values as an exported report to public callers.
        return json.loads(json.dumps(backend.build_report(), allow_nan=False))

    def _drain_commands(self, backend) -> None:
        for _ in range(32):
            try:
                name, future = self._commands.get_nowait()
            except Empty:
                break
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if name == "report":
                    value = self._report_data(backend)
                elif name == "export":
                    value = self._export(backend)
                elif name == "pause":
                    value = backend.manager.pause()
                    if value:
                        backend.detector.pause()
                elif name == "resume":
                    value = backend.manager.resume()
                    if value:
                        backend.detector.resume()
                elif name == "kill":
                    backend._mark_stopping()
                    backend._attempt_cleanup("kill")
                    value = not backend.manager.has_tracked_processes
                else:
                    raise ValueError("unknown session command")
            except Exception as error:  # noqa: BLE001 - communicate command failures to their caller
                future.set_exception(error)
            else:
                future.set_result(value)

    def _run(self) -> None:
        backend = None
        errors: list[str] = []
        reason = "cancelled"
        exports = None
        self._publish(None, "starting", errors)
        try:
            if not self._cancel.is_set():
                settings = self.options._settings()
                backend = MonitorSession(self._target, list(self._args),
                                         console=Console(file=io.StringIO(), no_color=True),
                                         report_dir=settings.report_dir, attach_pid=self._attach_pid,
                                         attach_create_time=self._create_time,
                                         timeout=settings.timeout, settings=settings)
                backend.VERSION = _version()
                if self._manager_factory is not None:
                    manager = self._manager_factory(self._target, args=list(self._args))
                    backend.manager = manager
                    backend.detector.process_manager = manager
                if self._cancel.is_set():
                    return
                backend.start()
                self._publish(backend, "running", errors, kind="snapshot")
                while not self._cancel.is_set():
                    self._drain_commands(backend)
                    if backend.poll():
                        self._publish(backend, "running", errors, kind="snapshot")
                    if backend.timed_out:
                        reason = "timeout"
                        break
                    if backend.tree_finished:
                        reason = "target_exited"
                        break
                    self._cancel.wait(0.02)
        except Exception as error:  # noqa: BLE001 - partial startup/runtime failure is an explicit result
            reason = "failed"
            errors.append(str(error) or type(error).__name__)
        finally:
            self._publish(backend, "stopping", errors)
            if backend is not None:
                try:
                    backend.shutdown()
                except Exception as error:  # noqa: BLE001 - preserve cleanup failure alongside prior failure
                    errors.append(f"cleanup: {str(error) or type(error).__name__}")
                try:
                    backend.poll()
                    if self.options.export_on_close:
                        exports = self._export(backend)
                        errors.extend(exports.errors)
                    report = self._report_data(backend)
                except Exception as error:  # noqa: BLE001 - no silent final-report failures
                    errors.append(f"final report: {str(error) or type(error).__name__}")
                    report = None
                errors.extend(item for item in backend.monitor_errors if item not in errors)
            else:
                report = None
            from .report_contract import session_summary
            outcome = report.get("outcome", {}) if report else {}
            try:
                summary = _freeze(session_summary(report)) if report else _freeze({})
            except Exception as error:  # noqa: BLE001 - malformed partial evidence cannot prevent completion
                errors.append(f"summary: {str(error) or type(error).__name__}")
                summary = _freeze({})
            exit_code = self._exit_code(backend, reason, errors)
            result = SessionResult(reason, exit_code, outcome.get("startup", {}).get("state", "not_started"),
                                   outcome.get("cleanup", {}).get("state", "not_attempted"),
                                   backend.measurement_outcome if backend else "inconclusive",
                                   backend.full_coverage if backend else False,
                                   report.get("target_exit_code") if report else None,
                                   tuple(errors), exports, summary)
            with self._lock:
                self._result = result
                self._final_report = report
                self._publish(backend, "finished", errors)
                while not self._commands.empty():
                    _name, future = self._commands.get_nowait()
                    if future.set_running_or_notify_cancel():
                        future.set_exception(RuntimeError("session ended before command execution"))
                self._done.set()

    def _exit_code(self, backend, reason: str, errors: list[str]) -> int:
        if backend is None:
            return session_exit_code(startup_failed=reason == "failed", signal_breached=False,
                                     cancelled=reason == "cancelled", monitor_failed=bool(errors),
                                     over_budget=False, target_failed=False, inconclusive=True)
        settings = backend.settings
        return session_exit_code(
            startup_failed=backend._startup_state == "failed",
            signal_breached=severity_rank(backend.worst_severity) >= severity_rank(settings.fail_on),
            cancelled=reason == "cancelled", monitor_failed=reason == "failed" or bool(errors),
            over_budget=any(check.exceeded for check in check_budgets(settings.budgets, backend._peaks)),
            target_failed=settings.fail_on_target_error and backend.target_failed,
            inconclusive=((settings.require_full_coverage and not backend.full_coverage)
                          or (settings.fail_on_inconclusive and not backend.conclusive)),
        )

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.close() is None:
            raise TimeoutError("session cleanup is still running")


__all__ = ["CommandTimeoutError", "ExportResult", "Session", "SessionEvent", "SessionOptions",
           "SessionResult", "SessionStatus", "Snapshot", "Subscription"]
