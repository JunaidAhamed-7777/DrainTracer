"""Process search, paged presentation, and keyboard selection."""

from __future__ import annotations

import unittest
from unittest import mock

from helpers import make_console

from src import process_picker as picker
from src.picker_input import PickerKey
from src.process_picker import _render_page, filter_processes
from src.running_processes import RunningProcess


class ProcessPresentationTests(unittest.TestCase):
    def test_live_query_handles_name_pid_and_explicit_search_edits(self) -> None:
        for value, expected in ((" Ed ", "Ed"), ("42", "42"), ("", ""),
                                ("/search 42", "42"), ("/SEARCH editor", "editor")):
            with self.subTest(value=value):
                self.assertEqual(picker._live_query(value, "old"), expected)

    def test_partial_commands_keep_the_active_filter(self) -> None:
        for value in ("/", "/r", "/refresh", "/next", "/prev", "/clear", "/search"):
            with self.subTest(value=value):
                self.assertEqual(picker._live_query(value, "editor"), "editor")

    def test_name_and_pid_search_preserve_distinct_processes(self) -> None:
        rows = [RunningProcess(42, "Editor.exe", 1.0), RunningProcess(142, "editor.exe", 2.0),
                RunningProcess(43, "worker.exe", 3.0)]
        self.assertEqual(filter_processes(rows, " EDITOR "), rows[:2])
        self.assertEqual(filter_processes(rows, "42"), rows[:2])
        self.assertEqual(filter_processes(rows, ""), rows)
        self.assertEqual(filter_processes(rows, "missing"), [])

    def test_pages_are_bounded_and_clamped(self) -> None:
        rows = [RunningProcess(pid, f"app{pid}.exe", 1.0) for pid in range(1, 18)]
        console = make_console()
        self.assertEqual(_render_page(console, rows, "", 1), 1)
        output = console.file.getvalue()
        self.assertIn("app16.exe", output)
        self.assertNotIn("app1.exe", output)
        self.assertIn("Showing 16-17 of 17 | Page 2/2", output)
        self.assertEqual(_render_page(make_console(), rows, "", 99), 1)
        self.assertEqual(_render_page(make_console(), rows, "", -1), 0)

    def test_page_shows_at_most_15_pids_with_a_visible_active_row(self) -> None:
        rows = [RunningProcess(pid, f"app{pid}.exe", 1.0) for pid in range(1, 32)]
        console = make_console()
        _render_page(console, rows, "", 0, selected=1)
        output = console.file.getvalue()
        self.assertEqual(sum(f"app{pid}.exe" in output for pid in range(1, 32)), 15)
        self.assertIn("app15.exe", output)
        self.assertNotIn("app16.exe", output)
        self.assertTrue(any(">" in line and "app2.exe" in line for line in output.splitlines()))

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
            [RunningProcess(pid, "worker.exe", 1.0) for pid in range(1, 18)],
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

    def test_tiny_terminal_keeps_a_selectable_row_above_the_prompt(self) -> None:
        self.console = make_console(width=24, height=10)
        self.assertIsNone(self.choose("Editor", "q"))
        last_screen = self.console.file.getvalue().split("RUNNING PROGRAMS")[-1]
        self.assertTrue(any("42" in line for line in last_screen.splitlines()[-10:]))
        self.assertIn("/next /prev /refresh", last_screen)


class KeyboardSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [RunningProcess(42, "Editor.exe", 1.0), RunningProcess(142, "editor.exe", 2.0)]
        self.console = make_console()
        scan = mock.patch.object(picker, "list_running_processes", return_value=self.rows)
        current = mock.patch.object(picker, "is_process_current", return_value=True)
        self.scan = scan.start()
        self.addCleanup(scan.stop)
        self.current = current.start()
        self.addCleanup(current.stop)

    def choose(self, *keys):
        keyboard = mock.Mock()
        keyboard.read.side_effect = keys
        with (
            mock.patch.object(picker, "supports_picker_keyboard", return_value=True),
            mock.patch.object(picker, "PickerKeyboard", return_value=keyboard),
            mock.patch.object(picker, "Live") as display,
        ):
            self.display = display
            return picker.select_running_process(self.console)

    def frames(self) -> str:
        console = make_console()
        for call in self.display.return_value.__enter__.return_value.update.call_args_list:
            console.print(call.args[0])
        return console.file.getvalue()

    def test_arrows_cross_the_15_row_boundary_and_enter_selects(self) -> None:
        self.scan.return_value = [RunningProcess(pid, f"app{pid}.exe", 1.0) for pid in range(1, 32)]
        down = [PickerKey("down")] * 15
        self.assertEqual(self.choose(*down, PickerKey("enter")), 16)
        self.assertIn("Page 2/3", self.frames())
        self.assertEqual(self.choose(*down, PickerKey("up"), PickerKey("enter")), 15)

    def test_navigation_clamps_at_both_ends(self) -> None:
        self.assertEqual(self.choose(PickerKey("up"), PickerKey("enter")), 42)
        self.assertEqual(self.choose(*[PickerKey("down")] * 4, PickerKey("enter")), 142)

    def test_typed_pid_and_backspace_override_the_highlight(self) -> None:
        keys = [PickerKey("text", value) for value in "143"]
        self.assertEqual(self.choose(*keys, PickerKey("backspace"), PickerKey("text", "2"),
                                     PickerKey("enter")), 142)
        self.current.assert_called_once_with(self.rows[1])

    def test_arrows_clear_unsubmitted_input_before_enter(self) -> None:
        self.assertEqual(self.choose(PickerKey("text", "999"), PickerKey("down"),
                                     PickerKey("enter")), 142)

    def test_empty_search_results_do_not_select_a_process(self) -> None:
        self.assertIsNone(self.choose(PickerKey("text", "missing"), PickerKey("enter"),
                                      PickerKey("enter"), PickerKey("escape")))
        self.current.assert_not_called()
        self.assertIn("No program to select", self.frames())

    def test_refresh_shrinking_inventory_resets_the_active_row(self) -> None:
        self.scan.side_effect = [self.rows, [self.rows[0]]]
        self.assertEqual(self.choose(PickerKey("down"), PickerKey("text", "/refresh"),
                                     PickerKey("enter"), PickerKey("enter")), 42)
        self.current.assert_called_once_with(self.rows[0])

    def test_enter_uses_the_same_stale_identity_checks_as_pid_entry(self) -> None:
        self.current.side_effect = [False, True]
        self.scan.side_effect = [self.rows, [self.rows[1]]]
        self.assertEqual(self.choose(PickerKey("enter"), PickerKey("enter")), 142)
        self.assertEqual(self.current.call_args_list,
                         [mock.call(self.rows[0]), mock.call(self.rows[1])])

    def test_escape_eof_and_ctrl_c_leave_the_live_display(self) -> None:
        for key in (PickerKey("escape"), EOFError()):
            with self.subTest(key=key):
                self.assertIsNone(self.choose(key))
                self.display.return_value.__exit__.assert_called_once()
        with self.assertRaises(KeyboardInterrupt):
            self.choose(KeyboardInterrupt())
        self.display.return_value.__exit__.assert_called_once()

    def test_tiny_keyboard_frames_keep_errors_long_input_and_prompt_within_height(self) -> None:
        self.console = make_console(width=24, height=10)
        self.assertIsNone(self.choose(PickerKey("text", "999"), PickerKey("enter"),
                                      PickerKey("text", "pasted search " * 50), PickerKey("escape")))
        frames = self.display.return_value.__enter__.return_value.update.call_args_list
        for call in frames:
            with self.subTest(frame=call):
                lines = self.console.render_lines(call.args[0], self.console.options, pad=False)
                self.assertLessEqual(len(lines), self.console.height)


if __name__ == "__main__":
    unittest.main()
