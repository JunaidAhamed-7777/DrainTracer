"""Command-line parsing and dispatch."""

from __future__ import annotations

import contextlib
import io
import logging
import tempfile
import unittest
from pathlib import Path
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
            ("--profile", "ci", "--no-config"): "--profile needs a settings file",
            ("--config", "a.toml", "--no-config"): "not allowed with argument",
            ("--interval", "10ms"): "between 100ms and 1m",
            ("--window", "2h"): "at most 1h",
            ("--warmup", "2d"): "unknown duration unit",
            ("--warmup", "30h"): "at most 24h",
            tuple(arg for seconds in range(10, 19) for arg in ("--window", f"{seconds}s")):
                "at most 8 different --window values",
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
                    "--fail-on", "medium", "--report-dir", "out", "--no-config",
                    "--interval", "250ms", "--window", "30s", "--window", "5s",
                    "--warmup", "20s", "--", "--fast",
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
        settings = kwargs["settings"]
        self.assertEqual(settings.sample_interval_ms, 250)
        self.assertEqual(settings.windows, (5.0, 30.0))
        self.assertEqual(settings.warmup, 20.0)
        self.assertIsNone(settings.source)

    def test_headless_defaults_to_failing_on_high(self) -> None:
        with mock.patch.object(cli, "HeadlessRunner") as runner:
            runner.return_value.run.return_value = 0
            cli._main(make_console(), ["--headless", "--pid", "4242", "--no-config"])
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["fail_on"], LeakSeverity.HIGH)
        self.assertEqual(kwargs["attach_pid"], 4242)

    def test_settings_file_profile_and_flags_layer_in_order(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "draintracer.toml"
            config.write_text(
                'timeout = "1m"\nfail_on = "low"\nreport_dir = "from-file"\n'
                '[profiles.ci]\ntimeout = "2m"\n',
                encoding="utf-8",
            )
            with mock.patch.object(cli, "HeadlessRunner") as runner:
                runner.return_value.run.return_value = 0
                cli._main(
                    make_console(),
                    ["--headless", "--target", "a.exe", "--config", str(config),
                     "--profile", "ci", "--fail-on", "critical"],
                )
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 120.0)  # profile over the base
        self.assertEqual(kwargs["fail_on"], LeakSeverity.CRITICAL)  # flag over the file
        self.assertEqual(Path(kwargs["report_dir"]), config.parent / "from-file")
        self.assertEqual(kwargs["settings"].profile, "ci")

    def test_settings_errors_exit_2_before_anything_starts(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / ".drainrc"
            config.write_text("timout = 5\n", encoding="utf-8")
            console = make_console()
            with mock.patch.object(cli, "HeadlessRunner") as runner:
                code = cli._main(
                    console, ["--headless", "--target", "a.exe", "--config", str(config)]
                )
        self.assertEqual(code, 2)
        runner.assert_not_called()
        output = console.file.getvalue()
        self.assertIn("Settings error:", output)
        self.assertIn("unknown setting timout (did you mean timeout?)", output)

    def test_a_time_limit_inside_the_warmup_is_a_settings_error(self) -> None:
        console = make_console()
        code = cli._main(
            console,
            ["--headless", "--target", "a.exe", "--no-config", "--warmup", "1m", "--timeout", "30s"],
        )
        self.assertEqual(code, 2)
        self.assertIn("ends before the 1m warm-up does", console.file.getvalue())

    def test_process_errors_print_paths_literally(self) -> None:
        from src.process_runner import ProcessManagerError

        console = make_console()
        app = mock.Mock()
        app.run.side_effect = ProcessManagerError("cannot run C:\\x\\app[red].exe")
        self.assertEqual(cli._run_dashboard(app, console), 1)
        self.assertIn("cannot run C:\\x\\app[red].exe", console.file.getvalue())

    def test_output_survives_unencodable_names(self) -> None:
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch.object(cli.sys, "stdout", stream), \
                mock.patch.object(cli.sys, "stderr", None):
            cli._make_output_safe()
            stream.write("launching тест_日本.py")
            stream.flush()
        self.assertEqual(stream.buffer.getvalue(), b"launching ????_??.py")

    def test_unexpected_errors_are_reported_with_the_mode_exit_code(self) -> None:
        console = make_console()
        app = mock.Mock()
        app.run.side_effect = RuntimeError("render bug")
        self.assertEqual(cli._run_dashboard(app, console), 1)
        output = console.file.getvalue()
        self.assertIn("render bug", output)
        self.assertIn("stopped after an unexpected error", output)

        with mock.patch.object(cli, "HeadlessRunner") as runner:
            runner.return_value.run.side_effect = RuntimeError("export bug")
            code = cli._main(make_console(), ["--headless", "--target", "a.exe", "--no-config"])
        self.assertEqual(code, 2)

    def test_dashboard_warns_when_output_is_not_a_terminal(self) -> None:
        console = make_console()
        with mock.patch.object(cli, "_run_dashboard", return_value=0) as run:
            self.assertEqual(cli._main(console, ["--target", "a.exe", "--no-config"]), 0)
        run.assert_called_once()
        self.assertIn("Use --headless for logs and CI", console.file.getvalue())

    def test_ctrl_c_exits_130(self) -> None:
        with mock.patch.object(cli, "_main", side_effect=KeyboardInterrupt), \
                mock.patch.object(cli, "Console", return_value=make_console()):
            self.assertEqual(cli.main([]), 130)



class LoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        saved = (root.handlers[:], root.level)
        root.handlers.clear()

        def restore() -> None:
            for handler in root.handlers:
                handler.close()
            root.handlers[:] = saved[0]
            root.setLevel(saved[1])

        self.addCleanup(restore)

    def test_diagnostics_stay_off_the_terminal(self) -> None:
        stderr = io.StringIO()
        with mock.patch.dict(cli.os.environ, {"DRAINTRACER_LOG": ""}),                 contextlib.redirect_stderr(stderr):
            handler = cli._configure_logging()
            logging.getLogger("src.leak_detector").error("Resource sampling failed")
        self.assertIsInstance(handler, logging.NullHandler)
        self.assertEqual(stderr.getvalue(), "")
        # Logging configured by an embedding program is left alone.
        self.assertIsNone(cli._configure_logging())
        self.assertEqual(logging.getLogger().handlers, [handler])

    def test_drain_log_records_tracebacks_in_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "drain.log"
            with mock.patch.dict(cli.os.environ, {"DRAINTRACER_LOG": str(log)}):
                handler = cli._configure_logging()
            try:
                raise ValueError("layout bug")
            except ValueError:
                logging.getLogger("src.dashboard").exception("Drawing the dashboard failed")
            handler.close()
            text = log.read_text(encoding="utf-8")

            logging.getLogger().handlers.clear()
            stderr = io.StringIO()
            missing = Path(temp) / "missing" / "drain.log"
            with mock.patch.dict(cli.os.environ, {"DRAINTRACER_LOG": str(missing)}),                     contextlib.redirect_stderr(stderr):
                fallback = cli._configure_logging()
        self.assertIn("ERROR MainThread src.dashboard: Drawing the dashboard failed", text)
        self.assertIn("ValueError: layout bug", text)
        self.assertIsInstance(fallback, logging.NullHandler)
        self.assertIn("DRAINTRACER_LOG: cannot write", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
