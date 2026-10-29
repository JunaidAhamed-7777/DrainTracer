"""Process execution and lifecycle management with recursive PID tracking."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _windows_process_api() -> ctypes.CDLL:
    """Bind pointer-sized process handles once, including on 64-bit Windows."""
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _windows_process_exited(pid: int) -> bool:
    """Check exit without waiting or relying on a PID disappearing.

    Windows can expose a terminated process while another owner holds its
    handle. Creation time and psutil.is_running() can still succeed then.
    """
    api = _windows_process_api()
    handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
            return True
        raise ctypes.WinError(error)
    try:
        result = api.WaitForSingleObject(handle, 0)
        if result == 0:  # WAIT_OBJECT_0: the process has exited
            return True
        if result == 0x00000102:  # WAIT_TIMEOUT: still running
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)


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


# Creation times tell a process apart from a later one that reuses its PID.
# Windows reports them exactly, so a process started even a few
# milliseconds later is a different one. Linux derives them from boot time
# and clock ticks, but repeated reads still identify the same start tick;
# a one-second tolerance could mistake a quickly reused PID for its owner.
_SAME_START = 0.01 if sys.platform == "win32" else 0.001


@dataclass(frozen=True)
class _Departed:
    """A tree member that exited; the orphans it left are found through its PID."""

    created: float | None
    gone_at: float
    depth: int


def _reused_since(pid: int, original: float | None) -> float | None:
    """Creation time of another process now using ``pid``, or ``None``."""
    try:
        created = psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None
    if original is not None and abs(created - original) <= _SAME_START:
        return None
    return created


def _orphan_of(gone: _Departed, holder: float | None, created: float | None) -> bool:
    """Whether a process naming ``gone``'s PID as its parent really is its child.

    A child is born while its parent lives. One created after the parent was
    seen gone, or after another process took over the PID, belongs to that
    other process.
    """
    if created is None:
        return False  # cannot be checked against a PID that may be reused
    if created > gone.gone_at:
        return False
    return holder is None or created < holder


def _monitor_chain() -> dict[int, float | None]:
    """DrainTracer's own process and its ancestors, with their creation times.

    ``parents()`` checks each link's creation time, so a stale parent PID
    reused by an unrelated process does not join the chain.
    """
    chain: dict[int, float | None] = {}
    try:
        own = psutil.Process(os.getpid())
        members = [own, *own.parents()]
    except (psutil.Error, OSError):
        return {os.getpid(): None}
    for member in members:
        try:
            chain[member.pid] = member.create_time()
        except (psutil.Error, OSError):
            chain[member.pid] = None
    return chain


# Characters that end or split an argument on a cmd.exe command line.
_CMD_BARE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#$*+-./:?@\\_"
)


def _quote_for_cmd(argument: str) -> str:
    """Quote one argument so cmd.exe passes it to a batch file unchanged.

    ASCII outside a small safe set is quoted, which covers the delimiters
    that split batch arguments (space, comma, semicolon, equals) and the
    characters cmd.exe acts on. cmd.exe expands ``%NAME%`` even inside
    quotes, before it looks for ``&`` or ``|``, so each ``%`` is written as
    ``%%cd:~,%``: an empty substring of ``%cd%`` then sits between it and
    the next character, and no variable name can form. Line breaks would
    end the command, so they cannot be passed at all.
    """
    if any(character in "\r\n\0" for character in argument):
        raise ProcessManagerError("Arguments for batch files cannot contain line breaks")
    if argument and all(
        character in _CMD_BARE or (not character.isascii() and character.isprintable())
        for character in argument
    ):
        return argument
    return '"' + argument.replace('"', '""').replace("%", "%%cd:~,%") + '"'


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

    # How long an exited member's PID is still searched for orphans.
    DEPARTED_SECONDS = 10.0
    # TerminateProcess is asynchronous; allow the tree to finish kernel cleanup.
    KILL_WAIT_SECONDS = 3.0

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
        # Keep partial suspension state so failed rollback/detach can retry.
        self._suspended_pids: set[int] = set()
        self._attached = False
        self._last_error: str | None = None
        self._exit_code: int | None = None
        # DrainTracer and the processes it runs inside, never to be tracked.
        self._monitor: dict[int, float | None] = {}
        # The root's creation time outlives the root, so a process that
        # later reuses its PID is never taken for it.
        self._root_created: float | None = None
        # Members that exited lately, still walked for the orphans they left.
        self._departed: dict[int, _Departed] = {}
        # A launched Linux tree gets its own session. Unlike parent PIDs,
        # that session survives reparenting when a short-lived root exits.
        self._linux_session_id: int | None = None

    @property
    def root_pid(self) -> int | None:
        return self._root_pid

    @property
    def contains_monitor(self) -> bool:
        """Whether the attached root is an ancestor of DrainTracer itself.

        Attaching to the terminal, an IDE, or explorer.exe makes DrainTracer
        a descendant of its own target. That branch is left out: pausing it
        would freeze the monitor and its terminal, and killing it would end
        them.
        """
        return self._attached and self._root_pid in self._monitor

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

    def created_at(self, pid: int) -> float | None:
        """Return the creation time recorded for a tracked PID, if known.

        Together with the PID it identifies one process: a later process
        that reuses the PID has a different creation time.
        """
        with self._lock:
            created = self._identities.get(pid)
            if created is None and pid == self._root_pid:
                created = self._root_created
            return created

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
            elif sys.platform.startswith("linux"):
                popen_kwargs["start_new_session"] = True

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
            self._linux_session_id = (
                self._root_pid if sys.platform.startswith("linux") else None
            )
            self._tracked_pids = {self._root_pid}
            self._identities.clear()
            self._depths = {self._root_pid: 0}
            self._parents = {self._root_pid: None}
            self._remember_identity(self._root_pid)
            self._root_created = self._identities.get(self._root_pid)
            self._departed.clear()
            self._paused = False
            self._suspended_pids.clear()
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
            self._linux_session_id = None
            self._root_pid = pid
            self._tracked_pids = {pid}
            self._identities.clear()
            self._depths = {pid: 0}
            self._parents = {pid: None}
            self._remember_identity(pid)
            self._root_created = self._identities.get(pid)
            self._departed.clear()
            self._monitor = _monitor_chain()
            self._paused = False
            self._suspended_pids.clear()
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
            if self._suspended_pids and not self.resume():
                self._last_error = (
                    "Cannot detach while processes remain suspended; "
                    f"retry resume for PIDs {sorted(self._suspended_pids)}"
                )
                self._start_poll_thread()
                raise ProcessManagerError(self._last_error)
            self._process = None
            self._tracked_pids.clear()
            self._identities.clear()
            self._depths.clear()
            self._parents.clear()
            self._departed.clear()
            self._root_pid = None
            self._linux_session_id = None
            self._paused = False
            self._suspended_pids.clear()

    def pause(self) -> bool:
        """Suspend the root process and all tracked descendants."""
        with self._lock:
            if not self.is_running:
                self._refresh_tracked_pids()
                if not self._tracked_pids:
                    return False
            if self._paused and self._tracked_pids <= self._suspended_pids:
                return True
            if self.contains_monitor:
                # The root may be the terminal DrainTracer draws in.
                self._last_error = (
                    "DrainTracer runs inside this process tree, and suspending it "
                    "could freeze DrainTracer's own terminal"
                )
                return False
            self._refresh_tracked_pids()
            # Stop parents first, reducing the chance of new children being
            # created while their tree is being suspended.
            for pid in reversed(self._termination_order(self._tracked_pids)):
                if pid in self._suspended_pids:
                    continue
                # An interruption after the OS suspends the process must
                # still leave a resume path during session cleanup.
                self._suspended_pids.add(pid)
                if self._suspend_pid(pid):
                    continue
                self._suspended_pids.discard(pid)
                self.resume()
                self._last_error = "One or more processes could not be suspended"
                if self._suspended_pids:
                    self._last_error += "; some remain suspended, so retry resume or detach"
                return False
            self._paused = True
            return True

    def resume(self) -> bool:
        """Resume a previously paused process tree."""
        with self._lock:
            if not self._suspended_pids:
                self._paused = False
                return True
            for pid in tuple(self._suspended_pids):
                if self._resume_pid(pid):
                    self._suspended_pids.discard(pid)
            success = not self._suspended_pids
            self._paused = not success
            if not success:
                self._last_error = "One or more processes could not be resumed"
            return success

    def terminate(self, timeout: float = 3.0) -> None:
        """Gracefully terminate the process tree."""
        with self._lock:
            if self._refuse_to_end_monitor():
                return
            self._stop_poll_thread()
            if self._process is None and not self._tracked_pids:
                return

            self._refresh_tracked_pids()
            if self._suspended_pids:
                # POSIX defers SIGTERM while stopped; let its handler run.
                self.resume()
            targets = set(self._tracked_pids)
            for pid in self._termination_order(targets):
                self._terminate_pid(pid, graceful=True)

            remaining = self._wait_for_pids(targets, timeout)
            if remaining:
                for pid in self._termination_order(remaining):
                    self._terminate_pid(pid, graceful=False)
                remaining = self._wait_for_pids(remaining, self.KILL_WAIT_SECONDS, kill_new=True)

            self._poll_launched()
            self._process = None
            self._suspended_pids.intersection_update(remaining)
            self._paused = bool(self._suspended_pids)
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
                self._departed.clear()
                self._root_pid = None
                self._linux_session_id = None

    def kill(self) -> None:
        """Force-kill the process tree."""
        with self._lock:
            if self._refuse_to_end_monitor():
                return
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

            remaining = self._wait_for_pids(targets, self.KILL_WAIT_SECONDS, kill_new=True)
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
                self._linux_session_id = None
                self._tracked_pids.clear()
                self._identities.clear()
                self._depths.clear()
                self._parents.clear()
                self._departed.clear()
            self._suspended_pids.intersection_update(remaining)
            self._paused = bool(self._suspended_pids)

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
            # is not enough: an argument such as "a b", "x&y", or "%PATH%"
            # would be split, run as a second command, or expanded. /s keeps
            # everything between the outer quotes intact and each part is
            # quoted for cmd. /d skips AutoRun commands, /e:on enables the
            # substring syntax the % escape relies on, and /v:off keeps !
            # literal.
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            inner = " ".join(_quote_for_cmd(part) for part in (str(path), *self.args))
            return f'"{comspec}" /d /e:on /v:off /s /c "{inner}"'
        if suffix in {".bat", ".cmd"}:
            raise ProcessManagerError(
                "Windows batch files require Windows; choose a Linux executable, "
                "Python script, or executable script with a shebang"
            )
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

        now = time.time()
        alive: set[int] = set()
        for pid in self._tracked_pids:
            created = self._identities.get(pid)
            if created is None and pid == self._root_pid:
                created = self._root_created
            if self._pid_is_alive(pid):
                alive.add(pid)
            elif pid not in self._departed:
                self._departed[pid] = _Departed(created, now, self._depths.get(pid, 0))
        self._departed = {
            pid: gone
            for pid, gone in self._departed.items()
            if now - gone.gone_at <= self.DEPARTED_SECONDS
        }

        members = self._walk_tree(alive)
        self._tracked_pids = members
        self._suspended_pids.intersection_update(members)
        self._identities = {
            pid: created for pid, created in self._identities.items() if pid in members
        }
        self._depths = {pid: depth for pid, depth in self._depths.items() if pid in members}
        self._parents = {
            pid: parent for pid, parent in self._parents.items() if pid in members
        }

    def _walk_tree(self, alive: set[int]) -> set[int]:
        """Every live member of the tree, found from one parent-PID snapshot.

        The walk starts at each live member and at each member that exited
        in the last few seconds: Windows does not re-parent orphans, so a
        process that outlives its parent can only be found through the
        parent's old PID. Walking ``parent()`` per child would cost a full
        process enumeration each time; one map serves the whole refresh
        and also records each child's parent for the dashboard tree view.
        """
        parents = _parent_pid_map()
        children_of: dict[int, list[int]] = {}
        for pid, ppid in parents.items():
            if pid != ppid:
                children_of.setdefault(ppid, []).append(pid)

        members = set(alive)
        session_members = self._linux_session_members(parents)
        # Seed only surviving roots, then walk their parent links normally
        # so descendant depths (and child-first cleanup) remain accurate.
        members.update(
            pid for pid in session_members if parents.get(pid) not in session_members
        )
        # Departed parents first, while their PIDs still mean them.
        queue: deque[int] = deque([*self._departed, *sorted(members)])
        while queue:
            parent_pid = queue.popleft()
            gone = self._departed.get(parent_pid)
            if gone is not None:
                parent_created, depth = gone.created, gone.depth
                holder: float | None = None
                if children_of.get(parent_pid):
                    holder = _reused_since(parent_pid, gone.created)
            else:
                parent_created = self._identities.get(parent_pid)
                depth = self._depths.get(parent_pid, 0)
            for child_pid in children_of.get(parent_pid, ()):
                if child_pid in members:
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
                if self._is_monitor(child_pid, created):
                    continue  # DrainTracer's own branch; see contains_monitor.
                # A child older than its parent means the PID was reused.
                if (
                    parent_created is not None
                    and created is not None
                    and created < parent_created
                ):
                    continue
                if gone is not None and not _orphan_of(gone, holder, created):
                    continue
                previous = self._departed.get(child_pid)
                if previous is not None and (
                    created is None
                    or previous.created is None
                    or abs(created - previous.created) <= _SAME_START
                ):
                    continue  # the exited member itself, still listed
                self._departed.pop(child_pid, None)  # a new process on an old PID
                members.add(child_pid)
                if created is not None:
                    self._identities[child_pid] = created
                self._depths[child_pid] = depth + 1
                self._parents[child_pid] = parent_pid
                queue.append(child_pid)
        return members

    def _linux_session_members(self, parents: dict[int, int]) -> set[int]:
        """Recover launched descendants that Linux has reparented to init.

        Session IDs remain reserved while any member lives. If the leader's
        PID has since been reused, the old session is gone: never adopt a
        different program's new session. Attached programs keep their own
        sessions, which may include unrelated processes, so only launches
        use this fallback.
        """
        session_id = self._linux_session_id
        if session_id is None:
            return set()
        if _reused_since(session_id, self._root_created) is not None:
            self._linux_session_id = None
            return set()
        members: set[int] = set()
        for pid, parent_pid in parents.items():
            try:
                if os.getsid(pid) != session_id:
                    continue
            except OSError:
                continue  # exited or inaccessible between enumeration and lookup
            proc = self._open_tracked(pid)
            if proc is None:
                continue
            created = self._identities.get(pid)
            if (
                self._root_created is not None
                and created is not None
                and created < self._root_created - _SAME_START
            ):
                continue
            members.add(pid)
            self._parents.setdefault(pid, None if pid == self._root_pid else parent_pid)
            self._depths.setdefault(pid, 0 if pid == self._root_pid else 1)
        return members

    def _refuse_to_end_monitor(self) -> bool:
        """Leave a tree that contains DrainTracer running; see contains_monitor."""
        if not self.contains_monitor:
            return False
        self._last_error = "DrainTracer runs inside this process tree, so it was left running"
        return True

    def _is_monitor(self, pid: int, created: float | None) -> bool:
        """Whether ``pid`` is DrainTracer itself or a process it runs inside."""
        if pid not in self._monitor:
            return False
        expected = self._monitor[pid]
        return expected is None or created is None or abs(created - expected) <= _SAME_START

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
            self._suspended_pids.discard(pid)
            return None

        # POSIX zombies still have a PID. Windows liveness is checked below
        # using a process handle, avoiding a system-wide status enumeration.
        if sys.platform != "win32":
            try:
                if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                    self._suspended_pids.discard(pid)
                    return None
            except psutil.AccessDenied:
                pass
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                self._suspended_pids.discard(pid)
                return None

        expected = self._identities.get(pid)
        if expected is None and pid == self._root_pid:
            expected = self._root_created
        try:
            created = proc.create_time()
        except psutil.AccessDenied:
            return proc
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            self._suspended_pids.discard(pid)
            return None

        if expected is not None and abs(created - expected) > _SAME_START:
            # Retain the old identity until refresh removes it from the
            # tree. A second open must never adopt this replacement PID.
            self._suspended_pids.discard(pid)
            self._last_error = f"PID {pid} was reused; the new process was left untouched"
            return None
        if sys.platform == "win32":
            try:
                if _windows_process_exited(pid):
                    self._suspended_pids.discard(pid)
                    return None
            except OSError:
                # An unreadable exit state must not silently drop a live PID.
                pass
        if expected is None:
            self._identities[pid] = created
        return proc

    def _any_tracked_alive(self) -> bool:
        return any(self._pid_is_alive(pid) for pid in self._tracked_pids)

    def _pid_is_alive(self, pid: int) -> bool:
        return self._open_tracked(pid) is not None

    def _wait_for_pids(
        self, pids: set[int], timeout: float, *, kill_new: bool = False
    ) -> set[int]:
        deadline = time.monotonic() + max(timeout, 0.0)
        remaining = set(pids)
        signaled = set(pids)
        while True:
            # A SIGTERM handler can spawn a helper before it exits. Keep
            # discovering children while cleanup runs, even after the root
            # has gone; never clear tracking for a still-running helper.
            self._refresh_tracked_pids()
            remaining = {pid for pid in remaining if self._pid_is_alive(pid)}
            remaining.update(self._tracked_pids)
            if kill_new:
                for pid in self._termination_order(remaining - signaled):
                    self._terminate_pid(pid, graceful=False)
                signaled.update(remaining)
            if not remaining or time.monotonic() >= deadline:
                return {pid for pid in remaining if self._pid_is_alive(pid)}
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _suspend_pid(self, pid: int) -> bool:
        proc = self._open_tracked(pid)
        if proc is None:
            return False
        try:
            if sys.platform.startswith("linux") and proc.status() in {
                psutil.STATUS_STOPPED, psutil.STATUS_TRACING_STOP
            }:
                # SIGSTOP has no suspension count. Leave an existing stop
                # owned by the user/debugger out of our later SIGCONT pass.
                self._suspended_pids.discard(pid)
                return True
            proc.suspend()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as exc:
            logger.debug("Could not suspend pid %s: %s", pid, exc)
            return False

    def _resume_pid(self, pid: int) -> bool:
        proc = self._open_tracked(pid)
        if proc is None:
            return True  # an exited or reused process no longer needs resuming
        try:
            proc.resume()
            return True
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return True
        except psutil.AccessDenied as exc:
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
