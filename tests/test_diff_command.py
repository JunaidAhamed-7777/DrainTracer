"""The diff command: rendering, JSON output, exit codes, and dispatch."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import make_console
from test_report_diff import _experiment, _run

from src import main as cli
from src.diff_command import (
    EXIT_ERROR,
    EXIT_NOT_COMPARABLE,
    EXIT_OK,
    EXIT_OVER_BUDGET,
    EXIT_REGRESSION,
    build_diff_parser,
    run_diff,
)
from src.events import LeakSeverity


class DiffCommandTests(unittest.TestCase):
    def test_plain_diff_has_text_labels_and_no_terminal_controls(self) -> None:
        from rich.console import Console

        output = io.StringIO()
        console = Console(file=output, force_terminal=True, color_system="truecolor", width=40)
        code = run_diff([str(self.leaky), str(self.fixed), "--plain"], console)
        self.assertEqual(code, EXIT_OK)
        text = output.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertIn("VERDICT IMPROVED", text)
        self.assertIn("memory: signal HIGH to NONE", text)
        self.assertNotIn("│", text)

    def test_quiet_diff_keeps_blocking_caveats_and_json_takes_precedence(self) -> None:
        report = _run(0.0)
        report["target_path"] = "other:warning:.exe"
        self.fixed.write_text(json.dumps(report), encoding="utf-8")
        code, text = self._run(str(self.leaky), str(self.fixed), "--quiet", "--strict")
        self.assertEqual(code, EXIT_NOT_COMPARABLE)
        self.assertIn("Caveat:", text)
        self.assertIn("VERDICT NOT COMPARABLE", text)
        self.assertNotIn("memory: signal", text)
        code, text = self._run(str(self.leaky), str(self.fixed), "--json", "--plain", "--quiet")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(text)["after"]["target"], "other:warning:.exe")

    def test_no_color_overrides_forced_color_without_losing_verdict_words(self) -> None:
        from rich.ansi import AnsiDecoder
        from rich.console import Console

        output = io.StringIO()
        console = Console(file=output, force_terminal=True, color_system="truecolor", width=100)
        code = run_diff([str(self.leaky), str(self.fixed), "--no-color"], console)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("IMPROVED", output.getvalue())
        for line in AnsiDecoder().decode(output.getvalue()):
            for span in line.spans:
                self.assertIsNone(span.style.color)
                self.assertIsNone(span.style.bgcolor)

    def test_count_budget_flags_reject_fractional_limits(self) -> None:
        for flag in ("--budget-handles", "--budget-threads"):
            with self.subTest(flag=flag):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    build_diff_parser().parse_args(["before.json", "after.json", flag, "2.25"])
                self.assertEqual(caught.exception.code, EXIT_ERROR)
                self.assertIn("whole number", stderr.getvalue())

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.leaky = root / "leaky.json"
        self.fixed = root / "fixed.json"
        self.leaky.write_text(json.dumps(_run(4.0, severity=LeakSeverity.HIGH)), encoding="utf-8")
        self.fixed.write_text(json.dumps(_run(0.0)), encoding="utf-8")
        self.root = root

    def _run(self, *argv: str, width: int = 100, encoding: str = "utf-8") -> tuple[int, str]:
        console = make_console(width=width, encoding=encoding)
        code = run_diff(list(argv), console)
        return code, console.file.getvalue()

    def test_narrow_consoles_get_one_block_per_resource(self) -> None:
        code, output = self._run(str(self.leaky), str(self.fixed), width=80)
        self.assertEqual(code, EXIT_OK)
        self.assertRegex(output, r"MEMORY\s+HIGH → clear\s+resolved")
        self.assertIn("drift  +8.0 MB/s →", output)
        self.assertIn("VERDICT  IMPROVED", output)
        self.assertTrue(all(len(line) <= 80 for line in output.splitlines()))

    def test_wide_consoles_get_a_table(self) -> None:
        _code, output = self._run(str(self.leaky), str(self.fixed), width=140)
        self.assertIn("SIGNAL A → B", output)
        self.assertIn("▼ 100%", output)

    def test_legacy_code_pages_stay_encodable(self) -> None:
        _code, output = self._run(str(self.leaky), str(self.fixed), width=80, encoding="cp1252")
        output.encode("cp1252")
        self.assertIn("HIGH -> clear", output)

    def test_json_output(self) -> None:
        code, output = self._run(str(self.leaky), str(self.fixed), "--json")
        self.assertEqual(code, EXIT_OK)
        data = json.loads(output)
        self.assertEqual(data["verdict"], "improved")
        self.assertEqual(data["resources"]["memory"]["signal"], "resolved")

    def test_regressions_fail_only_when_asked(self) -> None:
        self.assertEqual(self._run(str(self.fixed), str(self.leaky))[0], EXIT_OK)
        code, output = self._run(str(self.fixed), str(self.leaky), "--fail-on-regression")
        self.assertEqual(code, EXIT_REGRESSION)
        self.assertIn("REGRESSED", output)
        improved = self._run(str(self.leaky), str(self.fixed), "--fail-on-regression")[0]
        self.assertEqual(improved, EXIT_OK)

    def _write(self, name: str, report: dict) -> str:
        path = self.root / name
        path.write_text(json.dumps(report), encoding="utf-8")
        return str(path)

    def _lighter_pair(self) -> tuple[str, str]:
        leaky = self._write("heavy.json", _run(
            4.0, severity=LeakSeverity.HIGH, samples=41,
            experiment=_experiment(workload_id="import", build="1.0", operations=1000)))
        light = self._write("light.json", _run(
            0.0, samples=11,
            experiment=_experiment(workload_id="import", build="1.1", operations=100)))
        return leaky, light

    def test_a_lighter_run_shows_a_qualified_verdict_wide_and_narrow(self) -> None:
        leaky, light = self._lighter_pair()
        for width in (80, 140):
            with self.subTest(width=width):
                code, output = self._run(leaky, light, width=width)
                self.assertEqual(code, EXIT_OK)
                self.assertIn("VERDICT  IMPROVED (QUALIFIED)", output)
                self.assertIn("workload import · build 1.1 · 100 ops", output)
                self.assertIn("--strict would report 'not comparable'", output)
                self.assertTrue(all(len(line) <= width for line in output.splitlines()))

    def test_strict_mode_refuses_non_equivalent_runs_with_exit_3(self) -> None:
        leaky, light = self._lighter_pair()
        code, output = self._run(leaky, light, "--strict", "--fail-on-regression", width=80)
        self.assertEqual(code, EXIT_NOT_COMPARABLE)
        self.assertIn("VERDICT  NOT COMPARABLE", output)
        self.assertNotIn("IMPROVED", output)
        code, output = self._run(leaky, light, "--strict", "--json")
        self.assertEqual(code, EXIT_NOT_COMPARABLE)
        data = json.loads(output)
        self.assertEqual((data["verdict"], data["measured_verdict"]), ("not comparable", "improved"))
        # Equivalent runs keep their verdict and exit code under --strict.
        code, output = self._run(str(self.leaky), str(self.fixed), "--strict", width=80)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("VERDICT  IMPROVED  ", output)

    def test_strict_mode_reports_insufficient_coverage(self) -> None:
        unjudged = self._write("unjudged.json", _run(0.0, detection_ran=False))
        code, output = self._run(str(self.leaky), unjudged, "--strict", width=80,
                                 encoding="cp1252")
        self.assertEqual(code, EXIT_NOT_COMPARABLE)
        self.assertIn("INSUFFICIENT COVERAGE", output)
        output.encode("cp1252")

    def test_budgets_peaks_and_per_operation_growth_wide_and_narrow(self) -> None:
        before = self._write("ops_a.json", _run(4.0, severity=LeakSeverity.HIGH,
                                                experiment=_experiment(operations=1000)))
        after = self._write("ops_b.json", _run(0.5, experiment=_experiment(operations=1000)))
        for width in (80, 140):
            with self.subTest(width=width):
                code, output = self._run(before, after, "--budget-private", "100MB",
                                         "--budget-handles", "5000", width=width)
                self.assertEqual(code, EXIT_OVER_BUDGET)
                self.assertIn("B private bytes peak 110.0 MB over the 100.0 MB budget", output)
                self.assertIn("B handles peak 200 within the 5,000 budget", output)
                self.assertIn("180.0 MB → 110.0 MB", output)
                self.assertIn("+81.9 KB → +10.2 KB", output)
                self.assertTrue(all(len(line) <= width for line in output.splitlines()))
        self.assertIn("PEAK A → B", self._run(before, after, width=140)[1])
        code, output = self._run(before, after, "--budget-threads", "2", "--json")
        self.assertEqual(code, EXIT_OVER_BUDGET)
        data = json.loads(output)
        self.assertTrue(data["over_budget"])
        self.assertEqual(data["budgets"][0]["metric"], "threads")
        # Without budgets the same pair passes.
        self.assertEqual(self._run(before, after)[0], EXIT_OK)

    def test_bad_inputs_exit_2(self) -> None:
        code, output = self._run(str(self.leaky), str(self.root / "missing.json"))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("Diff error:", output)
        self.assertIn("no such file", output)

    def test_numbers_beyond_float_range_are_rejected_before_comparison(self) -> None:
        report = json.loads(self.fixed.read_text(encoding="utf-8"))
        report["snapshots"][1]["totals"]["handles"] = 10**400
        huge = self.root / "huge.json"
        huge.write_text(json.dumps(report), encoding="utf-8")
        # The shared bounded reader rejects values analysis cannot represent.
        code, output = self._run(str(self.fixed), str(huge), "--fail-on-regression")
        self.assertEqual(code, EXIT_ERROR, output)
        self.assertIn("unrepresentable number", output)
        self.assertNotIn("DRAINTRACER REPORT DIFF", output)
        # One too long to parse at all is an unreadable report.
        endless = self.root / "endless.json"
        endless.write_text(json.dumps(report).replace("1" + "0" * 400, "1" + "0" * 5000),
                           encoding="utf-8")
        code, output = self._run(str(self.fixed), str(endless), "--fail-on-regression")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not a usable JSON report", output)

    def test_main_dispatches_the_diff_command(self) -> None:
        console = make_console()
        code = cli._main(console, ["diff", str(self.fixed), str(self.leaky), "--fail-on-regression"])
        self.assertEqual(code, EXIT_REGRESSION)
        self.assertIn("DRAINTRACER REPORT DIFF", console.file.getvalue())

    def test_missing_arguments_are_usage_errors(self) -> None:
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            run_diff([str(self.leaky)], make_console())
        self.assertEqual(caught.exception.code, 2)


class HostileReportTextTests(unittest.TestCase):
    """Report fields are untrusted: none may reach the terminal as control sequences."""

    TARGET = "C:\\t\\evil\x1b]0;PWNED\x07\x1b[2J.exe"
    HOSTILE = ("\x1b", "\x07", "\x9b", "\u202e", "\nINJECTED")

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def _report(self, name: str, rss_step: float, workload: str, **extra) -> str:
        experiment = _experiment(workload_id="w", build="1.0")
        experiment["workload_id"] = workload
        experiment["build"] = "b\x1b[31mRED\u202e"
        report = _run(rss_step, target_path=self.TARGET, experiment=experiment, **extra)
        for index, snapshot in enumerate(report["snapshots"]):
            snapshot["handle_types"] = {"File": 10 + index, "cat\x1b]0;T\x07\nINJECTED": index}
        report["version"] = "v1\x1b[2J"
        report["report_id"] = f"run-{name}"
        path = self.root / name
        path.write_text(json.dumps(report), encoding="utf-8")
        return str(path)

    def _pair(self) -> tuple[str, str]:
        return (self._report("a.json", 4.0, "w1\x1b[8mHIDDEN", severity=LeakSeverity.HIGH),
                self._report("b.json", 0.0, "w2\x1b[8mHIDDEN"))

    def _assert_inert(self, output: str) -> None:
        for marker in self.HOSTILE:
            self.assertNotIn(marker, output)
        self.assertNotIn("\nINJECTED", output)

    def test_panel_plain_and_quiet_output_escape_report_strings(self) -> None:
        before, after = self._pair()
        for flags, width in (((), 140), ((), 80), (("--plain",), 100), (("--quiet",), 100),
                             (("--strict",), 140)):
            with self.subTest(flags=flags, width=width):
                console = make_console(width=width)
                run_diff([before, after, *flags], console)
                output = console.file.getvalue()
                self._assert_inert(output)
                if not {"--plain", "--quiet"} & set(flags):  # only the panel names the target
                    self.assertIn("evil\\x1b]0;PWNED\\x07\\x1b[2J.exe", output.replace("\n", ""))
                self.assertIn("\\x1b[8mHIDDEN", output.replace("\n", ""))

    def test_repeated_mode_escapes_report_strings(self) -> None:
        runs = [self._report(f"r{index}.json", 0.0, f"w{index}\x1b[8mHIDDEN") for index in range(4)]
        for flags in ((), ("--plain",), ("--quiet",)):
            with self.subTest(flags=flags):
                console = make_console(width=140)
                run_diff([runs[0], runs[2], "--before-run", runs[1], "--after-run", runs[3],
                          *flags], console)
                output = console.file.getvalue()
                self.assertIn("COMPARABILITY", output)
                self.assertIn("\\x1b[8mHIDDEN", output.replace("\n", ""))
                self._assert_inert(output)

    def test_json_output_keeps_the_original_strings_escaped_by_json(self) -> None:
        before, after = self._pair()
        console = make_console()
        run_diff([before, after, "--json"], console)
        output = console.file.getvalue()
        self.assertNotIn("\x1b", output)
        self.assertIn("evil\x1b]0;PWNED\x07\x1b[2J.exe", json.loads(output)["after"]["target"])


if __name__ == "__main__":
    unittest.main()
