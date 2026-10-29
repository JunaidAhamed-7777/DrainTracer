"""Headless runner exit codes, output, and end-to-end runs on short scripts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import make_alert, make_console, make_snapshot

from src.events import LeakSeverity
from src.headless import (
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_LEAK,
    EXIT_PASS,
    HeadlessRunner,
)


def _output(runner: HeadlessRunner) -> str:
    return runner.console.file.getvalue()


class ExitStatusTests(unittest.TestCase):
    def _runner(self, fail_on: LeakSeverity = LeakSeverity.HIGH) -> HeadlessRunner:
        return HeadlessRunner("C:/targets/worker.exe", console=make_console(), fail_on=fail_on)

    def test_threshold_decides_pass_or_fail(self) -> None:
        cases = (
            (LeakSeverity.HIGH, LeakSeverity.MEDIUM, EXIT_PASS),
            (LeakSeverity.HIGH, LeakSeverity.HIGH, EXIT_LEAK),
            (LeakSeverity.HIGH, LeakSeverity.CRITICAL, EXIT_LEAK),
            (LeakSeverity.LOW, LeakSeverity.LOW, EXIT_LEAK),
            (LeakSeverity.CRITICAL, LeakSeverity.HIGH, EXIT_PASS),
        )
        for fail_on, observed, expected in cases:
            with self.subTest(fail_on=fail_on, observed=observed):
                runner = self._runner(fail_on)
                runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=observed),)))
                self.assertEqual(runner.exit_status(), expected)

    def test_progress_and_summary_lead_with_private_bytes(self) -> None:
        from helpers import make_process

        mib = 1024 * 1024
        runner = self._runner()
        runner._accept_snapshot(
            make_snapshot(processes=(make_process(100, rss=300 * mib, private=25 * mib),))
        )
        runner._print_progress()
        runner._print_summary()
        output = _output(runner).replace("·", "|")
        self.assertIn("private 25.0 MB | rss 300.0 MB", output)
        self.assertIn("private 25.0 MB | RSS (working set) 300.0 MB", output)

    def test_summary_reports_the_monitor_cost(self) -> None:
        from dataclasses import replace

        from src.events import MonitorDiagnostics

        runner = self._runner()
        self.assertIsNone(runner.monitor_cost_text())
        for index, cost in enumerate((10.0, 30.0)):
            runner._accept_snapshot(replace(
                make_snapshot(timestamp=100.0 + index * 0.5),
                diagnostics=MonitorDiagnostics(500, collection_ms=cost, monitor_cpu_percent=1.25),
            ))
        runner._print_summary()
        output = _output(runner).replace("·", "|")
        self.assertIn(
            "monitor   cadence 500 ms mean (500 requested) | collection 20 ms mean, 30 ms max "
            "| monitor CPU 1.2% mean",
            output,
        )

    def test_clean_run_passes(self) -> None:
        runner = self._runner()
        runner._accept_snapshot(make_snapshot())
        self.assertEqual(runner.exit_status(), EXIT_PASS)

    def test_status_priority_is_leak_then_interrupt_then_error(self) -> None:
        runner = self._runner()
        runner._interrupted = True
        runner._export_error = "disk full"
        self.assertEqual(runner.exit_status(), EXIT_INTERRUPTED)
        runner._interrupted = False
        self.assertEqual(runner.exit_status(), EXIT_ERROR)
        runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        self.assertEqual(runner.exit_status(), EXIT_LEAK)

    def test_reports_record_the_threshold_and_outcome(self) -> None:
        runner = self._runner(LeakSeverity.MEDIUM)
        runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        self.assertEqual(
            runner.build_report()["headless"],
            {"fail_on": "medium", "breached": True, "fail_on_inconclusive": False,
             "fail_on_target_error": False, "budgets": []},
        )

    def test_a_budget_exceeded_by_the_first_sample_is_logged_after_the_launch(self) -> None:
        from dataclasses import replace

        from helpers import make_process

        from src.config import MonitorSettings

        settings = replace(MonitorSettings(), budgets={"threads": 3.0})
        runner = HeadlessRunner("C:/targets/worker.exe", console=make_console(),
                                settings=settings)
        # ``start`` samples once before it logs the launch.
        runner._accept_snapshot(make_snapshot(processes=(make_process(100),)))
        self.assertNotIn("Budget exceeded", _output(runner))
        runner._log_start()
        output = _output(runner)
        self.assertLess(output.index("Launched"), output.index("Budget exceeded"))
        self.assertEqual(output.count("Budget exceeded"), 1)

    def test_stable_but_excessive_usage_fails_a_budget_without_a_leak(self) -> None:
        from dataclasses import replace

        from helpers import make_process

        from src.config import MonitorSettings
        from src.headless import EXIT_BUDGET

        mib = 1024 * 1024
        settings = replace(MonitorSettings(), budgets={"private_bytes": 500.0 * mib,
                                                       "handles": 2000.0})
        runner = HeadlessRunner("C:/targets/worker.exe", console=make_console(),
                                settings=settings)
        runner._accept_snapshot(make_snapshot(processes=(make_process(100, private=400 * mib),)))
        runner._log_start()
        self.assertEqual(runner.exit_status(), EXIT_PASS)
        for index in range(3):  # flat, so no leak signal; just too much
            runner._accept_snapshot(make_snapshot(
                processes=(make_process(100, private=612 * mib),), timestamp=101.0 + index))
        self.assertEqual(runner.exit_status(), EXIT_BUDGET)
        output = _output(runner)
        self.assertEqual(output.count("Budget exceeded"), 1)
        runner._print_summary()
        output = _output(runner).replace("·", "|")
        self.assertIn("private bytes peak 612.0 MB over the 500.0 MB budget", output)
        self.assertIn("handles peak 50 within the 2,000 budget", output)
        result = next(line for line in output.splitlines() if line.startswith("RESULT"))
        self.assertIn("OVER BUDGET", result)
        self.assertIn("budgets", result)
        budgets = runner.build_report()["headless"]["budgets"]
        self.assertEqual([item["exceeded"] for item in budgets], [True, False])
        # A leak signal still wins over a budget.
        runner._accept_snapshot(make_snapshot(
            processes=(make_process(100, private=612 * mib),),
            alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        self.assertEqual(runner.exit_status(), EXIT_LEAK)

    def test_none_is_not_a_failure_threshold(self) -> None:
        with self.assertRaises(ValueError):
            self._runner(LeakSeverity.NONE)


class OutputTests(unittest.TestCase):
    def test_plain_interrupt_exports_readable_evidence_and_runs_cleanup(self) -> None:
        from src.theme import plain_console

        with tempfile.TemporaryDirectory() as directory:
            runner = HeadlessRunner("worker.exe", console=plain_console(make_console()),
                                    report_dir=directory, quiet=True)
            with mock.patch.object(runner, "start"), \
                    mock.patch.object(runner, "_monitor", side_effect=KeyboardInterrupt), \
                    mock.patch.object(runner, "shutdown") as cleanup:
                self.assertEqual(runner.run(), EXIT_INTERRUPTED)
            cleanup.assert_called_once_with()
            self.assertEqual(len(list(Path(directory).rglob("*.json"))), 1)
            self.assertEqual(len(list(Path(directory).rglob("*.html"))), 1)
            self.assertIn("INTERRUPTED", _output(runner))
            self.assertIn("INCONCLUSIVE", _output(runner).upper())
            self.assertNotIn("\x1b", _output(runner))

    def test_static_output_distinguishes_unavailable_counters_from_real_zero(self) -> None:
        from helpers import make_denied, make_process

        for process, amount in ((make_denied(100), "n/a"),
                                (make_process(100, private=0, rss=0, handles=0, threads=0), "0.0 B")):
            with self.subTest(amount=amount):
                runner = HeadlessRunner("worker.exe", console=make_console())
                runner._accept_snapshot(make_snapshot(processes=(process,), measured_coverage=True))
                runner._print_progress()
                runner._print_summary()
                output = _output(runner)
                self.assertIn(f"private {amount}", output)
                self.assertEqual(runner._metric_text("private_bytes", peak=True), amount)
                if amount == "n/a":
                    self.assertIn("handles n/a", output)
                    self.assertIsNone(runner.facts.peak("private_bytes"))
                else:
                    self.assertIn("handles 0", output)

    def test_quiet_hides_routine_output_but_keeps_evidence_and_failures(self) -> None:
        runner = HeadlessRunner("worker.exe", console=make_console(), quiet=True)
        runner._print_banner()
        runner._append_event("routine event", "info")
        runner._print_progress()
        self.assertEqual(_output(runner), "")
        runner._append_event("partial coverage warning", "warn")
        runner._append_event("export error", "error")
        runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        runner._print_summary()
        output = _output(runner)
        self.assertIn("partial coverage warning", output)
        self.assertIn("export error", output)
        self.assertIn("RESULT", output)
        self.assertIn("HIGH", output)
        self.assertEqual(runner.exit_status(), EXIT_LEAK)
        self.assertTrue(any("routine event" in event[1] for event in runner._events))

    def test_events_print_as_tagged_lines(self) -> None:
        runner = HeadlessRunner("C:/targets/worker.exe", console=make_console())
        runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.MEDIUM),)))
        lines = _output(runner).splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("ALERT", lines[0])
        self.assertIn("Memory leak signal MEDIUM", lines[0])

    def test_markup_in_event_text_is_printed_literally(self) -> None:
        runner = HeadlessRunner("C:/targets/worker.exe", console=make_console())
        runner._append_event("[bold]not markup[/bold]", "info")
        self.assertIn("[bold]not markup[/bold]", _output(runner))

    def test_github_annotation_is_escaped(self) -> None:
        runner = HeadlessRunner("C:/targets/100%,x.exe", console=make_console())
        runner._github_actions = True
        runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        runner._print_summary()
        annotations = [line for line in _output(runner).splitlines() if line.startswith("::")]
        self.assertEqual(len(annotations), 1)
        self.assertTrue(annotations[0].startswith("::error title=DrainTracer leak check::"))
        self.assertIn("100%25,x.exe", annotations[0])

    def test_no_annotation_outside_github_actions(self) -> None:
        runner = HeadlessRunner("C:/targets/worker.exe", console=make_console())
        runner._github_actions = False
        runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        runner._print_summary()
        self.assertNotIn("::error", _output(runner))
        self.assertIn("FAIL", _output(runner))


class StepSummaryTests(unittest.TestCase):
    def test_github_step_summary_gets_a_result_table(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as temp:
            summary_file = Path(temp) / "summary.md"
            runner = HeadlessRunner("C:/targets/a|b*.exe", console=make_console())
            runner._github_actions = True
            runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
            runner._last_report_data = runner.build_report()
            runner._last_report = Path(temp) / "leak_report_x.json"
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_file)}):
                runner._print_summary()
                runner._print_summary()  # a second run appends
            markdown = summary_file.read_text(encoding="utf-8")
        self.assertEqual(markdown.count("### DrainTracer leak check: FAIL"), 2)
        self.assertIn("**a\\|b\\*.exe**", markdown)
        self.assertIn("| memory | **HIGH** |", markdown)
        self.assertIn("| handles | clear |", markdown)
        self.assertIn("Worst leak signal HIGH.", markdown)
        self.assertIn("leak_report_x.json` (HTML alongside)", markdown)

    def test_names_utf8_cannot_hold_still_reach_the_step_summary(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as temp:
            summary_file = Path(temp) / "summary.md"
            runner = HeadlessRunner("C:/targets/bad\udc80name.exe", console=make_console())
            runner._github_actions = True
            runner._accept_snapshot(make_snapshot())
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_file)}):
                runner._print_summary()
            markdown = summary_file.read_text(encoding="utf-8")
        self.assertIn("**bad?name.exe**", markdown)

    def test_no_step_summary_outside_github_actions(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as temp:
            summary_file = Path(temp) / "summary.md"
            runner = HeadlessRunner("C:/targets/worker.exe", console=make_console())
            runner._github_actions = False
            runner._accept_snapshot(make_snapshot())
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_file)}):
                runner._print_summary()
            self.assertFalse(summary_file.exists())


class ConclusivenessTests(unittest.TestCase):
    def _runner(self, warmup: float = 0.0) -> HeadlessRunner:
        from dataclasses import replace

        from src.config import MonitorSettings

        return HeadlessRunner(
            "C:/targets/worker.exe",
            console=make_console(),
            settings=replace(MonitorSettings(), warmup=warmup),
        )

    def _result_line(self, runner: HeadlessRunner) -> str:
        return next(line for line in _output(runner).splitlines() if line.startswith("RESULT"))

    def test_a_run_that_never_judged_a_window_is_inconclusive(self) -> None:
        runner = self._runner()
        runner._github_actions = True
        runner._accept_snapshot(make_snapshot())
        runner._print_summary()
        self.assertEqual(runner.exit_status(), EXIT_PASS)
        self.assertIn("INCONCLUSIVE", self._result_line(runner))
        self.assertIn("monitoring ended before the 10s window filled", _output(runner))
        self.assertIn("::warning title=DrainTracer leak check::", _output(runner))

    def test_an_unfinished_warmup_is_named(self) -> None:
        runner = self._runner(warmup=30.0)
        runner._accept_snapshot(make_snapshot())
        runner._print_summary()
        output = _output(runner)
        self.assertIn("the 30s warm-up never finished", output)
        self.assertIn("warm-up   30s | did not finish", output.replace("·", "|"))

    def test_a_judged_clean_run_passes(self) -> None:
        runner = self._runner()
        runner._github_actions = True
        runner.detector._detection_started = True
        runner._accept_snapshot(make_snapshot())
        runner._print_summary()
        self.assertIn("PASS", self._result_line(runner))
        self.assertNotIn("::", _output(runner))

    def test_a_breach_is_a_failure_even_before_a_window_filled(self) -> None:
        runner = self._runner()
        runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        runner._print_summary()
        self.assertIn("FAIL", self._result_line(runner))


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def _runner(self, body: str, **kwargs) -> HeadlessRunner:
        script = self.root / "target.py"
        script.write_text(body, encoding="utf-8")
        return HeadlessRunner(
            str(script), console=make_console(), report_dir=self.root / "reports", **kwargs
        )

    def _report(self) -> dict:
        reports = list((self.root / "reports").glob("*/leak_report_*.json"))
        self.assertEqual(len(reports), 1)
        return json.loads(reports[0].read_text(encoding="utf-8"))

    def test_target_exit_ends_the_run_and_exports_a_report(self) -> None:
        runner = self._runner("import time\ntime.sleep(0.3)\nraise SystemExit(3)\n")
        self.assertEqual(runner.run(), EXIT_PASS)
        output = _output(runner)
        self.assertIn("exited with code 3", output)
        self.assertIn("Monitored process tree exited", output)
        report = self._report()
        self.assertEqual(report["target_exit_code"], 3)
        self.assertIsNotNone(report["root_pid"])
        self.assertIsNotNone(report["session_ended_at"])
        self.assertFalse(report["snapshots"][-1]["process_alive"])

    def test_time_limit_stops_and_cleans_up_the_target(self) -> None:
        runner = self._runner("import time\ntime.sleep(60)\n", timeout=1.0)
        self.assertEqual(runner.run(), EXIT_PASS)
        self.assertIn("Time limit of 1s reached", _output(runner))
        self.assertIn("stopped by DrainTracer", _output(runner))
        self.assertTrue(runner.stopped_target)
        self.assertFalse(runner.manager.has_tracked_processes)
        self.assertIsNone(self._report()["target_exit_code"])

    def test_interrupt_cleans_up_and_exits_130(self) -> None:
        runner = self._runner("import time\ntime.sleep(60)\n")
        with mock.patch.object(HeadlessRunner, "_monitor", side_effect=KeyboardInterrupt):
            self.assertEqual(runner.run(), EXIT_INTERRUPTED)
        self.assertFalse(runner.manager.has_tracked_processes)
        self.assertIn("INTERRUPTED", _output(runner))

    def test_an_unexpected_error_still_stops_the_target_and_exports(self) -> None:
        runner = self._runner("import time\ntime.sleep(60)\n")
        with mock.patch.object(HeadlessRunner, "_monitor", side_effect=RuntimeError("progress bug")), \
                self.assertLogs("src.headless", "ERROR"):
            self.assertEqual(runner.run(), EXIT_ERROR)
        self.assertFalse(runner.manager.has_tracked_processes)
        self.assertIn("monitoring failed: progress bug", _output(runner))
        messages = [event["message"] for event in self._report()["event_log"]]
        self.assertIn("Monitoring failed: progress bug", messages)

    def test_missing_target_is_an_error(self) -> None:
        runner = HeadlessRunner(
            str(self.root / "missing.exe"), console=make_console(), report_dir=self.root
        )
        self.assertEqual(runner.run(), EXIT_ERROR)
        self.assertIn("Could not start monitoring", _output(runner))


class PolicyTests(unittest.TestCase):
    def _runner(self, **policies) -> HeadlessRunner:
        return HeadlessRunner("C:/targets/worker.exe", console=make_console(), **policies)

    def _exit_code(self, runner: HeadlessRunner, code: int | None):
        return mock.patch.object(type(runner.manager), "exit_code",
                                 new_callable=mock.PropertyMock, return_value=code)

    def test_policies_are_off_by_default(self) -> None:
        runner = self._runner()
        runner._accept_snapshot(make_snapshot())
        with self._exit_code(runner, 5):
            self.assertEqual(runner.exit_status(), EXIT_PASS)

    def test_inconclusive_runs_can_fail(self) -> None:
        from src.headless import EXIT_INCONCLUSIVE

        runner = self._runner(fail_on_inconclusive=True)
        runner._accept_snapshot(make_snapshot())
        self.assertEqual(runner.exit_status(), EXIT_INCONCLUSIVE)
        runner._print_summary()
        result = next(line for line in _output(runner).splitlines() if line.startswith("RESULT"))
        self.assertIn("INCONCLUSIVE", result)
        self.assertIn("inconclusive runs", result)
        runner.detector._detection_started = True
        self.assertEqual(runner.exit_status(), EXIT_PASS)
        runner._accept_snapshot(make_snapshot(alerts=(make_alert(severity=LeakSeverity.HIGH),)))
        self.assertEqual(runner.exit_status(), EXIT_LEAK)

    def test_target_errors_can_fail(self) -> None:
        from src.headless import EXIT_TARGET_ERROR

        runner = self._runner(fail_on_target_error=True)
        runner.detector._detection_started = True
        runner._accept_snapshot(make_snapshot())
        with self._exit_code(runner, 0):
            self.assertEqual(runner.exit_status(), EXIT_PASS)
        with self._exit_code(runner, 3):
            self.assertEqual(runner.exit_status(), EXIT_TARGET_ERROR)
            runner._print_summary()
            self.assertIn("the target exited with code 3", _output(runner))
            runner._stopped_target = True  # DrainTracer ended it: not the target's failure
            self.assertEqual(runner.exit_status(), EXIT_PASS)

    def test_summary_separates_target_measurement_and_errors(self) -> None:
        from helpers import make_denied, make_process

        runner = self._runner()
        runner.detector._detection_started = True
        runner._accept_snapshot(make_snapshot(metadata={"sampling_error": "boom"}))
        runner._accept_snapshot(make_snapshot(
            processes=(make_process(100), make_denied(200)), measured_coverage=True))
        runner._run_error = "monitoring failed: oops"
        runner._print_summary()
        output = _output(runner).replace("·", "|")
        self.assertIn("measured  partial | 1 process unreadable (PID 200)", output)
        self.assertIn("errors    sampling error: boom | monitoring failed: oops", output)
        self.assertIn("Partial coverage: 1 process unreadable", output)
        outcome = runner.build_report()["outcome"]
        self.assertEqual(outcome["measurement"]["result"], "partial")
        self.assertEqual(outcome["monitor_errors"], ["sampling error: boom", "monitoring failed: oops"])

    def test_progress_lines_show_the_evidence(self) -> None:
        from dataclasses import replace

        from src.events import EvidenceState, LeakResource, ResourceEvidence

        runner = self._runner()
        evidence = tuple(
            ResourceEvidence(resource, EvidenceState.COLLECTING, valid_samples=9,
                             needed_samples=4, elapsed_seconds=4.0, needed_seconds=8.5)
            for resource in LeakResource
        )
        runner._accept_snapshot(replace(make_snapshot(), evidence=evidence))
        runner._print_progress()
        self.assertIn("signal collecting 4s/8.5s", _output(runner))


if __name__ == "__main__":
    unittest.main()
