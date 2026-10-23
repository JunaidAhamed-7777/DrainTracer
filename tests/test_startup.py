"""Startup choices and keyboard input handling."""

from __future__ import annotations

import unittest
from unittest import mock

from helpers import make_console

from src import startup
from src.picker_input import PickerKeyboard
from src.startup import choose_startup_action


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
    def choose(self, keys: str) -> str | None:
        keyboard = PickerKeyboard(iter(keys).__next__)
        with (
            mock.patch.object(startup, "supports_picker_keyboard", return_value=True),
            mock.patch.object(startup, "PickerKeyboard", return_value=keyboard),
            mock.patch("builtins.input", side_effect=AssertionError("unexpected line input")),
        ):
            return choose_startup_action(make_console())

    def test_space_selects_the_highlighted_action_without_enter(self) -> None:
        for keys, expected in ((" ", "open"), ("\xe0P ", "hook"), ("\x00P\x00H ", "open")):
            with self.subTest(keys=keys):
                self.assertEqual(self.choose(keys), expected)

    def test_arrow_navigation_stays_within_the_choices(self) -> None:
        self.assertEqual(self.choose("\xe0H "), "open")
        self.assertEqual(self.choose("\xe0P\xe0P "), "hook")

    def test_enter_also_selects_and_other_keys_do_not(self) -> None:
        self.assertEqual(self.choose("12x\x08\xe0P\r"), "hook")

    def test_quit_escape_and_eof_cancel_without_selecting(self) -> None:
        for keys in ("q", "Q", "\x1b", "\x1a", "\x04", "\xe0P\x1b"):
            with self.subTest(keys=keys):
                self.assertIsNone(self.choose(keys))

    def test_ctrl_c_propagates(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.choose("\x03")

    def test_highlight_and_hint_render_in_narrow_and_legacy_terminals(self) -> None:
        for width, encoding in ((80, "utf-8"), (35, "cp1252")):
            with self.subTest(width=width, encoding=encoding):
                console = make_console(width=width, encoding=encoding)
                console.print(startup._startup_view(1))
                output = console.file.getvalue()
                self.assertIn(">  Hook", output)
                self.assertIn("Up/Down: move", output)
                self.assertNotIn("Choose 1 or 2", output)
                self.assertLess(output.rfind("\u2570"), output.index("Up/Down"))


if __name__ == "__main__":
    unittest.main()
