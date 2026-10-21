"""Native picker-key decoding without requiring a real terminal."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import make_console

from src import picker_input
from src.picker_input import PickerKey, PickerKeyboard


def keyboard(*characters: str) -> PickerKeyboard:
    return PickerKeyboard(mock.Mock(side_effect=characters))


class PickerKeyboardTests(unittest.TestCase):
    def test_both_windows_arrow_prefixes_and_enter(self) -> None:
        keys = keyboard("\x00", "H", "\xe0", "P", "\r")
        self.assertEqual([keys.read(), keys.read(), keys.read()],
                         [PickerKey("up"), PickerKey("down"), PickerKey("enter")])

    def test_printable_text_is_not_a_navigation_token(self) -> None:
        keys = keyboard("u", "p", "4", "2", "日", "\x08", "\x7f", "\x1b")
        self.assertEqual([keys.read() for _ in range(5)],
                         [PickerKey("text", value) for value in "up42日"])
        self.assertEqual([keys.read(), keys.read(), keys.read()],
                         [PickerKey("backspace"), PickerKey("backspace"), PickerKey("escape")])

    def test_unsupported_extended_keys_and_controls_are_consumed(self) -> None:
        keys = keyboard("\xe0", "K", "\x00", "M", "\t", "x")
        self.assertEqual(keys.read(), PickerKey("text", "x"))

    def test_leading_lf_and_crlf_do_not_create_duplicate_selections(self) -> None:
        keys = keyboard("\n", "4", "\r", "\n", "\xe0", "P", "\r")
        self.assertEqual([keys.read() for _ in range(4)],
                         [PickerKey("text", "4"), PickerKey("enter"),
                          PickerKey("down"), PickerKey("enter")])

    def test_ctrl_c_and_end_of_input_propagate(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            keyboard("\x03").read()
        for characters in (("",), ("\x04",), ("\x1a",), ("\xe0", "")):
            with self.subTest(characters=characters), self.assertRaises(EOFError):
                keyboard(*characters).read()

    def test_native_input_requires_windows_and_both_terminal_streams(self) -> None:
        console = SimpleNamespace(is_terminal=True)
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        with (
            mock.patch.object(picker_input.os, "name", "nt"),
            mock.patch.object(picker_input.sys, "stdin", stdin),
        ):
            self.assertTrue(picker_input.supports_picker_keyboard(console))
            self.assertFalse(picker_input.supports_picker_keyboard(make_console()))
            stdin.isatty.return_value = False
            self.assertFalse(picker_input.supports_picker_keyboard(console))
            stdin.isatty.side_effect = OSError("input unavailable")
            self.assertFalse(picker_input.supports_picker_keyboard(console))
        with mock.patch.object(picker_input.os, "name", "posix"):
            self.assertFalse(picker_input.supports_picker_keyboard(console))


if __name__ == "__main__":
    unittest.main()
