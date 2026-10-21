"""Process search, paged presentation, and keyboard selection."""

from __future__ import annotations

import unittest
from unittest import mock

from helpers import make_console

from src import process_picker as picker
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


class ProcessSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [RunningProcess(42, "Editor.exe", 1.0), RunningProcess(142, "editor.exe", 2.0)]
        self.console = make_console()
        scan_patch = mock.patch.object(picker, "list_running_processes", return_value=self.rows)
        current_patch = mock.patch.object(picker, "is_process_current", return_value=True)
        self.scan = scan_patch.start()
        self.addCleanup(scan_patch.stop)
        self.current = current_patch.start()
        self.addCleanup(current_patch.stop)

    def choose(self, *answers):
        with mock.patch("builtins.input", side_effect=answers):
            return picker.select_running_process(self.console)

    def test_search_then_exact_pid_selects_the_right_duplicate_name(self) -> None:
        self.assertEqual(self.choose("EDITOR", "142"), 142)
        self.current.assert_called_once_with(self.rows[1])
        self.assertIn("Hooking to editor.exe (PID 142)", self.console.file.getvalue())

    def test_numeric_search_and_unlisted_pid_do_not_attach_implicitly(self) -> None:
        self.assertEqual(self.choose("/search 42", "99", "42"), 42)
        self.current.assert_called_once_with(self.rows[0])
        self.assertIn("not in these results", self.console.file.getvalue())

    def test_no_matches_clear_and_single_result_still_require_selection(self) -> None:
        self.assertIsNone(self.choose("missing", "/clear", "/search 142", "", "q"))
        self.current.assert_not_called()
        self.assertIn("No programs match", self.console.file.getvalue())

    def test_paging_and_refresh_keep_search_and_reset_page(self) -> None:
        self.scan.side_effect = [
            [RunningProcess(pid, "worker.exe", 1.0) for pid in range(1, 13)],
            [RunningProcess(42, "worker.exe", 2.0)],
        ]
        self.assertEqual(self.choose("worker", "/next", "/prev", "/next", "/refresh", "42"), 42)
        output = self.console.file.getvalue()
        self.assertIn("Page 2/2", output)
        self.assertIn("Search: worker", output)
        self.assertIn("Showing 1-1 of 1 | Page 1/1", output)

    def test_unavailable_inventory_can_be_retried(self) -> None:
        self.scan.side_effect = [OSError("denied"), self.rows]
        self.assertEqual(self.choose("/refresh", "42"), 42)
        output = self.console.file.getvalue()
        self.assertIn("Process list unavailable", output)
        self.assertNotIn("No running programs are available", output)

    def test_exited_or_changed_selection_refreshes_without_accepting_it(self) -> None:
        self.current.side_effect = [False, True]
        self.scan.side_effect = [self.rows, [RunningProcess(142, "editor.exe", 3.0)]]
        self.assertEqual(self.choose("42", "142"), 142)
        self.assertEqual(self.scan.call_count, 2)
        self.assertIn("exited, changed, or cannot be verified", self.console.file.getvalue())

    def test_unknown_identity_cannot_be_silently_selected(self) -> None:
        self.scan.return_value = [RunningProcess(42, "Restricted.exe", None)]
        self.assertIsNone(self.choose("42", "q"))
        self.current.assert_not_called()
        self.assertIn("identity is unavailable", self.console.file.getvalue())

    def test_cancel_eof_and_unknown_command_never_select(self) -> None:
        for answer in ("Q", "/quit", EOFError()):
            with self.subTest(answer=answer):
                self.assertIsNone(self.choose("/invalid", answer))
        self.current.assert_not_called()

    def test_ctrl_c_propagates(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.choose(KeyboardInterrupt())


if __name__ == "__main__":
    unittest.main()
