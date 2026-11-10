"""Bounded target output is redacted before any bytes reach disk."""

import io
import unittest
from pathlib import Path

import helpers  # noqa: F401

from src.workload_logs import MAX_LINE_BYTES, BoundedLog


class LogTests(unittest.TestCase):
    def test_secrets_split_across_chunks_are_filtered_before_writing(self) -> None:
        log = BoundedLog(io.BytesIO(), Path("unused"), 1024, ("split-secret",))
        output = io.BytesIO()
        log._file = output
        for chunk in (b"token=abc", b"def normal split-", b"secret ", b"caf\xc3", b"\xa9\n"):
            log._consume(chunk)
        text = output.getvalue().decode("utf-8")
        self.assertEqual(text, "token=<redacted> normal <redacted> café\n")
        self.assertNotIn("abcdef", text)
        self.assertNotIn("split-secret", text)

    def test_no_partial_secret_line_is_kept_at_a_byte_or_line_limit(self) -> None:
        for limit, chunks in ((12, [b"safe\n", b"private secret that will not fit\n"]),
                              (1024, [b"a" * MAX_LINE_BYTES, b"private-secret", b"\n"])):
            with self.subTest(limit=limit):
                log = BoundedLog(io.BytesIO(), Path("unused"), limit, ())
                log._file = output = io.BytesIO()
                for chunk in chunks:
                    log._consume(chunk)
                self.assertLessEqual(len(output.getvalue()), limit)
                self.assertNotIn(b"private", output.getvalue())
                self.assertTrue(log.truncated)
                self.assertLessEqual(len(log._pending), MAX_LINE_BYTES)

    def test_an_overlong_line_is_omitted_without_ending_capture(self) -> None:
        log = BoundedLog(io.BytesIO(), Path("unused"), 1024 * 1024, ())
        log._file = output = io.BytesIO()
        for chunk in (b"before\n", b"x" * (MAX_LINE_BYTES + 100) + b"\n", b"after-1\n", b"after-2\n"):
            log._consume(chunk)
        self.assertEqual(output.getvalue(), b"before\nafter-1\nafter-2\n")
        self.assertEqual(log.omitted_lines, 1)
        self.assertTrue(log.truncated)

    def test_the_byte_bound_still_stops_later_lines(self) -> None:
        log = BoundedLog(io.BytesIO(), Path("unused"), 10, ())
        log._file = output = io.BytesIO()
        for chunk in (b"one\n", b"too long for the rest\n", b"two\n"):
            log._consume(chunk)
        self.assertEqual(output.getvalue(), b"one\n")
        self.assertTrue(log.truncated)
