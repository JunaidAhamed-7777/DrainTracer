"""Saved baseline loading, CLI parity, modal input, and failure isolation."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from helpers import make_console, make_snapshot, render_text

from src.config import MonitorSettings
from src.dashboard import DashboardApp
from src.diff_command import run_diff
from src.report_diff import load_report


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
        self.assertEqual(self.app.view, "comparison")
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


if __name__ == "__main__":
    unittest.main()
