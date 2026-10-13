"""Duration parsing and formatting."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401  (adds the repository root to sys.path)

from src.units import (
    format_bytes,
    format_clock,
    format_duration,
    parse_count,
    parse_duration,
    parse_rate,
    parse_size,
)

KB, MB, GB = 1024, 1024**2, 1024**3


class ParseSizeTests(unittest.TestCase):
    def test_accepted_forms(self) -> None:
        cases = {
            "512": 512.0,
            "512b": 512.0,
            "50MB": 50 * MB,
            "50 MiB": 50 * MB,
            "1.5gb": 1.5 * GB,
            "64k": 64 * KB,
            ".5 TB": 0.5 * 1024**4,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_size(text), expected)
        self.assertEqual(parse_size(2048), 2048.0)

    def test_rejected_forms(self) -> None:
        for value in ("", "MB", "-5MB", "5 parsecs", "5MB/s", True, None, float("nan"), -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_size(value)  # type: ignore[arg-type]


class ParseRateTests(unittest.TestCase):
    def test_memory_rates(self) -> None:
        self.assertEqual(parse_rate("5MB/s", parse_size), 5 * MB)
        self.assertEqual(parse_rate("300MB/min", parse_size), 5 * MB)
        self.assertEqual(parse_rate("36000 MB / h", parse_size), 10 * MB)
        self.assertEqual(parse_rate("5MB", parse_size), 5 * MB)  # per second by default
        self.assertEqual(parse_rate(1024, parse_size), 1024.0)

    def test_count_rates(self) -> None:
        self.assertEqual(parse_rate("20/s", parse_count), 20.0)
        self.assertEqual(parse_rate("120/min", parse_count), 2.0)
        self.assertEqual(parse_rate(2.5, parse_count), 2.5)

    def test_rejected_rates(self) -> None:
        for value in ("5MB/fortnight", "/s", "x/s"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_rate(value, parse_size)


class ParseCountTests(unittest.TestCase):
    def test_counts(self) -> None:
        self.assertEqual(parse_count(500), 500.0)
        self.assertEqual(parse_count("12.5"), 12.5)
        for value in ("many", "-1", True, float("inf"), [], "1e400"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_count(value)  # type: ignore[arg-type]


class ParseDurationTests(unittest.TestCase):
    def test_accepted_forms(self) -> None:
        cases = {
            "90": 90.0,
            "90s": 90.0,
            " 2m ": 120.0,
            "1m30s": 90.0,
            "1h 5m": 3900.0,
            "1.5h": 5400.0,
            ".5s": 0.5,
            "250ms": 0.25,
            "10 seconds": 10.0,
            "2 MIN": 120.0,
            "0s": 0.0,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertAlmostEqual(parse_duration(text), expected)

    def test_numbers_are_seconds(self) -> None:
        self.assertEqual(parse_duration(45), 45.0)
        self.assertEqual(parse_duration(2.5), 2.5)

    def test_rejected_forms(self) -> None:
        for value in ("", "  ", "abc", "-5s", "5x", "1m30", "1e3", "5 s s", "inf", "nan"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_duration(value)

    def test_rejected_types_and_values(self) -> None:
        for value in (True, None, [], -1, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_duration(value)  # type: ignore[arg-type]


class FormatDurationTests(unittest.TestCase):
    def test_compact_forms(self) -> None:
        cases = {
            0.0: "0s",
            -3.0: "0s",
            0.25: "250ms",
            1.0: "1s",
            10.5: "10.5s",
            59.96: "1m",
            90.0: "1m30s",
            3600.0: "1h",
            3900.0: "1h5m",
            7261.0: "2h1m1s",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)

    def test_round_trip(self) -> None:
        for seconds in (0.25, 5.0, 90.0, 3900.0):
            self.assertAlmostEqual(parse_duration(format_duration(seconds)), seconds)


class FormatClockAndBytesTests(unittest.TestCase):
    def test_clock(self) -> None:
        self.assertEqual(format_clock(0), "00:00:00")
        self.assertEqual(format_clock(3725.9), "01:02:05")
        self.assertEqual(format_clock(90061), "1d 01:01:01")
        self.assertEqual(format_clock(-5), "00:00:00")
        self.assertEqual(format_clock(float("nan")), "00:00:00")

    def test_bytes(self) -> None:
        self.assertEqual(format_bytes(512), "512.0 B")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(5 * 1024**3), "5.0 GB")
        self.assertEqual(format_bytes(3 * 1024**5), "3,072.0 TB")
        self.assertEqual(format_bytes(-10), "0.0 B")
        self.assertEqual(format_bytes(float("inf")), "0.0 B")


if __name__ == "__main__":
    unittest.main()
