"""Startup choices and keyboard input handling."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from dataclasses import replace
from unittest import mock

from helpers import make_console

from src import startup
from src.running_processes import RunningProcess
from src.startup import choose_startup_action
from src.system_overview import RankedProcess, SystemOverview


def _overview() -> SystemOverview:
    def rows(prefix: str, largest: int) -> tuple[RankedProcess, ...]:
        return tuple(
            RankedProcess(RunningProcess(101 + i, f"{prefix}-{i + 1}", 1.0), largest >> i)
            for i in range(5)
        )

    return SystemOverview(
        22.5, 4 * 1024**3, 16 * 1024**3,
        rows("cpu", 160), rows("ram", 2 * 1024**3), rows("fd", 850), rows("thread", 50),
        "Open FDs",
    )


class StartupChoiceTests(unittest.TestCase):
    def test_choices_explain_launch_and_attachment(self) -> None:
        for answer, expected in (("1", "open"), (" 2 ", "hook")):
            with self.subTest(answer=answer):
                console = make_console()
                with mock.patch("builtins.input", return_value=answer):
                    self.assertEqual(choose_startup_action(console), expected)
                output = console.file.getvalue()
                self.assertIn("Open a program", output)
                self.assertIn("Hook to a running program", output)
                self.assertIn("Q to quit", output)

    def test_invalid_and_empty_choices_retry_without_a_default(self) -> None:
        console = make_console()
        with mock.patch("builtins.input", side_effect=["", "3", "open", "1"]) as read:
            self.assertEqual(choose_startup_action(console), "open")
        self.assertEqual(read.call_count, 4)
        self.assertEqual(console.file.getvalue().count("Enter 1, 2, or Q."), 3)

    def test_quit_and_eof_cancel(self) -> None:
        for answer in ("q", " Q ", EOFError()):
            with (
                self.subTest(answer=answer),
                mock.patch("builtins.input", side_effect=[answer]),
            ):
                self.assertIsNone(choose_startup_action(make_console()))

    def test_ctrl_c_propagates_to_the_entry_point(self) -> None:
        with (
            mock.patch("builtins.input", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            choose_startup_action(make_console())


class StartupKeyboardTests(unittest.TestCase):
    def choose(self, *keys: str) -> str | None:
        reader = mock.Mock(eof=False)
        reader.read.side_effect = [*keys, "\x04"]
        with (
            mock.patch.object(startup, "KeyReader", return_value=nullcontext(reader)),
            mock.patch.object(startup, "_collect_overviews"),
            mock.patch.object(startup.time, "sleep"),
        ):
            return startup._choose_live(make_console(), 5)

    def test_space_selects_the_highlighted_action_without_enter(self) -> None:
        for keys, expected in (((" ",), "open"), (("down", " "), "hook"), (("down", "up", " "), "open")):
            with self.subTest(keys=keys):
                self.assertEqual(self.choose(*keys), expected)

    def test_arrow_navigation_stays_within_the_choices(self) -> None:
        self.assertEqual(self.choose("up", " "), "open")
        self.assertEqual(self.choose("down", "down", " "), "hook")

    def test_shortcuts_and_enter_select_an_action(self) -> None:
        self.assertEqual(self.choose("1"), "open")
        self.assertEqual(self.choose("2"), "hook")
        self.assertEqual(self.choose("x", "backspace", "right", "down", "enter"), "hook")

    def test_quit_escape_and_eof_cancel_without_selecting(self) -> None:
        self.assertIsNone(self.choose())
        for key in ("q", "Q", "escape", "\x1a", "\x04"):
            with self.subTest(key=key):
                self.assertIsNone(self.choose("down", key))

    def test_ctrl_c_propagates(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.choose("\x03")

    def test_hold_keeps_the_display_still_and_resume_jumps_to_the_latest_reading(self) -> None:
        samples = [_overview(), replace(_overview(), cpu_percent=78.0)]
        keys = iter((None, "p", None, "p", "q"))
        reader = mock.Mock(eof=False)
        with (
            mock.patch.object(startup, "Thread") as worker,
            mock.patch.object(startup, "KeyReader", return_value=nullcontext(reader)),
            mock.patch.object(startup, "_startup_view", wraps=startup._startup_view) as views,
            mock.patch.object(startup.time, "sleep"),
        ):
            def read_key():
                key = next(keys)
                if key is None:
                    worker.call_args.kwargs["args"][1].append(samples.pop(0))
                return key

            reader.read.side_effect = read_key
            startup._choose_live(make_console(), 5)
        frames = [(call.args[1].cpu_percent, call.kwargs["held"])
                  for call in views.call_args_list if call.args[1] is not None]
        self.assertEqual(frames, [(22.5, False), (22.5, True), (78.0, False)])

    def test_highlight_and_hint_render_in_narrow_and_legacy_terminals(self) -> None:
        for width, encoding in ((80, "utf-8"), (35, "cp1252")):
            with self.subTest(width=width, encoding=encoding):
                console = make_console(width=width, encoding=encoding)
                view, _ = startup._startup_view(console, None, 1, 0)
                console.print(view)
                output = console.file.getvalue()
                self.assertIn("> [2] Hook", output)
                self.assertIn("Up/Down", output)
                self.assertNotIn("Choose 1 or 2", output)
                self.assertIn("P hold", output)

    def test_overview_displays_host_values_and_all_four_rankings(self) -> None:
        console = make_console(width=80, height=24)
        view, _ = startup._startup_view(console, _overview(), 0, 0)
        console.print(view)
        output = console.file.getvalue()
        self.assertIn("22.5%", output)
        self.assertIn("4.0 GB / 16.0 GB (25.0%)", output)
        for metric in ("CPU (% of one core)", "Memory (RSS)", "Open FDs", "Threads"):
            self.assertIn(metric, output)
        for name in ("cpu", "ram", "fd", "thread"):
            self.assertLess(output.index(f"{name}-1"), output.index(f"{name}-5"))
        self.assertLessEqual(len(output.splitlines()), console.height)

    def test_compact_overview_pages_all_rankings_together(self) -> None:
        console = make_console(width=35, height=24, encoding="cp1252")
        view, page = startup._startup_view(console, _overview(), 1, 99)
        console.print(view)
        output = console.file.getvalue()
        self.assertEqual(page, 1)
        self.assertIn("2/2 Left/Right", output)
        self.assertIn("> [2] Hook", output)
        for name in ("cpu", "ram", "fd", "thread"):
            self.assertIn(f"{name}-5", output)
            self.assertNotIn(f"{name}-1", output)
        self.assertLessEqual(len(output.splitlines()), console.height)

    def test_truncated_rankings_are_encodable_on_legacy_consoles(self) -> None:
        row = RankedProcess(RunningProcess(123, "worker-" * 12 + "\u754c", 1.0), 400)
        snapshot = replace(_overview(), top_threads=(row,))
        for encoding in ("cp437", "ascii"):
            with self.subTest(encoding=encoding):
                console = make_console(35, 24, encoding)
                view, _ = startup._startup_view(console, snapshot, 0, 0)
                console.print(view)
                self.assertIn(b"worker-", console.file.getvalue().encode(encoding))


if __name__ == "__main__":
    unittest.main()
