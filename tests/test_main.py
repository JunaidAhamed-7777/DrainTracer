"""Command-line parsing and dispatch."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from helpers import make_console

from src import main as cli
from src.events import LeakSeverity


def _parse_error(argv: list[str]) -> str:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), _expect_usage_error():
        cli._main(make_console(), argv)
    return stderr.getvalue()


@contextlib.contextmanager
def _expect_usage_error():
    try:
        yield
    except SystemExit as exc:
        if exc.code != 2:
            raise AssertionError(f"expected a usage error, got exit {exc.code}") from exc
    else:
        raise AssertionError("expected a usage error")


class ParserTests(unittest.TestCase):
    def test_time_limit_accepts_durations(self) -> None:
        args = cli.build_parser().parse_args(["--timeout", "1m30s"])
        self.assertEqual(args.timeout, 90.0)

    def test_invalid_combinations_are_usage_errors(self) -> None:
        cases = {
            ("--headless",): "--headless needs --target or --pid",
            ("--fail-on", "low", "--target", "x.exe"): "--fail-on only applies with --headless",
            ("--timeout", "0", "--target", "x.exe"): "greater than zero",
            ("--timeout", "soon", "--target", "x.exe"): "expected a duration",
            ("--pid", "1", "--", "arg"): "only apply to launched targets",
            ("--headless", "--target", "x.exe", "--fail-on", "none"): "invalid choice",
        }
        for argv, message in cases.items():
            with self.subTest(argv=argv):
                self.assertIn(message, _parse_error(list(argv)))


class DispatchTests(unittest.TestCase):
    def test_headless_builds_a_runner_from_flags(self) -> None:
        with mock.patch.object(cli, "HeadlessRunner") as runner:
            runner.return_value.run.return_value = 1
            code = cli._main(
                make_console(),
                [
                    "--headless", "--target", "app.exe", "--timeout", "2m",
                    "--fail-on", "medium", "--report-dir", "out", "--", "--fast",
                ],
            )
        self.assertEqual(code, 1)
        _args, kwargs = runner.call_args
        self.assertEqual(runner.call_args.args[0], "app.exe")
        self.assertEqual(kwargs["args"], ["--fast"])
        self.assertEqual(kwargs["timeout"], 120.0)
        self.assertEqual(kwargs["fail_on"], LeakSeverity.MEDIUM)
        self.assertEqual(kwargs["report_dir"], "out")
        self.assertIsNone(kwargs["attach_pid"])

    def test_headless_defaults_to_failing_on_high(self) -> None:
        with mock.patch.object(cli, "HeadlessRunner") as runner:
            runner.return_value.run.return_value = 0
            cli._main(make_console(), ["--headless", "--pid", "4242"])
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["fail_on"], LeakSeverity.HIGH)
        self.assertEqual(kwargs["attach_pid"], 4242)

    def test_ctrl_c_exits_130(self) -> None:
        with mock.patch.object(cli, "_main", side_effect=KeyboardInterrupt), \
                mock.patch.object(cli, "Console", return_value=make_console()):
            self.assertEqual(cli.main([]), 130)


if __name__ == "__main__":
    unittest.main()
