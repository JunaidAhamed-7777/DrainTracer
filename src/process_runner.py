"""Process execution and lifecycle management with recursive PID tracking."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
import psutil

logger = logging.getLogger(__name__)


class ProcessManagerError(Exception):
    """Base error for process manager failures."""


class ProcessNotFoundError(ProcessManagerError):
    """Raised when the target executable does not exist."""


class ProcessAccessDeniedError(ProcessManagerError):
    """Raised when the target cannot be started or controlled due to permissions."""


class ProcessManager:
    """
    Launch and control a target process while tracking its full process tree.

    Supports start, pause, resume, and terminate/kill operations. Child PIDs
    are refreshed on demand and can be polled via a background thread.
    """

    def __init__(
        self,
        target_path: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        self.target_path = str(Path(target_path).resolve())
        self.args = list(args or [])
        self.cwd = cwd
        self.env = env
        self.poll_interval = poll_interval

        self._process: subprocess.Popen[str] | None = None
        self._root_pid: int | None = None
        self._tracked_pids: set[int] = set()
        self._lock = threading.RLock()
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._paused = False
        self._last_error: str | None = None

    @property
    def root_pid(self) -> int | None:
        return self._root_pid

    @property
    def is_running(self) -> bool:
        with self._lock:
            if self._process is None:
                return False
            return self._process.poll() is None

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def tracked_pids(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._tracked_pids)

    def start(self) -> int:
        """Launch the target process and begin PID tree polling."""
        with self._lock:
            if self.is_running:
                if self._root_pid is None:
                    raise ProcessManagerError("Process is running but root PID is unknown.")
                return self._root_pid

            if not os.path.isfile(self.target_path):
                raise ProcessNotFoundError(f"Target file not found: {self.target_path}")

            command = self._build_command()
            popen_kwargs: dict = {
                "cwd": self.cwd,
                "env": self.env,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            try:
                self._process = subprocess.Popen(command, **popen_kwargs)
            except PermissionError as exc:
                self._last_error = str(exc)
                raise ProcessAccessDeniedError(
                    f"Access denied starting process: {self.target_path}"
                ) from exc
            except OSError as exc:
                self._last_error = str(exc)
                raise ProcessManagerError(f"Failed to start process: {exc}") from exc

            self._root_pid = self._process.pid
            self._tracked_pids = {self._root_pid}
            self._paused = False
            self._last_error = None
            self._start_poll_thread()
            return self._root_pid

    def pause(self) -> None:
        """Suspend the root process and all tracked descendants."""
        with self._lock:
            if not self.is_running:
                return
            if self._paused:
                return
            self._refresh_tracked_pids()
            for pid in self._tracked_pids:
                self._suspend_pid(pid)
            self._paused = True

    def resume(self) -> None:
        """Resume a previously paused process tree."""
        with self._lock:
            if not self._paused:
                return
            for pid in self._tracked_pids:
                self._resume_pid(pid)
            self._paused = False

    def terminate(self, timeout: float = 3.0) -> None:
        """Gracefully terminate the process tree."""
        with self._lock:
            self._stop_poll_thread()
            if self._process is None:
                return

            self._refresh_tracked_pids()
            for pid in sorted(self._tracked_pids, reverse=True):
                self._terminate_pid(pid, graceful=True)

            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.kill()
            finally:
                self._process = None
                self._root_pid = None
                self._tracked_pids.clear()
                self._paused = False

    def kill(self) -> None:
        """Force-kill the process tree."""
        with self._lock:
            self._stop_poll_thread()
            if self._process is None and not self._tracked_pids:
                return

            self._refresh_tracked_pids()
            for pid in sorted(self._tracked_pids, reverse=True):
                self._terminate_pid(pid, graceful=False)

            if self._process is not None:
                try:
                    self._process.kill()
                except OSError:
                    pass
                self._process = None

            self._root_pid = None
            self._tracked_pids.clear()
            self._paused = False

    def refresh_process_tree(self) -> set[int]:
        """Synchronously refresh and return the tracked PID set."""
        with self._lock:
            self._refresh_tracked_pids()
            return set(self._tracked_pids)

    def iter_psutil_processes(self) -> list[psutil.Process]:
        """Return psutil.Process objects for all tracked PIDs that are accessible."""
        processes: list[psutil.Process] = []
        for pid in self.tracked_pids:
            try:
                proc = psutil.Process(pid)
                if proc.is_running():
                    processes.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return processes

    def _build_command(self) -> list[str]:
        path = Path(self.target_path)
        suffix = path.suffix.lower()

        if suffix in {".py"}:
            return [sys.executable, str(path), *self.args]
        if suffix in {".bat", ".cmd"}:
            if sys.platform == "win32":
                return ["cmd.exe", "/c", str(path), *self.args]
        return [str(path), *self.args]

    def _start_poll_thread(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="ProcessManagerPoll",
            daemon=True,
        )
        self._poll_thread.start()

    def _stop_poll_thread(self) -> None:
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=1.0)
        self._poll_thread = None

    def _poll_loop(self) -> None:
        while not self._poll_stop.wait(self.poll_interval):
            with self._lock:
                if self._process is not None and self._process.poll() is not None:
                    self._refresh_tracked_pids()
                    if not self._any_tracked_alive():
                        break
                self._refresh_tracked_pids()

    def _refresh_tracked_pids(self) -> None:
        if self._root_pid is None:
            return

        discovered: set[int] = set()
        try:
            root = psutil.Process(self._root_pid)
            discovered.update(self._collect_descendants(root))
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            self._last_error = str(exc)
            discovered.add(self._root_pid)

        alive = {pid for pid in discovered if self._pid_is_alive(pid)}
        if alive:
            self._tracked_pids = alive
        elif self._root_pid is not None:
            self._tracked_pids = {self._root_pid}

    def _collect_descendants(self, root: psutil.Process) -> set[int]:
        pids: set[int] = {root.pid}
        try:
            for child in root.children(recursive=True):
                pids.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return pids

    def _any_tracked_alive(self) -> bool:
        return any(self._pid_is_alive(pid) for pid in self._tracked_pids)

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        try:
            proc = psutil.Process(pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    @staticmethod
    def _suspend_pid(pid: int) -> None:
        if sys.platform != "win32":
            return
        try:
            psutil.Process(pid).suspend()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            logger.debug("Could not suspend pid %s: %s", pid, exc)

    @staticmethod
    def _resume_pid(pid: int) -> None:
        if sys.platform != "win32":
            return
        try:
            psutil.Process(pid).resume()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            logger.debug("Could not resume pid %s: %s", pid, exc)

    @staticmethod
    def _terminate_pid(pid: int, graceful: bool) -> None:
        try:
            proc = psutil.Process(pid)
            if graceful:
                proc.terminate()
            else:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            logger.debug("Could not terminate pid %s: %s", pid, exc)

    def __enter__(self) -> ProcessManager:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.terminate()
