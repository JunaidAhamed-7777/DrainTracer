"""Real local recipes exercise readiness, ownership, bounded logs and provenance."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
import psutil
from rich.console import Console

from src.events import LeakSeverity
from src.workload import EXIT_WORKLOAD, RecipeError, WorkloadRunner, read_recipe

PYTHON = getattr(sys, "_base_executable", None) or sys.executable


class RecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.script = self.folder / "target.py"
        self.script.write_text("import time; time.sleep(60)", encoding="utf-8")
        self.path = self.folder / "recipe.json"
        self.data = {"schema": 1, "command": {"target": "target.py", "interpreter": PYTHON},
                     "timeout": 1.0}

    def recipe(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        return read_recipe(self.path)

    def runner(self) -> WorkloadRunner:
        return WorkloadRunner(self.recipe(), report_dir=self.folder / "reports",
                              console=Console(file=io.StringIO(), force_terminal=False))

    def test_checkpointed_recipe_writes_a_valid_bounded_prefix_without_monitor_errors(self) -> None:
        from src.checkpoints import CHECKPOINT_SAMPLES, read_checkpoint

        checkpoint = self.folder / "recipe-checkpoint.json"
        config = self.folder / "checkpoint.toml"
        config.write_text('sample_interval="100ms"\nwindows=["600ms"]\n'
                          'checkpoint="recipe-checkpoint.json"\ncheckpoint_interval="1s"\n', encoding="utf-8")
        self.data.update(timeout=1.3, config=config.name)
        runner = self.runner()
        self.assertEqual(runner.run(), 0)
        saved = read_checkpoint(checkpoint, app_version=runner.VERSION)
        self.assertLessEqual(len(saved["snapshots"]), CHECKPOINT_SAMPLES)
        self.assertGreater(saved["sample_count"], 0)
        self.assertEqual(saved["outcome"]["monitor_errors"], [])
        self.assertIn("workload", saved)
        self.assertFalse(runner.manager.has_tracked_processes)
        bounded = runner.build_report(snapshot_limit=2)
        self.assertLessEqual(len(bounded["snapshots"]), 2)
        self.assertEqual(bounded["outcome"]["workload"]["state"], "complete")

    def test_relative_paths_resolve_from_recipe_and_values_never_enter_provenance(self) -> None:
        self.data["environment"] = {"PRIVATE": "seeded-literal-value",
                                    "TOKEN": {"from_env": "WORKLOAD_TEST_SECRET"}}
        with mock.patch.dict(os.environ, {"WORKLOAD_TEST_SECRET": "seeded-parent-value"}):
            recipe = self.recipe()
        provenance = json.dumps(recipe.provenance())
        self.assertEqual(recipe.cwd, self.folder)
        self.assertEqual(recipe.command["target"], str(self.script))
        self.assertEqual(recipe.env["TOKEN"], "seeded-parent-value")
        self.assertNotIn("seeded-", provenance)
        self.assertIn('"name": "TOKEN"', provenance)
        self.assertNotIn("WORKLOAD_TEST_SECRET", provenance)
        self.assertFalse(recipe.environment["values_compared"])

    def test_invalid_or_unbounded_recipes_fail_without_echoing_payloads(self) -> None:
        cases = [{"timeout": 10**1000}, {"timeout": True}, {"timeout": float("inf")},
                 {"capture_logs": {"max_bytes": 1024**3}}, {"exercise": [{}] * 9},
                 {"unknown-secret-payload": "sentinel-secret"},
                 {"inherit_environment": "sentinel-secret"},
                 {"environment": {"BAD=sentinel-secret": "sentinel-secret"}},
                 {"environment": {"A": {"from_env": "MISSING_SENTINEL_SECRET_9823"}}}]
        for change in cases:
            with self.subTest(change=list(change)):
                original = dict(self.data)
                self.data.update(change)
                with self.assertRaises(RecipeError) as caught:
                    self.recipe()
                self.assertNotIn("sentinel-secret", str(caught.exception))
                self.data = original
        self.path.write_text('{"schema":1,"schema":1}', encoding="utf-8")
        with self.assertRaisesRegex(RecipeError, "Duplicate"):
            read_recipe(self.path)

    def test_filesystem_and_configuration_errors_are_payload_free(self) -> None:
        with self.assertRaises(RecipeError):
            read_recipe("invalid\0sentinel-secret")
        self.data["cwd"] = "invalid\0sentinel-secret"
        with self.assertRaises(RecipeError) as caught:
            self.recipe()
        self.assertNotIn("sentinel-secret", str(caught.exception))
        self.data.pop("cwd")
        (self.folder / "settings.toml").write_text('fail_on = "sentinel-secret"', encoding="utf-8")
        self.data["config"] = "settings.toml"
        with self.assertRaises(RecipeError) as caught:
            self.recipe()
        self.assertNotIn("sentinel-secret", str(caught.exception))

    def test_explicit_monitoring_profile_is_resolved_without_ambient_discovery(self) -> None:
        config = self.folder / ".drainrc.toml"
        config.write_text('[profiles.ci]\nfail_on = "medium"\nfail_on_inconclusive = true\n'
                          'windows = ["1s"]\nsample_interval = "100ms"\ntimeout = "20m"\n',
                          encoding="utf-8")
        self.assertIsNone(self.recipe().settings.source)
        self.data.update(config=".drainrc.toml", profile="ci")
        runner = self.runner()
        self.assertEqual(runner.settings.source, config)
        self.assertEqual(runner.settings.windows, (1.0,))
        self.assertEqual(runner.timeout, 1.0)
        self.assertEqual(runner.fail_on, LeakSeverity.MEDIUM)
        self.assertTrue(runner.fail_on_inconclusive)
        self.assertFalse(runner.recipe.provenance()["configuration"]["discovery"])

    def test_workload_failure_stays_distinct_from_an_observed_leak(self) -> None:
        runner = self.runner()
        runner.workflow["state"] = "failed"
        runner._worst_severity = LeakSeverity.HIGH
        self.assertTrue(runner.breached)
        self.assertEqual(runner.exit_status(), EXIT_WORKLOAD)

    def test_stale_marker_is_preserved_and_rechecked_before_launch(self) -> None:
        self.data["readiness"] = {"file": "ready", "timeout": 0.1}
        runner = self.runner()
        marker = self.folder / "ready"
        marker.write_text("unowned", encoding="utf-8")
        self.assertEqual(runner.run(), EXIT_WORKLOAD)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unowned")
        self.assertIsNone(runner.root_pid)
        with self.assertRaisesRegex(RecipeError, "already exists"):
            self.recipe()

    def test_ready_exercise_literal_arguments_and_capped_logs_run_without_console_input(self) -> None:
        self.script.write_text(
            "import os,sys,time\n"
            "from pathlib import Path\n"
            "Path('ready').write_text(repr(sys.stdin.read()))\n"
            "sys.stdout.write('token=pattern-secret ' + os.environ['PRIVATE'] + '\\n'); sys.stdout.flush()\n"
            "sys.stdout.write('x'*200000 + '\\n'); sys.stdout.flush()\n"
            "sys.stderr.write('stderr-line\\n'); sys.stderr.flush()\n"
            "time.sleep(60)\n", encoding="utf-8")
        (self.folder / "exercise.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            "Path('received.json').write_text(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        arguments = ["space argument", "x&y|z", "%PATH%", "!USERNAME!", ""]
        self.data.update(timeout=2, environment={"PRIVATE": "selected-secret"},
                         readiness={"file": "ready", "timeout": 1.5},
                         exercise=[{"target": "exercise.py", "interpreter": PYTHON, "args": arguments}],
                         capture_logs={"max_bytes": 1024})
        runner = self.runner()
        self.assertEqual(runner.run(), 0)
        self.assertEqual((self.folder / "ready").read_text(), "''")
        self.assertEqual(json.loads((self.folder / "received.json").read_text()), arguments)
        self.assertFalse(runner.manager.has_tracked_processes)
        record = json.loads(runner._last_report.read_text(encoding="utf-8"))
        self.assertEqual(record["workload"]["outcome"]["state"], "complete")
        self.assertEqual(record["workload"]["outcome"]["readiness"], "ready")
        self.assertNotIn("selected-secret", json.dumps(record))
        self.assertEqual(len(record["workload"]["logs"]), 4)
        self.assertTrue(any(log["truncated"] for log in record["workload"]["logs"]))
        for log in record["workload"]["logs"]:
            content = Path(log["path"]).read_bytes()
            self.assertLessEqual(len(content), 1024)
            self.assertNotIn(b"pattern-secret", content)
            self.assertNotIn(b"selected-secret", content)
            self.assertFalse(log["error"])
        target_out = next(log for log in record["workload"]["logs"] if log["path"].endswith("target-stdout.log"))
        self.assertIn(b"<redacted>", Path(target_out["path"]).read_bytes())

    def test_readiness_timeout_is_distinct_and_leaves_unrelated_process_alive(self) -> None:
        unrelated = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(60)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (unrelated.kill(), unrelated.wait()) if unrelated.poll() is None else None)
        self.data["readiness"] = {"file": "never-ready", "timeout": 0.2}
        runner = self.runner()
        self.assertEqual(runner.run(), EXIT_WORKLOAD)
        self.assertIsNone(unrelated.poll())
        self.assertFalse(runner.manager.has_tracked_processes)
        report = json.loads(runner._last_report.read_text(encoding="utf-8"))
        self.assertEqual(report["workload"]["outcome"]["readiness"], "failed")
        self.assertIn("Readiness timed out", report["workload"]["outcome"]["failure"])
        self.assertEqual(report["outcome"]["workload"]["state"], "failed")

        from src.privacy import redact_report
        from src.report_contract import session_summary

        shared = redact_report(report)
        self.assertNotIn("workload", shared)
        self.assertEqual(shared["outcome"]["workload"], {"state": "failed"})
        self.assertEqual(session_summary(shared)["workload_state"], "failed")
        self.assertIn("WORKLOAD FAILED", runner._last_html_report.read_text(encoding="utf-8"))

    def test_exercise_timeout_cleans_its_own_child_and_target(self) -> None:
        (self.folder / "exercise.py").write_text(
            "import subprocess,sys,time\nfrom pathlib import Path\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            "Path('child-pid').write_text(str(child.pid))\n"
            "time.sleep(60)\n", encoding="utf-8")
        self.data.update(timeout=5, exercise=[{"target": "exercise.py", "interpreter": PYTHON, "timeout": 1}])
        runner = self.runner()
        self.assertEqual(runner.run(), EXIT_WORKLOAD)
        pid = int((self.folder / "child-pid").read_text())
        if psutil.pid_exists(pid):
            self.assertEqual(psutil.Process(pid).status(), psutil.STATUS_ZOMBIE)
        self.assertFalse(runner.manager.has_tracked_processes)
        self.assertIsNone(runner._exercise_manager)
        self.assertEqual(runner.workflow["exercise_cleanup"]["remaining_count"], 0)
        self.assertEqual(runner.workflow["exercise"][0]["state"], "failed")

    def test_interruption_during_exercise_still_cleans_both_owned_trees(self) -> None:
        (self.folder / "exercise.py").write_text("import time; time.sleep(60)", encoding="utf-8")
        self.data["exercise"] = [{"target": "exercise.py", "interpreter": PYTHON}]
        runner = self.runner()
        with mock.patch.object(runner, "_tick", side_effect=KeyboardInterrupt):
            self.assertEqual(runner.run(), 130)
        self.assertFalse(runner.manager.has_tracked_processes)
        self.assertIsNone(runner._exercise_manager)
        self.assertEqual(runner.workflow["state"], "interrupted")

    def test_capture_startup_error_still_closes_pipes_and_owned_target(self) -> None:
        self.data["capture_logs"] = {"max_bytes": 1024}
        runner = self.runner()
        with mock.patch("src.workload.BoundedLog.start", side_effect=OSError("unavailable output")):
            self.assertEqual(runner.run(), 2)
        self.assertFalse(runner.manager.has_tracked_processes)
        self.assertTrue(all(stream.closed for stream in runner.manager.output_streams.values()))
        self.assertIsNotNone(runner._last_report)

    @unittest.skipUnless(sys.platform == "win32", "native Windows batch forwarding")
    def test_batch_recipe_retains_literals_and_selected_interpreter(self) -> None:
        script = self.folder / "literal worker.cmd"
        script.write_text('@echo off\r\nset "A1=%~1"\r\nset "A2=%~2"\r\n'
                          'setlocal EnableDelayedExpansion\r\n'
                          '> "%~dp0received.txt" echo(^[!A1!^] ^[!A2!^]\r\n', encoding="ascii")
        self.data["command"] = {"target": script.name, "args": ["%BUILD_TAG%", "!USERNAME!"]}
        self.data["environment"] = {"COMSPEC": os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe"),
                                    "BUILD_TAG": "expanded-value-must-not-appear"}
        runner = self.runner()
        self.assertEqual(runner.run(), 0)
        self.assertEqual((self.folder / "received.txt").read_text().strip(), "[%BUILD_TAG%] [!USERNAME!]")
        self.assertFalse(runner.manager.has_tracked_processes)


if __name__ == "__main__":
    unittest.main()
