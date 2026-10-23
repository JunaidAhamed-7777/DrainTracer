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
from src import process_picker
from src.events import LeakSeverity
from src.running_processes import RunningProcess


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

    def test_picker_failures_suggest_target_and_cancel_exits_cleanly(self) -> None:
        failure = OSError("All Windows file picker implementations failed")
        failure.__cause__ = RuntimeError("no display")
        console = make_console()
        with (
            mock.patch.object(cli, "choose_startup_action", return_value="open"),
            mock.patch.object(cli, "select_target_file", side_effect=failure),
        ):
            self.assertEqual(cli._main(console, ["--no-config"]), 1)
        output = console.file.getvalue()
        self.assertIn("implementations failed: no display", output)
        self.assertIn("--target PATH", output)

        console = make_console()
        with (
            mock.patch.object(cli, "choose_startup_action", return_value="open"),
            mock.patch.object(cli, "select_target_file", return_value=None),
        ):
            self.assertEqual(cli._main(console, ["--no-config"]), 0)
        self.assertIn("Selection canceled", console.file.getvalue())

    def test_ctrl_c_exits_130(self) -> None:
        with mock.patch.object(cli, "_main", side_effect=KeyboardInterrupt), \
                mock.patch.object(cli, "Console", return_value=make_console()):
            self.assertEqual(cli.main([]), 130)



class StartupDispatchTests(unittest.TestCase):
    def test_title_shows_each_update_result_below_the_version(self) -> None:
        from rich.console import Console

        for status, expected in (
            (True, "New Version Available! Update using pip install --upgrade draintracer"),
            (False, "No new updates. Contribute at github.com/JunaidAhamed-7777/DrainTracer"),
            (None, "Update check unavailable. Contribute at github.com/JunaidAhamed-7777/DrainTracer"),
        ):
            with self.subTest(status=status):
                output = io.StringIO()
                console = Console(file=output, width=140, force_terminal=True, color_system=None)
                with mock.patch.object(cli, "check_for_update", return_value=status) as check:
                    cli._display_title(console)
                check.assert_called_once_with(cli.DashboardApp.VERSION)
                lines = output.getvalue().splitlines()
                subtitle = next(i for i, line in enumerate(lines) if "Resource leak monitor" in line)
                self.assertIn(cli.DashboardApp.VERSION, lines[subtitle])
                self.assertEqual(lines[subtitle + 1], expected)
                if status is not True:
                    self.assertNotIn("New Version Available!", output.getvalue())

    def test_update_notice_colors_match_the_requested_labels(self) -> None:
        notice = cli._update_notice(True)
        self.assertEqual(notice.spans[0].style, "red")
        self.assertEqual(notice.spans[1].style, "green")
        self.assertTrue(str(cli._update_notice(False).spans[0].style).startswith("blue"))

    def test_headless_help_version_and_redirected_output_do_not_check_updates(self) -> None:
        with (
            mock.patch.object(cli, "check_for_update") as check,
            mock.patch.object(cli, "HeadlessRunner") as runner,
            mock.patch.object(cli, "_run_dashboard", return_value=0),
        ):
            runner.return_value.run.return_value = 0
            self.assertEqual(cli._main(make_console(), ["--headless", "--pid", "4242", "--no-config"]), 0)
            self.assertEqual(cli._main(make_console(), ["--pid", "4242", "--no-config"]), 0)
            for flag in ("--help", "--version"):
                with self.subTest(flag=flag), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_:
                        cli._main(make_console(), [flag])
                    self.assertEqual(exit_.exception.code, 0)
        check.assert_not_called()

    def test_open_uses_the_file_picker_and_preserves_target_arguments(self) -> None:
        console = make_console()
        with (
            mock.patch("builtins.input", return_value="1"),
            mock.patch.object(cli, "select_target_file", return_value="app.exe") as picker,
            mock.patch.object(cli, "DashboardApp") as app,
        ):
            app.return_value.run.return_value = 0
            code = cli._main(console, ["--no-config", "--", "--fast", "two words"])
        self.assertEqual(code, 0)
        picker.assert_called_once_with()
        self.assertEqual(app.call_args.args, ("app.exe",))
        self.assertEqual(app.call_args.kwargs["args"], ["--fast", "two words"])
        self.assertNotIn("attach_pid", app.call_args.kwargs)

    def test_hook_uses_the_pid_and_preserves_settings_without_a_file_picker(self) -> None:
        console = make_console()
        with (
            mock.patch("builtins.input", side_effect=["2", "EDITOR", "4242"]),
            mock.patch.object(process_picker, "list_running_processes", return_value=[
                RunningProcess(4242, "editor.exe", 10.0),
                RunningProcess(52, "worker.exe", 12.0),
            ]),
            mock.patch.object(process_picker, "is_process_current", return_value=True),
            mock.patch.object(cli, "select_target_file") as picker,
            mock.patch.object(cli, "DashboardApp") as app,
        ):
            app.return_value.run.return_value = 0
            code = cli._main(console, ["--no-config", "--timeout", "2m", "--report-dir", "out"])
        self.assertEqual(code, 0)
        picker.assert_not_called()
        self.assertEqual(app.call_args.args, (None,))
        kwargs = app.call_args.kwargs
        self.assertEqual(kwargs["attach_pid"], 4242)
        self.assertEqual(kwargs["timeout"], 120.0)
        self.assertEqual(kwargs["report_dir"], "out")
        self.assertIs(kwargs["console"], console)
        self.assertIsNone(kwargs["settings"].source)
        self.assertIn("Search: EDITOR", console.file.getvalue())
        self.assertIn("Hooking to editor.exe (PID 4242)", console.file.getvalue())

    def test_cancellation_or_eof_never_constructs_a_session(self) -> None:
        for answers in (["q"], ["2", "q"], [EOFError()], ["2", EOFError()]):
            with (
                self.subTest(answers=answers),
                mock.patch("builtins.input", side_effect=answers),
                mock.patch.object(process_picker, "list_running_processes", return_value=[]),
                mock.patch.object(cli, "select_target_file") as picker,
                mock.patch.object(cli, "DashboardApp") as app,
            ):
                console = make_console()
                self.assertEqual(cli._main(console, ["--no-config"]), 0)
                picker.assert_not_called()
                app.assert_not_called()
                self.assertIn("Selection canceled", console.file.getvalue())

    def test_hook_rejects_launch_arguments_before_requesting_a_pid(self) -> None:
        with (
            mock.patch("builtins.input", return_value="2") as read,
            mock.patch.object(cli, "DashboardApp") as app,
        ):
            error = _parse_error(["--no-config", "--", "--fast"])
        self.assertIn("only apply to launched targets", error)
        read.assert_called_once_with()
        app.assert_not_called()

    def test_explicit_targets_and_headless_bypass_the_process_picker(self) -> None:
        with (
            mock.patch.object(cli, "select_running_process", side_effect=AssertionError("unexpected picker")) as picker,
            mock.patch.object(cli, "DashboardApp") as dashboard,
            mock.patch.object(cli, "HeadlessRunner") as headless,
        ):
            dashboard.return_value.run.return_value = 0
            headless.return_value.run.return_value = 0
            for arguments in (["--target", "app.exe"], ["--pid", "4242"],
                              ["--headless", "--pid", "4242"]):
                with self.subTest(arguments=arguments):
                    self.assertEqual(cli._main(make_console(), [*arguments, "--no-config"]), 0)
        picker.assert_not_called()


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


