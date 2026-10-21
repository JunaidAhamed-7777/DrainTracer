"""Decoding of Windows console and ANSI key sequences into key names."""

from __future__ import annotations

import unittest
from unittest import mock

import helpers  # noqa: F401  (puts the repository on sys.path)

from src import keys
from src.keys import KeyReader, decode_ansi, decode_windows, normalize_key


def _windows(*characters: str) -> list[str | None]:
    """Decode a stream of ``getwch`` characters the way ``KeyReader`` does."""
    stream = iter(characters)
    decoded = []
    for first in stream:
        decoded.append(decode_windows(first, lambda: next(stream, None)))
    return decoded


class WindowsDecodingTests(unittest.TestCase):
    def test_both_prefixes_decode_every_navigation_scan_code(self) -> None:
        expected = {
            "H": keys.UP,
            "P": keys.DOWN,
            "K": keys.LEFT,
            "M": keys.RIGHT,
            "G": keys.HOME,
            "O": keys.END,
            "I": keys.PAGE_UP,
            "Q": keys.PAGE_DOWN,
        }
        for prefix in ("\x00", "\xe0"):
            for code, name in expected.items():
                with self.subTest(prefix=repr(prefix), code=code):
                    self.assertEqual(_windows(prefix, code), [name])

    def test_scan_codes_never_leak_as_letters(self) -> None:
        # Up then "h": the H scan code must not toggle help.
        self.assertEqual(_windows("\xe0", "H", "h"), [keys.UP, "h"])
        # An unknown extended key (F1 is \x00 ;) is consumed and ignored.
        self.assertEqual(_windows("\x00", ";", "q"), [None, "q"])

    def test_control_characters_have_names(self) -> None:
        self.assertEqual(
            _windows("\r", "\t", "\x1b", "\x08", " ", "\x00", "\x0f"),
            [keys.ENTER, keys.TAB, keys.ESCAPE, keys.BACKSPACE, " ", keys.BACKTAB],
        )

    def test_a_prefix_without_scan_code_is_dropped(self) -> None:
        self.assertIsNone(decode_windows("\xe0", lambda: None))


class AnsiDecodingTests(unittest.TestCase):
    def test_cursor_and_paging_sequences(self) -> None:
        self.assertEqual(
            decode_ansi("\x1b[A\x1b[B\x1b[C\x1b[D\x1bOH\x1bOF\x1b[5~\x1b[6~\x1b[Z"),
            [keys.UP, keys.DOWN, keys.RIGHT, keys.LEFT, keys.HOME, keys.END,
             keys.PAGE_UP, keys.PAGE_DOWN, keys.BACKTAB],
        )

    def test_modifiers_and_tilde_forms(self) -> None:
        self.assertEqual(decode_ansi("\x1b[1;5A\x1b[1~\x1b[4~\x1b[3~"),
                         [keys.UP, keys.HOME, keys.END, keys.DELETE])

    def test_lone_escape_and_text_mix(self) -> None:
        self.assertEqual(decode_ansi("\x1b"), [keys.ESCAPE])
        self.assertEqual(decode_ansi("a\x1b\x1b[Bq\r"), ["a", keys.ESCAPE, keys.DOWN, "q", keys.ENTER])

    def test_unknown_and_truncated_sequences_are_skipped(self) -> None:
        self.assertEqual(decode_ansi("\x1b[15~x"), ["x"])  # F5
        self.assertEqual(decode_ansi("x\x1b[1;"), ["x"])


class ReaderTests(unittest.TestCase):
    def test_posix_reader_queues_several_keys_from_one_read(self) -> None:
        reader = KeyReader()
        with mock.patch.object(keys.os, "name", "posix"), \
                mock.patch.object(KeyReader, "_read_posix", return_value=[keys.UP, "q"]) as read:
            self.assertEqual(reader.read(), keys.UP)
            self.assertEqual(reader.read(), "q")
            self.assertEqual(read.call_count, 1)

    def test_windows_reader_never_blocks_on_a_lone_prefix_character(self) -> None:
        # "à" arrives as "\xe0" with no scan code behind it.
        console = mock.Mock()
        console.kbhit.side_effect = [True, False]
        console.getwch.side_effect = ["\xe0"]
        with mock.patch.dict("sys.modules", {"msvcrt": console}):
            self.assertIsNone(KeyReader._read_windows())
        self.assertEqual(console.getwch.call_count, 1)
        console = mock.Mock()
        console.kbhit.side_effect = [True, True]
        console.getwch.side_effect = ["\xe0", "H"]
        with mock.patch.dict("sys.modules", {"msvcrt": console}):
            self.assertEqual(KeyReader._read_windows(), keys.UP)

    def test_normalize_names_raw_control_characters(self) -> None:
        self.assertEqual(normalize_key("\x1b"), keys.ESCAPE)
        self.assertEqual(normalize_key("\r"), keys.ENTER)
        self.assertEqual(normalize_key("k"), "k")
        self.assertEqual(normalize_key(keys.UP), keys.UP)
        self.assertTrue(all(len(name) > 1 for name in keys.NAMED_KEYS))


if __name__ == "__main__":
    unittest.main()
