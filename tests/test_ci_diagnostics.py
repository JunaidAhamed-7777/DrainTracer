"""CI command logging preserves failures while keeping artifact files bounded."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / ".github" / "run_check.py"


class CheckLoggingTests(unittest.TestCase):
    def _run(self, directory: str, script: str):
        return subprocess.run(
            [sys.executable, str(WRAPPER), "--directory", directory, "fixture",
             sys.executable, "-c", script],
            check=False, capture_output=True, timeout=15,
        )

    def test_failure_exit_status_and_both_output_streams_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(directory, "import sys; print('stdout'); "
                               "print('stderr', file=sys.stderr); sys.exit(23)")
            self.assertEqual(result.returncode, 23)
            data = (Path(directory) / "fixture.log").read_bytes()
            self.assertIn(b"stdout", data)
            self.assertIn(b"stderr", data)
            self.assertEqual(result.stdout, data)

    def test_long_output_keeps_a_bounded_tail_and_reports_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(directory, "print('x' * (1024 * 1024)); print('final evidence')")
            self.assertEqual(result.returncode, 0)
            data = (Path(directory) / "fixture.log").read_bytes()
            self.assertLessEqual(len(data), 256 * 1024 + 100)
            self.assertIn(b"earlier diagnostic bytes omitted", data)
            self.assertTrue(data.rstrip().endswith(b"final evidence"))

    def test_missing_command_is_an_explicit_failed_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(WRAPPER), "--directory", directory, "missing",
                 str(Path(directory) / "nonexistent-command")],
                check=False, capture_output=True, timeout=15,
            )
            self.assertEqual(result.returncode, 2)
            self.assertTrue((Path(directory) / "missing.log").read_bytes())


if __name__ == "__main__":
    unittest.main()
