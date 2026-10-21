"""Process search, paged presentation, and keyboard selection."""

from __future__ import annotations

import unittest

from helpers import make_console

from src.process_picker import _render_page, filter_processes
from src.running_processes import RunningProcess


class ProcessPresentationTests(unittest.TestCase):
    def test_name_and_pid_search_preserve_distinct_processes(self) -> None:
        rows = [RunningProcess(42, "Editor.exe", 1.0), RunningProcess(142, "editor.exe", 2.0),
                RunningProcess(43, "worker.exe", 3.0)]
        self.assertEqual(filter_processes(rows, " EDITOR "), rows[:2])
        self.assertEqual(filter_processes(rows, "42"), rows[:2])
        self.assertEqual(filter_processes(rows, ""), rows)
        self.assertEqual(filter_processes(rows, "missing"), [])

    def test_pages_are_bounded_and_clamped(self) -> None:
        rows = [RunningProcess(pid, f"app{pid}.exe", 1.0) for pid in range(1, 13)]
        console = make_console()
        self.assertEqual(_render_page(console, rows, "", 1), 1)
        output = console.file.getvalue()
        self.assertIn("app11.exe", output)
        self.assertNotIn("app1.exe", output)
        self.assertIn("Showing 11-12 of 12 | Page 2/2", output)
        self.assertEqual(_render_page(make_console(), rows, "", 99), 1)
        self.assertEqual(_render_page(make_console(), rows, "", -1), 0)

    def test_short_terminals_keep_pid_and_name_columns(self) -> None:
        console = make_console(width=32, height=15, encoding="cp1252")
        rows = [RunningProcess(42, "editor[red].exe\n\x1b", 1.0)]
        _render_page(console, rows, "[red]", 0)
        output = console.file.getvalue()
        self.assertIn("42", output)
        self.assertIn("editor[red].exe", output)
        self.assertNotIn("\x1b", output)
        output.encode("cp1252")
        self.assertTrue(all(len(line) <= 32 for line in output.splitlines()))

    def test_no_results_are_actionable_and_unknown_identity_is_labeled(self) -> None:
        console = make_console()
        _render_page(console, [], "missing", 0)
        self.assertIn("No programs match", console.file.getvalue())
        self.assertIn("/clear or /refresh", console.file.getvalue())
        _render_page(console, [RunningProcess(42, "Restricted.exe", None)], "", 0)
        self.assertIn("identity unavailable", console.file.getvalue())


if __name__ == "__main__":
    unittest.main()
