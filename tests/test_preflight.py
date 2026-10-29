"""Preflight checks use real configuration validation without target control."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import psutil
from helpers import make_console

from src import main as cli
from src import preflight


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _run(self, *arguments):
        console = make_console(width=60)
        code = cli._main(console, ["doctor", "--no-config", "--report-dir",
                                  str(self.root / "not-created" / "reports"), *arguments])
        return code, console.file.getvalue()

    def test_valid_target_checks_do_not_launch_or_create_project_files(self) -> None:
        target = self.root / "worker [literal].py"
        target.write_text("raise RuntimeError('must never execute')", encoding="utf-8")
        original = set(self.root.iterdir())
        with mock.patch("src.process_runner.subprocess.Popen") as launched:
            code, text = self._run("--target", str(target), "--json", "--", "--token=opaque-secret")
        launched.assert_not_called()
        self.assertEqual(code, 0)
        result = json.loads(text)
        self.assertFalse(result["target_controlled"])
        self.assertEqual(result["settings"]["origins"]["report_dir"], "command line")
        self.assertNotIn("opaque-secret", text)
        self.assertEqual(set(self.root.iterdir()), original)

    def test_missing_target_and_existing_file_report_path_are_blocking(self) -> None:
        code, text = self._run("--target", str(self.root / "missing.py"), "--json")
        self.assertEqual(code, 2)
        self.assertTrue(any(item["check"] == "target" and item["state"] == "error"
                            for item in json.loads(text)["checks"]))
        destination = self.root / "keep.txt"
        destination.write_text("keep this", encoding="utf-8")
        code, text = self._run("--report-dir", str(destination), "--json")
        self.assertEqual(code, 2)
        self.assertIn("not a directory", text)
        self.assertEqual(destination.read_text(encoding="utf-8"), "keep this")

    def test_profile_origins_are_explicit_and_experiment_labels_are_not_dumped(self) -> None:
        source = self.root / "settings.toml"
        source.write_text('sample_interval = "1s"\nnote = "token=private-label"\n'
                          '[profiles.ci]\nsample_interval = "250ms"\n', encoding="utf-8")
        console = make_console()
        code = cli._main(console, ["doctor", "--config", str(source), "--profile", "ci",
                                  "--report-dir", str(self.root / "reports"), "--json"])
        result = json.loads(console.file.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["settings"]["values"]["sample_interval_ms"], 250)
        self.assertEqual(result["settings"]["origins"]["sample_interval_ms"], "profile ci")
        self.assertNotIn("private-label", console.file.getvalue())

    def test_actual_probe_failure_is_reported_without_false_writable_claim(self) -> None:
        with mock.patch.object(preflight.tempfile, "TemporaryFile", side_effect=PermissionError("denied")):
            code, text = self._run("--json")
        self.assertEqual(code, 2)
        self.assertIn("Choose a writable report directory", text)
        self.assertIn("denied", text)

    def test_pid_permissions_are_observed_without_control(self) -> None:
        process = mock.Mock()
        process.is_running.return_value = True
        process.status.return_value = psutil.STATUS_RUNNING
        process.memory_info.side_effect = psutil.AccessDenied(999999)
        with mock.patch.object(preflight.psutil, "Process", return_value=process):
            code, text = self._run("--pid", "999999", "--json")
        self.assertEqual(code, 0)
        self.assertIn("partial coverage", text)
        for method in ("terminate", "kill", "suspend", "resume"):
            getattr(process, method).assert_not_called()

    def test_linux_batch_error_is_static(self) -> None:
        target = self.root / "target.cmd"
        target.write_text("echo never run", encoding="utf-8")
        checks = []
        with mock.patch.object(preflight.sys, "platform", "linux"), \
                mock.patch("src.process_runner.subprocess.Popen") as launched:
            preflight._target_access(str(target), [], checks)
        launched.assert_not_called()
        self.assertEqual(checks[-1]["state"], "error")
        self.assertIn("Windows batch files require Windows", checks[-1]["message"])

    def test_invalid_settings_are_structured_and_quiet_output_keeps_warnings(self) -> None:
        code, text = self._run("--interval", "1s", "--window", "1s", "--json")
        self.assertEqual(code, 2)
        self.assertIsNone(json.loads(text)["settings"])
        code, text = self._run("--plain", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("WARNING", text)
        self.assertIn("RESULT READY", text)
        self.assertNotIn("thresholds", text)
        self.assertNotIn("\x1b", text)


if __name__ == "__main__":
    unittest.main()
