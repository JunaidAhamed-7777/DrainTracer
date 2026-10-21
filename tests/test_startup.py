"""Startup choices and keyboard input handling."""

from __future__ import annotations

import unittest
from unittest import mock

from helpers import make_console

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


if __name__ == "__main__":
    unittest.main()
