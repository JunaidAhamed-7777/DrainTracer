"""Live process selection, saved baseline loading, and comparison failure isolation."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import psutil

from helpers import make_console, make_snapshot, render_text

from src.config import MonitorSettings
from src.dashboard import DashboardApp
from src.diff_command import run_diff
from src.report_diff import load_report
from src.running_processes import ProcessReading, RunningProcess, sample_process


class ComparisonViewTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.app = DashboardApp("worker.exe", console=make_console(140, 40),
                                settings=replace(MonitorSettings(), operations=100))
        self.app.detector._detection_started = True
        for index in range(21):
            self.app._accept_snapshot(make_snapshot(timestamp=1000 + index * 0.5))
        self.baseline = self.folder / "baseline [red] with spaces.json"
        self.baseline.write_text(json.dumps(self.app.build_report()), encoding="utf-8")
        self.app._handle_key("6")

    def complete(self) -> None:
        self.app._comparison_worker.join(timeout=5)
        self.assertFalse(self.app._comparison_worker.is_alive())
        self.app._drain_comparison_result()

    def enter_pair(self, pids: str) -> None:
        self.app._handle_key("p")
        for char in pids:
            self.app._handle_key(char)
        self.app._handle_key("enter")

    def finish_pair(self) -> None:
        self.app._live_job[1].result(timeout=3)
        self.app._poll_live_pair()

    def test_compares_two_independent_pids_then_switches_to_report(self) -> None:
        self.assertIn("Two live processes", render_text(self.app))
        self.assertIn("Current session and saved JSON", render_text(self.app))
        samples = [
            ProcessReading(RunningProcess(454, "first", 20), threads=10, resident_bytes=1024**2),
            ProcessReading(RunningProcess(773, "other", 35), threads=13, resident_bytes=2 * 1024**2),
        ]
        with mock.patch("src.view_comparison.sample_process", side_effect=samples) as read:
            self.enter_pair("454 773")
            self.finish_pair()
            self.assertEqual(read.call_args_list, [mock.call(454, None), mock.call(773, None)])
        output = render_text(self.app)
        self.assertIn("PID 454  first", output)
        self.assertIn("PID 773  other", output)
        self.assertIn("+1.0 MB", output)
        self.assertIn("+3", output)
        self.app._handle_key("b")
        for char in str(self.baseline):
            self.app._handle_key(char)
        self.app._handle_key("enter")
        self.complete()
        self.assertIn("COMPARABILITY: COMPARABLE", render_text(self.app))

    def test_live_editor_validates_pids_and_owns_keys(self) -> None:
        with mock.patch.object(self.app.manager, "kill") as kill, \
                mock.patch.object(self.app.manager, "pause") as pause, \
                mock.patch("src.view_comparison.sample_process") as sample:
            self.enter_pair("qk0 0")
            self.assertIn("Enter two positive PIDs", render_text(self.app))
            self.assertFalse(self.app._quit_requested)
            kill.assert_not_called()
            pause.assert_not_called()
            for char in ("backspace", "backspace", "backspace", "1", ",", "1", "enter"):
                self.app._handle_key(char)
            self.assertIn("Choose two different PIDs", render_text(self.app))
            sample.assert_not_called()
            self.app._handle_key("escape")
            self.assertIsNone(self.app._pid_draft)
            self.assertEqual(self.app.view, "overview")

    def test_blocked_worker_does_not_freeze_ui_or_overwrite_new_pair(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def read(pid, previous):
            if pid == 123:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test worker was not released")
            return ProcessReading(RunningProcess(pid, str(pid), float(pid)), threads=2)

        with mock.patch("src.view_comparison.sample_process", side_effect=read) as lookup:
            try:
                self.enter_pair("123,456")
                self.assertTrue(entered.wait(1))
                for _ in range(3):
                    self.assertIn("Reading processes", render_text(self.app))
                self.enter_pair("456 789")
                self.assertIsNone(self.app._live_pair)
                lookup.assert_called_once()
            finally:
                release.set()
                self.finish_pair()
            self.finish_pair()
        self.assertEqual([sample.process.pid for sample in self.app._live_pair], [456, 789])

    def test_saved_baseline_uses_the_same_result_as_strict_cli(self) -> None:
        self.app._request_comparison(self.baseline)
        self.complete()
        comparison = self.app._comparison
        after = self.folder / "after.json"
        after.write_text(json.dumps(comparison.after.data), encoding="utf-8")
        console = make_console()
        run_diff([str(self.baseline), str(after), "--strict", "--json"], console)
        result = json.loads(console.file.getvalue())
        for key in ("verdict", "comparability", "resources", "caveats"):
            self.assertEqual(result[key], comparison.to_dict()[key])
        output = " ".join(render_text(self.app).split())
        self.assertIn("COMPARABILITY: COMPARABLE", output)
        self.assertIn("Signal episodes A -> B", output)
        self.assertTrue(all(item.normalized for item in comparison.resources))

    def test_incompatible_baseline_caveats_precede_difference_labels(self) -> None:
        data = json.loads(self.baseline.read_text(encoding="utf-8"))
        data["target_path"] = "other-program.exe"
        self.baseline.write_text(json.dumps(data), encoding="utf-8")
        self.app._request_comparison(self.baseline)
        self.complete()
        output = render_text(self.app)
        self.assertTrue(self.app._comparison.blocked)
        self.assertLess(output.index("COMPARABILITY: NOT COMPARABLE"), output.index("DRAINTRACER REPORT DIFF"))

    def test_background_loading_keeps_accepting_snapshots(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def wait_then_load(path):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test worker was not released")
            return load_report(path)

        with mock.patch("src.view_comparison.load_report", side_effect=wait_then_load):
            try:
                self.app._request_comparison(self.baseline)
                self.assertTrue(entered.wait(1))
                count = self.app._sample_count
                self.app._accept_snapshot(make_snapshot(timestamp=1011))
                self.assertEqual(self.app._sample_count, count + 1)
                self.assertIn("sampling continues", render_text(self.app))
                self.assertFalse(self.app.detector.is_paused)
            finally:
                release.set()
                self.complete()

    def test_bad_files_preserve_the_previous_comparison(self) -> None:
        self.app._request_comparison(self.baseline)
        self.complete()
        previous = self.app._comparison
        invalid = self.folder / "invalid.json"
        invalid.write_bytes(b"not json")
        for path in (invalid, self.folder / "missing.json"):
            self.app._request_comparison(path)
            self.complete()
            self.assertIs(self.app._comparison, previous)
            self.assertIn("Baseline unavailable", render_text(self.app))

    def test_worker_start_failure_preserves_evidence_and_allows_retry(self) -> None:
        self.app._request_comparison(self.baseline)
        self.complete()
        baseline, previous = self.app._baseline_report, self.app._comparison
        for operation in ("src.view_comparison.threading.Thread",
                          "src.view_comparison.threading.Thread.start"):
            with self.subTest(operation=operation), \
                    mock.patch(operation, side_effect=RuntimeError("[red]thread unavailable")):
                self.app._request_comparison(None)
            self.assertFalse(self.app._comparison_loading)
            self.assertIsNone(self.app._comparison_worker)
            self.assertIs(self.app._baseline_report, baseline)
            self.assertIs(self.app._comparison, previous)
            self.app.console = make_console(140, 40, "cp437")
            output = render_text(self.app)
            self.assertIn("[red]thread unavailable", output)
            self.assertIn("Press B to choose another report", output)
            output.encode("cp437")
        self.app._request_comparison(None)
        self.complete()
        self.assertIsNone(self.app._comparison_error)

    def test_uncertain_started_worker_blocks_overlapping_retry(self) -> None:
        entered, release = threading.Event(), threading.Event()
        real_start = threading.Thread.start

        def start_then_raise(worker):
            real_start(worker)
            raise RuntimeError("start acknowledgement lost")

        def wait_then_load(path):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test worker was not released")
            return load_report(path)

        with mock.patch("src.view_comparison.load_report", side_effect=wait_then_load), \
                mock.patch.object(threading.Thread, "start", autospec=True,
                                  side_effect=start_then_raise) as start:
            try:
                self.app._request_comparison(self.baseline)
                self.assertTrue(entered.wait(1))
                worker = self.app._comparison_worker
                self.assertTrue(self.app._comparison_loading)
                output = render_text(self.app)
                self.assertIn("Wait for the current comparison to finish before retrying.", output)
                self.assertNotIn("Press B", output)
                self.app._request_comparison(self.baseline)
                start.assert_called_once()
                self.assertIs(self.app._comparison_worker, worker)
            finally:
                release.set()
                self.complete()
        self.assertFalse(self.app._comparison_loading)
        self.assertIsNone(self.app._comparison_error)
        self.assertIsNotNone(self.app._comparison)
        self.app._request_comparison(None)
        self.complete()

    def test_worker_start_keyboard_interrupt_propagates(self) -> None:
        with mock.patch("src.view_comparison.threading.Thread.start", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.app._request_comparison(self.baseline)

    def test_queued_result_keeps_worker_owned_until_it_exits(self) -> None:
        queued, release = threading.Event(), threading.Event()
        real_thread = threading.Thread

        def build_worker(**options):
            target = options["target"]

            def hold_after_result():
                target()
                queued.set()
                release.wait(3)

            return real_thread(**{**options, "target": hold_after_result})

        with mock.patch("src.view_comparison.threading.Thread", side_effect=build_worker) as factory:
            try:
                self.app._request_comparison(self.baseline)
                self.assertTrue(queued.wait(1))
                self.app._drain_comparison_result()
                self.assertTrue(self.app._comparison_loading)
                self.assertIsNone(self.app._comparison)
                self.app._request_comparison(self.baseline)
                factory.assert_called_once()
            finally:
                release.set()
                self.complete()
        self.assertIsNotNone(self.app._comparison)

    def test_path_editor_captures_target_keys_and_cancels(self) -> None:
        self.app._handle_key("b")
        with mock.patch.object(self.app.manager, "kill") as kill, \
                mock.patch.object(self.app.manager, "pause") as pause:
            for char in "k q path with spaces":
                self.app._handle_key(char)
            kill.assert_not_called()
            pause.assert_not_called()
        self.assertIn("ENTER load", render_text(self.app).splitlines()[-1])
        self.app._handle_key("escape")
        self.assertIsNone(self.app._baseline_draft)
        self.assertEqual(self.app.view, "overview")
        self.assertFalse(self.app._quit_requested)

    def test_path_editor_loads_spaces_and_brackets_literally(self) -> None:
        self.app._handle_key("b")
        for character in str(self.baseline):
            self.app._handle_key(character)
        self.app._handle_key("enter")
        self.complete()
        self.assertEqual(self.app._baseline_report.path, self.baseline)
        self.assertIsNone(self.app._comparison_error)

    def test_per_operation_values_are_omitted_when_one_side_has_no_count(self) -> None:
        data = json.loads(self.baseline.read_text(encoding="utf-8"))
        data["experiment"]["operations"] = None
        self.baseline.write_text(json.dumps(data), encoding="utf-8")
        self.app._request_comparison(self.baseline)
        self.complete()
        self.assertFalse(any(item.normalized for item in self.app._comparison.resources))
        with self.app.console.capture() as capture:
            self.app.console.print(self.app._render_comparison_view())
        self.assertNotIn("per operation", capture.get())

    def test_view_fits_narrow_and_legacy_consoles_with_scrolling(self) -> None:
        self.app._request_comparison(self.baseline)
        self.complete()
        for width, height in ((80, 24), (50, 20), (140, 40)):
            self.app.console = make_console(width, height, "cp437")
            self.app._handle_key("home")
            text = render_text(self.app)
            text.encode("cp437")
            self.assertEqual(len(text.splitlines()), height)
            self.assertTrue(all(len(line) <= width for line in text.splitlines()))
            self.app._handle_key("end")
            if width <= 80:
                self.assertIn("end of comparison", render_text(self.app))


class ProcessSamplingTests(unittest.TestCase):
    def test_reads_current_process_and_warms_up_cpu(self) -> None:
        first = sample_process(os.getpid())
        self.assertIsNone(first.error)
        self.assertIsNone(first.cpu_percent)
        second = sample_process(os.getpid(), first)
        self.assertEqual(second.process, first.process)
        self.assertIsNotNone(second.cpu_percent)
        self.assertGreater(second.resident_bytes, 0)
        self.assertGreater(second.threads, 0)

    def test_partial_metric_access_and_reused_pid_do_not_supply_false_values(self) -> None:
        process = mock.Mock()
        process.create_time.side_effect = (3.0, 3.0, 8.0)
        process.as_dict.return_value = {
            "name": "first", "cpu_times": None, "num_threads": 4, "memory_info": None,
            "num_handles": None, "num_fds": None,
        }
        with mock.patch("src.running_processes.psutil.Process", return_value=process):
            first = sample_process(41)
            self.assertIsNone(first.error)
            self.assertIsNone(first.handles)
            self.assertIsNone(first.resident_bytes)
            self.assertEqual(first.threads, 4)
            reused = sample_process(41, first)
            self.assertIn("PID reused", reused.error)
            self.assertEqual(reused.process, first.process)
            self.assertIsNone(reused.threads)
            self.assertIs(sample_process(41, reused), reused)
            process.as_dict.assert_called_once()

    def test_process_exit_or_reuse_during_sample_discards_metrics(self) -> None:
        for failure in (psutil.NoSuchProcess(41), psutil.AccessDenied(41)):
            with self.subTest(failure=failure), \
                    mock.patch("src.running_processes.psutil.Process", side_effect=failure):
                reading = sample_process(41)
                self.assertTrue(reading.error)
                self.assertIsNone(reading.threads)
        with mock.patch("src.running_processes.psutil.Process") as constructor:
            process = constructor.return_value
            process.create_time.side_effect = (3.0, 8.0)
            process.as_dict.return_value = {"name": "replacement"}
            reading = sample_process(41)
            self.assertTrue(reading.error)
            self.assertNotEqual(reading.process.name, "replacement")
            self.assertIsNone(reading.resident_bytes)


if __name__ == "__main__":
    unittest.main()
