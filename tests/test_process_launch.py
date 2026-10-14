"""Launching targets: command lines for scripts, batch files, and executables."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401  (adds the repository root to sys.path)
import psutil

from src.process_runner import ProcessManager, _quote_for_cmd


class QuoteTests(unittest.TestCase):
    def test_plain_arguments_stay_bare(self) -> None:
        self.assertEqual(_quote_for_cmd("plain"), "plain")
        self.assertEqual(_quote_for_cmd("--port=8080"), '"--port=8080"')  # = splits batch args

    def test_special_arguments_are_quoted(self) -> None:
        cases = {
            "": '""',
            "has space": '"has space"',
            "x&y": '"x&y"',
            "a|b": '"a|b"',
            "(p)": '"(p)"',
            'say "hi"': '"say ""hi"""',
        }
        for argument, quoted in cases.items():
            with self.subTest(argument=argument):
                self.assertEqual(_quote_for_cmd(argument), quoted)


class StdinTests(unittest.TestCase):
    def test_targets_read_end_of_file_instead_of_the_console(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "stdin.txt"
            script = Path(temp) / "reader.py"
            script.write_text(
                "import sys\n"
                f"open({str(out)!r}, 'w').write(repr(sys.stdin.read()))\n",
                encoding="utf-8",
            )
            manager = ProcessManager(str(script))
            manager.start()
            deadline = time.monotonic() + 10
            while manager.is_running and time.monotonic() < deadline:
                time.sleep(0.05)
            still_running = manager.is_running
            manager.kill()
            self.assertFalse(still_running, "the target blocked reading stdin")
            self.assertEqual(out.read_text(encoding="utf-8"), "''")


@unittest.skipUnless(sys.platform == "win32", "batch files run through cmd.exe on Windows")
class BatchLaunchTests(unittest.TestCase):
    def test_batch_files_receive_hostile_arguments_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "dir with spaces"
            folder.mkdir()
            script = folder / "my script.bat"
            script.write_text(
                '@echo off\r\n> "%~dp0out.txt" echo [%1] [%2] [%3] [%4]\r\nexit /b 7\r\n',
                encoding="ascii",
            )
            manager = ProcessManager(str(script), args=["has space", "x&y", "a|b>c", "(p)"])
            manager.start()
            deadline = time.monotonic() + 10
            while manager.is_running and time.monotonic() < deadline:
                time.sleep(0.05)
            output = (folder / "out.txt").read_text(encoding="ascii").strip()
            exit_code = manager.exit_code
            manager.kill()
        self.assertEqual(output, '["has space"] ["x&y"] ["a|b>c"] ["(p)"]')
        self.assertEqual(exit_code, 7)

    def test_python_and_executable_targets_use_argument_lists(self) -> None:
        manager = ProcessManager("C:/apps/tool.py", args=["a b"])
        self.assertEqual(manager._build_command()[1:], [str(Path("C:/apps/tool.py").resolve()), "a b"])
        exe = ProcessManager("C:/apps/tool.exe", args=["x&y"])
        self.assertEqual(exe._build_command(), [str(Path("C:/apps/tool.exe").resolve()), "x&y"])


# The base interpreter, not a virtual environment's redirector, so each
# Python process below is exactly one process.
_PYTHON = getattr(sys, "_base_executable", None) or sys.executable


def _wait(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


@unittest.skipUnless(sys.platform == "win32", "Windows does not re-parent orphans")
class OrphanTests(unittest.TestCase):
    def _hand_off(self, folder: Path, worker: str) -> ProcessManager:
        """Launch a batch file that starts ``worker`` and exits at once."""
        script = folder / "handoff.bat"
        script.write_text(f'@echo off\r\nstart "" /b "{_PYTHON}" -c "{worker}"\r\n', encoding="utf-8")
        # No background refreshes: the first walk happens after the root exited.
        manager = ProcessManager(str(script), poll_interval=60)
        manager.start()
        self.addCleanup(manager.kill)
        self.assertTrue(_wait(lambda: manager.exit_code is not None), "the batch file never exited")
        return manager

    @staticmethod
    def _pythons(pids) -> list[psutil.Process]:
        processes = [psutil.Process(pid) for pid in pids]
        return [proc for proc in processes if proc.name().lower().startswith("python")]

    def test_a_process_left_by_an_exited_root_is_tracked_and_ended(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manager = self._hand_off(Path(temp), "import time; time.sleep(60)")
            workers = self._pythons(manager.refresh_process_tree())
            self.assertEqual(len(workers), 1)
            manager.terminate()
            self.assertFalse(manager.has_tracked_processes)
            self.assertTrue(_wait(lambda: not workers[0].is_running()))

    def test_children_started_after_the_root_exited_are_tracked_and_ended(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pid_file = Path(temp) / "grandchild.txt"
            worker = (
                "import subprocess, sys, time; time.sleep(1); "
                "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open(r'{pid_file}', 'w').write(str(g.pid)); time.sleep(60)"
            )
            manager = self._hand_off(Path(temp), worker)
            self.assertEqual(len(self._pythons(manager.refresh_process_tree())), 1)
            self.assertTrue(_wait(lambda: pid_file.exists() and pid_file.read_text().strip()))
            grandchild = int(pid_file.read_text())
            self.assertIn(grandchild, manager.refresh_process_tree())
            manager.terminate()
            self.assertTrue(_wait(lambda: not psutil.pid_exists(grandchild)))


if __name__ == "__main__":
    unittest.main()
