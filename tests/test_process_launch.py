"""Launching targets: command lines for scripts, batch files, and executables."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401  (adds the repository root to sys.path)

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


if __name__ == "__main__":
    unittest.main()
