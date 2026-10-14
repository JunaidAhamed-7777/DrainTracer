"""Process execution and lifecycle management with recursive PID tracking."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)


def _parent_pid_map() -> dict[int, int]:
    """Return ``{pid: parent_pid}`` for every visible process in one pass."""
    mapper = getattr(psutil, "_ppid_map", None)
    if mapper is not None:
        try:
            return dict(mapper())
        except (psutil.Error, OSError):
            pass
    mapping: dict[int, int] = {}
    for proc in psutil.process_iter(["ppid"]):
        ppid = proc.info.get("ppid")
        if ppid is not None:
            mapping[proc.pid] = ppid
    return mapping


def describe_pid(pid: int) -> str:
    """Best-effort executable path (or name) for display; never raises."""
    try:
        proc = psutil.Process(pid)
    except (psutil.Error, OSError, ValueError):
        return f"PID {pid}"
    for getter in (proc.exe, proc.name):
        try:
            value = getter()
        except (psutil.Error, OSError):
            continue
        if value:
            return value
    return f"PID {pid}"


# Characters that end or split an argument on a cmd.exe command line.
_CMD_SPECIAL = frozenset(" \t&|<>^(),;=\"")


def _quote_for_cmd(argument: str) -> str:
    """Quote one argument so cmd.exe passes it to a batch file unchanged."""
    if argument and not any(character in _CMD_SPECIAL for character in argument):
        return argument
    return '"' + argument.replace('"', '""') + '"'


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
        target_path: str | None,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        # ``None`` builds a manager that can only attach() to a running PID.
        self.target_path = str(Path(target_path).resolve()) if target_path else None
        self.args = list(args or [])
        self.cwd = cwd
        self.env = env
        self.poll_interval = poll_interval

        self._process: subprocess.Popen[str] | None = None
        self._root_pid: int | None = None
        self._tracked_pids: set[int] = set()
        self._identities: dict[int, float] = {}
        self._depths: dict[int, int] = {}
        self._parents: dict[int, int | None] = {}
        self._lock = threading.RLock()
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._paused = False
        self._attached = False
        self._last_error: str | None = None
        self._exit_code: int | None = None

    @property
    def root_pid(self) -> int | None:
        return self._root_pid

    @property
    def is_attached(self) -> bool:
        """Whether the root was attached by PID rather than launched."""
        return self._attached

    @property
    def is_running(self) -> bool:
        with self._lock:
            if self._attached:
                return self._root_pid is not None and self._pid_is_alive(self._root_pid)
            if self._process is None:
                return False
            return self._poll_launched() is None

    @property
    def exit_code(self) -> int | None:
        """Exit code of a launched root once it has exited.

        Attached processes were not started by DrainTracer, so their exit
        code is not available and this stays ``None``.
        """
        with self._lock:
            if self._process is not None:
                self._poll_launched()
            return self._exit_code

    def _poll_launched(self) -> int | None:
        code = self._process.poll() if self._process is not None else None
        if code is not None:
            self._exit_code = code
        return code

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

    def parent_of(self, pid: int) -> int | None:
        """Return the recorded parent PID of a tracked process, if known."""
        with self._lock:
            return self._parents.get(pid)

    @property
    def has_tracked_processes(self) -> bool:
        """Whether any root or descendant PID still needs cleanup."""
        with self._lock:
            return bool(self._tracked_pids)

    def start(self) -> int:
        """Launch the target process and begin PID tree polling."""
        with self._lock:
            if self.is_running:
                if self._root_pid is None:
                    raise ProcessManagerError("Process is running but root PID is unknown.")
                return self._root_pid

            if self.target_path is None:
                raise ProcessManagerError("No target path to launch; use attach() instead.")
            if not os.path.isfile(self.target_path):
                raise ProcessNotFoundError(f"Target file not found: {self.target_path}")

            command = self._build_command()
            popen_kwargs: dict = {
                "cwd": self.cwd,
                "env": self.env,
                # The dashboard owns keyboard input; a target reading the
                # shared console could swallow its keys or block on them.
                "stdin": subprocess.DEVNULL,
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
            self._identities.clear()
            self._depths = {self._root_pid: 0}
            self._parents = {self._root_pid: None}
            self._remember_identity(self._root_pid)
            self._paused = False
            self._attached = False
            self._last_error = None
            self._exit_code = None
            self._start_poll_thread()
            return self._root_pid

    def attach(self, pid: int) -> int:
        """Track an already running process (and its descendants) by PID.

        Attached processes are never terminated implicitly: ``detach()``
        releases them, and only an explicit ``kill()``/``terminate()`` ends
        them.
        """
        with self._lock:
            if self.is_running:
                raise ProcessManagerError("Already monitoring a process.")
            if pid <= 0:
                raise ProcessNotFoundError(f"Invalid PID: {pid}")
            if pid == os.getpid():
                raise ProcessManagerError("DrainTracer cannot attach to itself.")
            try:
                proc = psutil.Process(pid)
                if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                    raise psutil.NoSuchProcess(pid)
            except (psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
                raise ProcessNotFoundError(f"No running process with PID {pid}") from exc
            except psutil.AccessDenied:
                # Protected processes can still be listed; metrics will show
                # as unavailable rather than failing the attach.
                pass

            self._process = None
            self._root_pid = pid
            self._tracked_pids = {pid}
            self._identities.clear()
            self._depths = {pid: 0}
            self._parents = {pid: None}
            self._remember_identity(pid)
            self._paused = False
            self._attached = True
            self._last_error = None
            self._exit_code = None
            self._refresh_tracked_pids()
            self._start_poll_thread()
            return pid

    def detach(self) -> None:
        """Stop tracking without terminating; resume first if suspended."""
        with self._lock:
            self._stop_poll_thread()
            if self._paused:
                self.resume()
            self._process = None
            self._tracked_pids.clear()
            self._identities.clear()
            self._depths.clear()
            self._parents.clear()
            self._root_pid = None
            self._paused = False

    def pause(self) -> bool:
        """Suspend the root process and all tracked descendants."""
        with self._lock:
            if not self.is_running:
                return False
            if self._paused:
                return True
            self._refresh_tracked_pids()
            suspended: list[int] = []
            for pid in self._tracked_pids:
                if self._suspend_pid(pid):
                    suspended.append(pid)
                    continue
                for suspended_pid in suspended:
                    self._resume_pid(suspended_pid)
                self._paused = False
                self._last_error = "One or more processes could not be suspended"
                return False
            self._paused = True
            return True

    def resume(self) -> bool:
        """Resume a previously paused process tree."""
        with self._lock:
            if not self._paused:
                return True
            success = True
            for pid in self._tracked_pids:
                success = self._resume_pid(pid) and success
            self._paused = not success
            if not success:
                self._last_error = "One or more processes could not be resumed"
            return success

    def terminate(self, timeout: float = 3.0) -> None:
        """Gracefully terminate the process tree."""
        with self._lock:
            self._stop_poll_thread()
            if self._process is None and not self._tracked_pids:
                return

            self._refresh_tracked_pids()
            targets = set(self._tracked_pids)
            for pid in self._termination_order(targets):
                self._terminate_pid(pid, graceful=True)

            remaining = self._wait_for_pids(targets, timeout)
            if remaining:
                for pid in self._termination_order(remaining):
                    self._terminate_pid(pid, graceful=False)
                remaining = self._wait_for_pids(remaining, 0.5)

            self._poll_launched()
            self._process = None
            self._paused = False
            if remaining:
                self._tracked_pids = remaining
                self._last_error = (
                    f"Could not terminate process tree; remaining PIDs: "
                    f"{sorted(remaining)}"
                )
            else:
                self._tracked_pids.clear()
                self._identities.clear()
                self._depths.clear()
                self._parents.clear()
                self._root_pid = None

    def kill(self) -> None:
        """Force-kill the process tree."""
        with self._lock:
            self._stop_poll_thread()
            if self._process is None and not self._tracked_pids:
                return

            self._refresh_tracked_pids()
            targets = set(self._tracked_pids)
            for pid in self._termination_order(targets):
                self._terminate_pid(pid, graceful=False)

            if self._process is not None:
                try:
                    self._process.kill()
                except OSError:
                    pass

            remaining = self._wait_for_pids(targets, 0.5)
            self._poll_launched()
            self._process = None
            if remaining:
                self._tracked_pids = remaining
                self._last_error = (
                    f"Could not kill process tree; remaining PIDs: "
                    f"{sorted(remaining)}"
                )
            else:
                self._root_pid = None
                self._tracked_pids.clear()
                self._identities.clear()
                self._depths.clear()
                self._parents.clear()
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
            proc = self._open_tracked(pid)
            if proc is not None:
                processes.append(proc)
        return processes

    def _build_command(self) -> list[str] | str:
        path = Path(self.target_path)
        suffix = path.suffix.lower()

        if suffix in {".py"}:
            return [sys.executable, str(path), *self.args]
        if suffix in {".bat", ".cmd"} and sys.platform == "win32":
            # cmd.exe parses its own command line, so Python's list quoting
            # is not enough: an argument such as "a b" or "x&y" would be
            # split or run as a second command. /s keeps everything between
            # the outer quotes intact, each part is quoted for cmd, and /d
            # skips AutoRun commands.
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            inner = " ".join(_quote_for_cmd(part) for part in (str(path), *self.args))
            return f'"{comspec}" /d /s /c "{inner}"'
        return [str(path), *self.args]

    def _start_poll_thread(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._poll_thread = None
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="ProcessManagerPoll",
            daemon=True,
        )
        self._poll_thread.start()

    def _stop_poll_thread(self) -> None:
        self._poll_stop.set()
        thread = self._poll_thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        if thread is None or not thread.is_alive():
            self._poll_thread = None

    def _poll_loop(self) -> None:
        while not self._poll_stop.wait(self.poll_interval):
            try:
                with self._lock:
                    self._refresh_tracked_pids()
                    if self._process is not None and self._process.poll() is not None:
                        if not self._any_tracked_alive():
                            break
                    if self._attached and not self._tracked_pids:
                        break
            except Exception:
                # Keep tracking: one failed refresh must not end discovery.
                logger.exception("Process tree refresh failed")

    def _refresh_tracked_pids(self) -> None:
        if self._root_pid is None:
            return

        discovered: set[int] = set(self._tracked_pids)
        root = self._open_tracked(self._root_pid)
        if root is not None:
            discovered.update(self._collect_descendants(root))

        alive = {pid for pid in discovered if self._pid_is_alive(pid)}
        self._tracked_pids = alive
        self._identities = {
            pid: created for pid, created in self._identities.items() if pid in alive
        }
        self._depths = {pid: depth for pid, depth in self._depths.items() if pid in alive}
        self._parents = {
            pid: parent for pid, parent in self._parents.items() if pid in alive
        }

    def _collect_descendants(self, root: psutil.Process) -> set[int]:
        """Walk the tree from one parent-PID snapshot.

        Walking ``parent()`` per child costs a full process enumeration on
        Windows each time; one map serves the whole refresh and also records
        each child's parent for the dashboard tree view.
        """
        pids: set[int] = {root.pid}
        self._depths[root.pid] = 0
        self._parents[root.pid] = None
        self._remember_process(root)
        try:
            root_created = root.create_time()
        except (psutil.Error, OSError):
            root_created = None

        children_of: dict[int, list[int]] = {}
        for pid, ppid in _parent_pid_map().items():
            if pid != ppid:
                children_of.setdefault(ppid, []).append(pid)

        queue: deque[tuple[int, int, float | None]] = deque([(root.pid, 0, root_created)])
        while queue:
            parent_pid, depth, parent_created = queue.popleft()
            for child_pid in children_of.get(parent_pid, ()):
                if child_pid in pids:
                    continue
                try:
                    child = psutil.Process(child_pid)
                except (psutil.Error, OSError):
                    continue
                try:
                    created: float | None = child.create_time()
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except (psutil.AccessDenied, OSError):
                    # Still a descendant; it just cannot be age-checked.
                    created = None
                # A child older than its parent means the PID was reused.
                if (
                    parent_created is not None
                    and created is not None
                    and created < parent_created
                ):
                    continue
                pids.add(child_pid)
                self._remember_process(child)
                self._depths[child_pid] = depth + 1
                self._parents[child_pid] = parent_pid
                queue.append((child_pid, depth + 1, created))
        return pids

    def _termination_order(self, pids: set[int]) -> list[int]:
        """Kill descendants before their parents while parent links still exist."""
        return sorted(pids, key=lambda pid: (self._depths.get(pid, 0), pid), reverse=True)

    def _remember_identity(self, pid: int) -> None:
        try:
            self._remember_process(psutil.Process(pid))
        except (psutil.Error, OSError) as exc:
            self._last_error = str(exc)

    def _remember_process(self, proc: psutil.Process) -> None:
        try:
            self._identities[proc.pid] = proc.create_time()
        except (psutil.Error, OSError):
            return

    def _open_tracked(self, pid: int) -> psutil.Process | None:
        """Return the original process for a PID, rejecting reused PID values."""
        with self._lock:
            return self._open_tracked_unlocked(pid)

    def _open_tracked_unlocked(self, pid: int) -> psutil.Process | None:
        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            self._identities.pop(pid, None)
            return None

        # Windows has no zombies, and psutil already rejects exited processes
        # when opening them; status() would cost a system-wide process
        # enumeration per call, so the check is POSIX-only. Reused PIDs are
        # caught by the creation-time comparison below on every platform.
        if sys.platform != "win32":
            try:
                if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                    return None
            except psutil.AccessDenied:
                pass
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                self._identities.pop(pid, None)
                return None

        expected = self._identities.get(pid)
        try:
            created = proc.create_time()
        except psutil.AccessDenied:
            return proc
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            self._identities.pop(pid, None)
            return None

        if expected is None:
            self._identities[pid] = created
            return proc
        if abs(created - expected) > 1.0:
            self._identities.pop(pid, None)
            self._depths.pop(pid, None)
            self._parents.pop(pid, None)
            self._last_error = f"PID {pid} was reused; the new process was left untouched"
            return None
        return proc

    def _any_tracked_alive(self) -> bool:
        return any(self._pid_is_alive(pid) for pid in self._tracked_pids)

    def _pid_is_alive(self, pid: int) -> bool:
        return self._open_tracked(pid) is not None

    def _wait_for_pids(self, pids: set[int], timeout: float) -> set[int]:
        deadline = time.monotonic() + max(timeout, 0.0)
        remaining = set(pids)
        while remaining and time.monotonic() < deadline:
            remaining = {pid for pid in remaining if self._pid_is_alive(pid)}
            if remaining:
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return {pid for pid in remaining if self._pid_is_alive(pid)}

    def _suspend_pid(self, pid: int) -> bool:
        if sys.platform != "win32":
            return True
        proc = self._open_tracked(pid)
        if proc is None:
            return False
        try:
            proc.suspend()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as exc:
            logger.debug("Could not suspend pid %s: %s", pid, exc)
            return False

    def _resume_pid(self, pid: int) -> bool:
        if sys.platform != "win32":
            return True
        proc = self._open_tracked(pid)
        if proc is None:
            return False
        try:
            proc.resume()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as exc:
            logger.debug("Could not resume pid %s: %s", pid, exc)
            return False

    def _terminate_pid(self, pid: int, graceful: bool) -> None:
        proc = self._open_tracked(pid)
        if proc is None:
            return
        try:
            if graceful:
                proc.terminate()
            else:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as exc:
            logger.debug("Could not terminate pid %s: %s", pid, exc)

    def __enter__(self) -> ProcessManager:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.terminate()
