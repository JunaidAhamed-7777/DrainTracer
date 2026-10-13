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
        self.assertEqual(runner.build_report()["headless"], {"fail_on": "medium", "breached": True})

    def test_none_is_not_a_failure_threshold(self) -> None:
        with self.assertRaises(ValueError):
            self._runner(LeakSeverity.NONE)


class OutputTests(unittest.TestCase):
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
        reports = list((self.root / "reports").glob("leak_report_*.json"))
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

    def test_missing_target_is_an_error(self) -> None:
        runner = HeadlessRunner(
            str(self.root / "missing.exe"), console=make_console(), report_dir=self.root
        )
        self.assertEqual(runner.run(), EXIT_ERROR)
        self.assertIn("Could not start monitoring", _output(runner))


if __name__ == "__main__":
    unittest.main()
