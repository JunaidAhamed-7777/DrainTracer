"""Agent-only suspended Windows launch with a noninheritable kill-on-close job.

The ordinary CLI ProcessManager continues to use subprocess.Popen. This adapter
holds native process handles, not PID-based authority, across job assignment.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from .process_runner import ProcessManager, ProcessManagerError, python_launch

# The one variable an agent launch may add to its inherited environment.
VENV_LAUNCHER = "__PYVENV_LAUNCHER__"


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                ("x_size", wintypes.DWORD), ("y_size", wintypes.DWORD),
                ("x_chars", wintypes.DWORD), ("y_chars", wintypes.DWORD),
                ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
                ("reserved_bytes", wintypes.LPVOID), ("stdin", wintypes.HANDLE),
                ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("startup", _StartupInfo), ("attributes", wintypes.LPVOID)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("read_count", "write_count", "other_count", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IoCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID),
                ("inherit", wintypes.BOOL)]


@lru_cache(maxsize=1)
def _job_api():
    if os.name != "nt":
        raise OSError("agent-owned jobs require Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                         ctypes.POINTER(_SecurityAttributes), wintypes.DWORD, wintypes.DWORD,
                         wintypes.HANDLE], wintypes.HANDLE),
        "InitializeProcThreadAttributeList": ([wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                                ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        "UpdateProcThreadAttribute": ([wintypes.LPVOID, wintypes.DWORD, ctypes.c_size_t,
                                       wintypes.LPVOID, ctypes.c_size_t, wintypes.LPVOID,
                                       wintypes.LPVOID], wintypes.BOOL),
        "DeleteProcThreadAttributeList": ([wintypes.LPVOID], None),
        "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID,
                            wintypes.LPVOID, wintypes.BOOL, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.LPCWSTR, ctypes.POINTER(_StartupInfoEx),
                            ctypes.POINTER(_ProcessInfo)], wintypes.BOOL),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetEnvironmentStringsW": ([], ctypes.c_void_p),
        "FreeEnvironmentStringsW": ([ctypes.c_void_p], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def _environment_name(entry: str) -> str:
    # Hidden per-drive entries such as "=C:=C:\work" start with "=".
    end = entry.find("=", 1)
    return entry if end < 0 else entry[:end]


def _environment_block(overrides: Mapping[str, str]):
    """This process's native environment block with ``overrides`` applied.

    The block is read with GetEnvironmentStringsW rather than rebuilt from
    ``os.environ``, which upper-cases names and omits the hidden per-drive
    working directories, so every other entry reaches the child unchanged.
    """
    for name, value in overrides.items():
        if not name or "=" in name or "\0" in name or "\0" in value:
            raise ValueError("invalid environment override")
    api = _job_api()
    pointer = api.GetEnvironmentStringsW()
    if not pointer:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        units = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint16))
        length = 0
        while units[length] or units[length + 1]:
            length += 1
        raw = ctypes.string_at(pointer, length * 2)
    finally:
        api.FreeEnvironmentStringsW(pointer)
    replaced = {name.upper() for name in overrides}
    entries = [entry for entry in raw.decode("utf-16-le", "surrogatepass").split("\0")
               if entry and _environment_name(entry).upper() not in replaced]
    entries.extend(f"{name}={value}" for name, value in overrides.items())
    # Windows keeps the block sorted by case-insensitive name; the sort is
    # stable, so inherited entries keep their relative order.
    entries.sort(key=lambda entry: _environment_name(entry).upper())
    return ctypes.create_string_buffer(("\0".join(entries) + "\0\0").encode("utf-16-le", "surrogatepass"))


class JobProcess:
    """The small pid/poll/kill interface consumed by ProcessManager.

    Construction either returns a running process already assigned to its job,
    or stops the still-suspended process. Closing the last job handle also
    stops remaining normal descendants after an agent crash.
    """

    def __init__(self, command: list[str], *, cwd: str | None = None,
                 environment_overrides: Mapping[str, str] | None = None) -> None:
        self.pid = 0
        self._process = self._job = None
        self._exit_code = None
        if (not command or not all(isinstance(item, str) and "\0" not in item for item in command)
                or not Path(command[0]).is_absolute()):
            raise ValueError("agent launch requires an absolute executable and literal arguments")
        line = subprocess.list2cmdline(command)
        if len(line) >= 32767:
            raise ValueError("agent launch command exceeds the Windows command-line limit")
        api = _job_api()
        # None inherits this process's environment block as is.
        environment = _environment_block(environment_overrides) if environment_overrides else None
        info = _ProcessInfo()
        null_handle = None
        initialized = False
        attributes = None
        try:
            self._job = _check(api.CreateJobObjectW(None, None))
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
            _check(api.SetInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
            null_handle = api.CreateFileW("NUL", 0xC0000000, 3, ctypes.byref(security), 3, 0x80, None)
            if null_handle == ctypes.c_void_p(-1).value:
                null_handle = None
                raise ctypes.WinError(ctypes.get_last_error())
            size = ctypes.c_size_t()
            api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            _check(api.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)))
            initialized = True
            handles = (wintypes.HANDLE * 1)(null_handle)
            _check(api.UpdateProcThreadAttribute(attributes, 0, 0x00020002,
                                                 handles, ctypes.sizeof(handles), None, None))
            startup = _StartupInfoEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin = startup.startup.stdout = startup.startup.stderr = null_handle
            startup.attributes = ctypes.cast(attributes, wintypes.LPVOID)
            # CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
            # plus CREATE_UNICODE_ENVIRONMENT for an explicit block.
            flags = 0x08080004 | (0x400 if environment is not None else 0)
            _check(api.CreateProcessW(command[0], ctypes.create_unicode_buffer(line), None, None,
                                      True, flags, environment, cwd, ctypes.byref(startup), ctypes.byref(info)))
            self._process, self.pid = info.process, info.pid
            _check(api.AssignProcessToJobObject(self._job, self._process))
            if api.ResumeThread(info.thread) != 1:
                raise OSError("the agent-owned primary thread did not resume exactly once")
        except BaseException:
            if self._process:
                api.TerminateProcess(self._process, 1)
                api.WaitForSingleObject(self._process, 3000)
            self.close()
            raise
        finally:
            if info.thread:
                api.CloseHandle(info.thread)
            if initialized:
                api.DeleteProcThreadAttributeList(attributes)
            if null_handle:
                api.CloseHandle(null_handle)

    def poll(self) -> int | None:
        if self._process:
            api = _job_api()
            result = api.WaitForSingleObject(self._process, 0)
            if result == 0:
                code = wintypes.DWORD()
                _check(api.GetExitCodeProcess(self._process, ctypes.byref(code)))
                self._exit_code = code.value
            elif result != 258:
                raise ctypes.WinError(ctypes.get_last_error())
        return self._exit_code

    def kill(self) -> None:
        if self._job:
            _check(_job_api().TerminateJobObject(self._job, 1))

    def close(self) -> None:
        api = _job_api()
        if self._job:
            api.CloseHandle(self._job)
            self._job = None
        if self._process:
            # A bounded wait observes asynchronous kill-on-close completion.
            api.WaitForSingleObject(self._process, 3000)
            self.poll()
            api.CloseHandle(self._process)
            self._process = None

    def __del__(self):
        # No exceptions from GC or partially constructed objects. The OS owns
        # the final close even when Python cannot perform orderly shutdown.
        try:
            self.close()
        except (OSError, AttributeError, TypeError):
            pass


class AgentProcessManager(ProcessManager):
    """Agent launch adapter; attach still uses the ordinary identity checks."""

    def _python_launch(self):
        """Start a virtual environment's base interpreter directly.

        The venv redirector would otherwise be the job's root, and every
        ``sys.executable`` child would add another redirector level. Only
        ``__PYVENV_LAUNCHER__`` is named here; JobProcess adds it to the
        inherited server environment, which is otherwise unchanged.
        """
        if self.env is not None:
            raise ProcessManagerError("agent launches inherit their server environment")
        executable, environment = python_launch(None)
        if environment is None:
            return executable, None
        return executable, {VENV_LAUNCHER: environment[VENV_LAUNCHER]}

    def __init__(self, target_path, args=None, **kwargs):
        super().__init__(target_path, args=args, **kwargs)
        self._owned_job: JobProcess | None = None

    def _spawn_process(self, command, popen_kwargs):
        if isinstance(command, str):
            raise ProcessManagerError("agent launches support .exe and .py targets; wrap batch commands explicitly")
        overrides = popen_kwargs.get("env")
        if self.env is not None or (overrides is not None and set(overrides) != {VENV_LAUNCHER}):
            raise ProcessManagerError("agent launches inherit their server environment")
        process = JobProcess(command, cwd=popen_kwargs.get("cwd"), environment_overrides=overrides)
        self._owned_job = process
        return process

    def _close_job(self):
        if self._owned_job:
            self._owned_job.close()
            self._owned_job = None

    def terminate(self, timeout=3.0):
        try:
            super().terminate(timeout)
        finally:
            self._close_job()

    def kill(self):
        try:
            super().kill()
        finally:
            self._close_job()
