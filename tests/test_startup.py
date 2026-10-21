"""Startup choices and keyboard input handling."""

from __future__ import annotations

import unittest
from unittest import mock

from helpers import make_console

from src.startup import choose_startup_action, prompt_process_id


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


class ProcessIdTests(unittest.TestCase):
    def test_valid_pid_and_attachment_guidance(self) -> None:
        console = make_console()
        with mock.patch("builtins.input", return_value=" 4242 "):
            self.assertEqual(prompt_process_id(console), 4242)
        output = console.file.getvalue()
        self.assertIn("Task Manager > Details", output)
        self.assertIn("leaves the program running", output)
        self.assertIn("Q to cancel", output)

    def test_invalid_pid_retries(self) -> None:
        console = make_console()
        with mock.patch("builtins.input", side_effect=["", "name.exe", "0", "-1", "1.5", "42"]) as read:
            self.assertEqual(prompt_process_id(console), 42)
        self.assertEqual(read.call_count, 6)
        self.assertEqual(console.file.getvalue().count("Enter a positive process ID"), 5)

    def test_cancel_and_eof_do_not_select_a_target(self) -> None:
        for answer in ("q", " Q ", EOFError()):
            with (
                self.subTest(answer=answer),
                mock.patch("builtins.input", side_effect=[answer]),
            ):
                self.assertIsNone(prompt_process_id(make_console()))

    def test_ctrl_c_propagates_to_the_entry_point(self) -> None:
        with (
            mock.patch("builtins.input", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            prompt_process_id(make_console())


if __name__ == "__main__":
    unittest.main()
