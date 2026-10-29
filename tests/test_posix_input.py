"""Exercise immediate Linux terminal input using real pseudo-terminals."""

from __future__ import annotations

import io
import os
import select
import unittest
from unittest import mock

import helpers  # noqa: F401 (adds the repository root to sys.path)

from src import keys
from src.picker_input import PickerKey, PickerKeyboard


@unittest.skipUnless(os.name == "posix", "requires POSIX pseudo-terminals")
class PosixInputTests(unittest.TestCase):
    def setUp(self) -> None:
        import pty
        import termios

        self.termios = termios
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.stream = os.fdopen(os.dup(self.slave), encoding="utf-8")
        self.addCleanup(self.stream.close)
        self.saved = termios.tcgetattr(self.slave)
        patch = mock.patch.object(keys.sys, "stdin", self.stream)
        patch.start()
        self.addCleanup(patch.stop)

    def send(self, value: bytes) -> None:
        os.write(self.master, value)
        self.assertTrue(select.select([self.stream], [], [], 1)[0], "terminal input not delivered")

    def test_immediate_no_echo_input_and_restore_after_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "handler failed"):
            with keys.KeyReader() as reader:
                attributes = self.termios.tcgetattr(self.slave)
                self.assertFalse(attributes[3] & self.termios.ICANON)
                self.assertFalse(attributes[3] & self.termios.ECHO)
                self.assertTrue(attributes[3] & self.termios.ISIG)
                self.assertIsNone(reader.read())
                self.send(b"q")
                self.assertEqual(reader.read(), "q")
                raise RuntimeError("handler failed")
        self.assertEqual(self.termios.tcgetattr(self.slave), self.saved)

    def test_fragmented_arrows_and_unicode_are_buffered(self) -> None:
        with keys.KeyReader() as reader:
            for fragment in (b"\x1b", b"[1;"):
                self.send(fragment)
                self.assertIsNone(reader.read())
            self.send(b"5A")
            self.assertEqual(reader.read(), keys.UP)
            value = "日".encode("utf-8")
            self.send(value[:2])
            self.assertIsNone(reader.read())
            self.send(value[2:])
            self.assertEqual(reader.read(), "日")

    def test_escape_resolves_after_short_sequence_timeout(self) -> None:
        with keys.KeyReader() as reader, mock.patch.object(keys.time, "monotonic", return_value=100.0):
            self.send(b"\x1b")
            self.assertIsNone(reader.read())
            with mock.patch.object(keys.time, "monotonic", return_value=100.06):
                self.assertEqual(reader.read(), keys.ESCAPE)

    def test_delayed_sequence_tail_never_becomes_a_dashboard_hotkey(self) -> None:
        with keys.KeyReader() as reader, mock.patch.object(keys.time, "monotonic", return_value=100.0):
            self.send(b"\x1b[")
            self.assertIsNone(reader.read())
            with mock.patch.object(keys.time, "monotonic", return_value=101.0):
                self.assertIsNone(reader.read())
                self.send(b"H")
                self.assertEqual(reader.read(), keys.HOME)

    def test_picker_reads_arrows_text_and_eof_without_enter(self) -> None:
        with PickerKeyboard() as keyboard:
            self.send("\x1b[B日\x7f\x04".encode("utf-8"))
            self.assertEqual(keyboard.read(), PickerKey("down"))
            self.assertEqual(keyboard.read(), PickerKey("text", "日"))
            self.assertEqual(keyboard.read(), PickerKey("backspace"))
            with self.assertRaises(EOFError):
                keyboard.read()
        self.assertEqual(self.termios.tcgetattr(self.slave), self.saved)

    def test_redirected_input_does_not_change_terminal_mode(self) -> None:
        with mock.patch.object(keys.sys, "stdin", io.StringIO("q")):
            with keys.KeyReader() as reader:
                self.assertIsNone(reader.read())
        self.assertEqual(self.termios.tcgetattr(self.slave), self.saved)


if __name__ == "__main__":
    unittest.main()