class PolicyFlagTests(unittest.TestCase):
    def test_policy_flags_reach_the_runner(self) -> None:
        with mock.patch.object(cli, "HeadlessRunner") as runner:
            runner.return_value.run.return_value = 0
            cli._main(make_console(), ["--headless", "--target", "a.exe", "--no-config",
                                       "--fail-on-inconclusive", "--fail-on-target-error"])
        kwargs = runner.call_args.kwargs
        self.assertTrue(kwargs["fail_on_inconclusive"])
        self.assertTrue(kwargs["fail_on_target_error"])

    def test_policy_flags_need_headless(self) -> None:
        for flag in ("--fail-on-inconclusive", "--fail-on-target-error"):
            with self.subTest(flag=flag):
                message = _parse_error([flag, "--target", "x.exe"])
                self.assertIn(f"{flag} only applies with --headless", message)



class ExperimentFlagTests(unittest.TestCase):
    def test_experiment_labels_reach_the_settings(self) -> None:
        with mock.patch.object(cli, "HeadlessRunner") as runner:
            runner.return_value.run.return_value = 0
            cli._main(make_console(), [
                "--headless", "--target", "a.exe", "--no-config", "--workload-id", "import-1k",
                "--build", "1.4.2", "--operations", "1000", "--note", "nightly",
            ])
        experiment = runner.call_args.kwargs["settings"].experiment
        self.assertEqual(
            (experiment.workload_id, experiment.build, experiment.operations, experiment.note),
            ("import-1k", "1.4.2", 1000.0, "nightly"),
        )

    def test_budget_flags_reach_the_settings_and_need_headless(self) -> None:
        with mock.patch.object(cli, "HeadlessRunner") as runner:
            runner.return_value.run.return_value = 0
            cli._main(make_console(), ["--headless", "--target", "a.exe", "--no-config",
                                       "--budget-private", "500MB", "--budget-threads", "40"])
        budgets = dict(runner.call_args.kwargs["settings"].budgets)
        self.assertEqual(budgets, {"private_bytes": 500 * 1024 * 1024, "threads": 40.0})
        message = _parse_error(["--budget-handles", "2000", "--target", "x.exe"])
        self.assertIn("--budget-handles only applies with --headless", message)
        self.assertIn("greater than zero", _parse_error(["--budget-handles", "0"]))

    def test_operations_must_be_positive(self) -> None:
        self.assertIn("greater than zero", _parse_error(["--operations", "0", "--target", "x"]))
        self.assertIn("non-negative", _parse_error(["--operations", "-4", "--target", "x"]))


if __name__ == "__main__":
    unittest.main()
