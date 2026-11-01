"""Agent-only suspended Windows launch with a noninheritable kill-on-close job.

The ordinary CLI ProcessManager continues to use subprocess.Popen. This adapter
holds native process handles, not PID-based authority, across job assignment.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

from .process_runner import ProcessManager, ProcessManagerError


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
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


class JobProcess:
    """The small pid/poll/kill interface consumed by ProcessManager.

    Construction either returns a running process already assigned to its job,
    or stops the still-suspended process. Closing the last job handle also
    stops remaining normal descendants after an agent crash.
    """

    def __init__(self, command: list[str], *, cwd: str | None = None) -> None:
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
            # CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT.
            _check(api.CreateProcessW(command[0], ctypes.create_unicode_buffer(line), None, None,
                                      True, 0x08080004, None, cwd, ctypes.byref(startup), ctypes.byref(info)))
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

    def __init__(self, target_path, args=None, **kwargs):
        super().__init__(target_path, args=args, **kwargs)
        self._owned_job: JobProcess | None = None

    def _spawn_process(self, command, popen_kwargs):
        if isinstance(command, str):
            raise ProcessManagerError("agent launches support .exe and .py targets; wrap batch commands explicitly")
        if popen_kwargs.get("env") is not None:
            raise ProcessManagerError("agent launches inherit their server environment")
        process = JobProcess(command, cwd=popen_kwargs.get("cwd"))
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
