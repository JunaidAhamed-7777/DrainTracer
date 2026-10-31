"""Controlled approvals cannot rewrite evidence or bypass compatibility gates."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from helpers import make_console
from test_report_diff import _experiment, _run

from src.baseline_control import (
    BaselineError,
    BaselineEvidenceError,
    BaselineStore,
    load_runs,
    main,
)
from src.config import MonitorSettings
from src.diff_command import run_diff
from src.events import LeakSeverity

APPROVAL = {"approved_by": "reviewer", "reason": "Reviewed the repeated controlled workload",
            "source_commit": "a" * 40}


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "approved"

    def reports(self, prefix, steps=(0, 0), *, handles_step=0, budgets=None, change=None):
        paths = []
        thresholds = {resource.value: {"critical_delta": value.critical_delta, "critical_slope": value.critical_slope}
                      for resource, value in MonitorSettings().thresholds.items()}
        for index, step in enumerate(steps):
            experiment = _experiment(workload_id="controlled-import", environment={
                "python_version": "3.14.0", "python_implementation": "CPython", "draintracer_version": "v5.4.0",
                "private_memory_metric": "private_bytes", "handle_metric": "handles"})
            experiment["budgets"] = budgets or {}
            data = _run(step, handles_step=handles_step, report_schema=3, version="v5.4.0", mode="launch",
                        report_id=f"{prefix}-{index}", experiment=experiment,
                        outcome={"measurement": {"result": "conclusive", "full_coverage": True},
                                 "startup": {"state": "started", "error": None},
                                 "cleanup": {"state": "complete", "remaining_count": 0,
                                             "resume_required": False, "errors": []},
                                 "target": {"failed": False}, "monitor_errors": []})
            data["settings"]["thresholds"] = thresholds
            if change:
                change(data)
            path = self.root / f"{prefix}-{index}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            paths.append(path)
        return load_runs(paths)

    def approve(self, reports, **kwargs):
        with BaselineStore(self.store, create=True) as store:
            return store.promote(reports, **APPROVAL, **kwargs)

    def test_approval_copies_evidence_and_records_actual_actor_and_review_claim(self):
        reports = self.reports("a")
        result = self.approve(reports)
        reports[0].path.write_text("changed original", encoding="utf-8")
        with BaselineStore(self.store) as store:
            self.assertEqual(store.state["revision"], 1)
            self.assertEqual(len(store.reports()), 2)
            self.assertEqual(store.audit[0]["approved_by"], "reviewer")
            self.assertTrue(store.audit[0]["actor"].startswith(("uid:", "windows:")))
            self.assertEqual(store.audit[0]["digest"], result["audit_head"])

    def test_regression_reproducibly_fails_and_cannot_be_silently_promoted(self):
        self.approve(self.reports("a"))
        later = self.reports("b", (3, 3))
        with BaselineStore(self.store) as store:
            for _ in range(2):
                code, result = store.check(later, source_commit="b" * 40)
                self.assertEqual(code, 1)
                self.assertEqual(result["comparison"]["resources"]["memory"]["verdict"], "regressed")
            before = (self.store / "baseline.json").read_bytes()
            with self.assertRaisesRegex(BaselineError, "accept_regression"):
                store.promote(later, **APPROVAL)
            self.assertEqual((self.store / "baseline.json").read_bytes(), before)
            store.promote(later, **APPROVAL, accept_regression=True)
            self.assertEqual(store.state["revision"], 2)
            self.assertTrue(store.audit[-1]["details"]["accepted_regression"])

    def test_incompatible_or_incomplete_runs_never_replace_an_approval(self):
        self.approve(self.reports("a"))
        incompatible = self.reports("other", change=lambda data: data.update(target_path="different.exe"))
        with BaselineStore(self.store) as store:
            before = (self.store / "baseline.json").read_bytes()
            with self.assertRaises(BaselineError):
                store.promote(incompatible, **APPROVAL, accept_regression=True)
            self.assertEqual(store.check(incompatible, source_commit="b" * 40)[0], 3)
            self.assertEqual((self.store / "baseline.json").read_bytes(), before)
        with self.assertRaises(BaselineEvidenceError):
            self.reports("partial", change=lambda data: data["outcome"]["measurement"].update(full_coverage=False))
        with self.assertRaises(BaselineEvidenceError):
            self.reports("unknown", change=lambda data: data["experiment"].update(arguments=None))

    def test_report_budgets_default_and_explicit_metrics_override_them(self):
        self.approve(self.reports("a", budgets={"handles": 300, "threads": 100}))
        later = self.reports("b")
        with BaselineStore(self.store) as store:
            code, result = store.check(later, source_commit="b" * 40, budgets={"handles": 150})
            self.assertEqual(code, 4)
            self.assertEqual(result["budgets"], {"handles": 150, "threads": 100})
            self.assertEqual(store.check(later, source_commit="b" * 40, budgets={"handles": 500})[0], 0)

    def test_suppression_is_scoped_expiring_audited_and_invalidated_by_promotion(self):
        self.approve(self.reports("a"))
        later = self.reports("b", (3, 3))
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with BaselineStore(self.store) as store:
            store.suppress(resource="memory", kind="regression", expires=expiry, **APPROVAL)
            code, result = store.check(later, source_commit="b" * 40)
            self.assertEqual(code, 0)
            self.assertEqual(result["findings"][0]["suppressed_by"], [2])
            wrong = self.reports("c", (3, 3), change=lambda data: data.update(target_path="different.exe"))
            self.assertEqual(store.check(wrong, source_commit="b" * 40)[0], 3)
            store.promote(later, **APPROVAL, accept_regression=True)
            self.assertFalse(store.state["suppressions"])
            self.assertEqual(len(store.audit), 3)

    def test_expired_exceptions_stay_visible_and_do_not_waive_a_regression(self):
        self.approve(self.reports("a"))
        later = self.reports("b", (3, 3))
        now = datetime.now(timezone.utc)
        with BaselineStore(self.store) as store:
            for expiry in (now - timedelta(days=1), now + timedelta(days=91)):
                with self.assertRaises(BaselineError):
                    store.suppress(resource="memory", kind="regression", expires=expiry.isoformat(), **APPROVAL)
            store.suppress(resource="memory", kind="regression", expires=(now + timedelta(hours=1)).isoformat(), **APPROVAL)
            with mock.patch("src.baseline_control.datetime") as clock:
                clock.now.return_value = now + timedelta(days=2)
                clock.fromisoformat.side_effect = datetime.fromisoformat
                code, result = store.check(later, source_commit="b" * 40)
            self.assertEqual(code, 1)
            self.assertFalse(result["suppressions"][0]["active"])

    def test_failed_atomic_write_preserves_previous_approval(self):
        self.approve(self.reports("a"))
        later = self.reports("b")
        before = (self.store / "baseline.json").read_bytes()
        replace_file = os.replace
        def replace(source, destination):
            if Path(destination).name == "baseline.json":
                raise OSError("disk unavailable")
            return replace_file(source, destination)
        with BaselineStore(self.store) as store, mock.patch("src.baseline_control.os.replace", side_effect=replace):
            with self.assertRaises(OSError):
                store.promote(later, **APPROVAL)
            self.assertEqual(store.state["revision"], 1)
        self.assertEqual((self.store / "baseline.json").read_bytes(), before)
        with BaselineStore(self.store) as store:
            self.assertEqual(store.state["revision"], 1)
        self.assertFalse(list(self.store.glob(".baseline-*")))

    def test_crashed_mid_object_write_never_publishes_a_partial_hash_object_and_retry_succeeds(self):
        self.approve(self.reports("a"))
        later = self.reports("b")
        before = (self.store / "baseline.json").read_bytes()
        objects = set(self.store.glob("*.json"))
        script = r'''import os, sys, tempfile
from src.baseline_control import BaselineStore, load_runs
original = tempfile.NamedTemporaryFile
class CrashWrite:
    def __init__(self, handle): self.handle, self.name = handle, handle.name
    def __enter__(self): return self
    def __exit__(self, *args): return self.handle.__exit__(*args)
    def write(self, data):
        self.handle.write(data[:10]); self.handle.flush(); os.fsync(self.handle.fileno())
        os._exit(23)
def open_temporary(*args, **kwargs): return CrashWrite(original(*args, **kwargs))
tempfile.NamedTemporaryFile = open_temporary
with BaselineStore(sys.argv[1]) as store:
    store.promote(load_runs(sys.argv[2:]), approved_by='reviewer', reason='Interrupted object-write drill', source_commit='b'*40)
'''
        crashed = subprocess.run([sys.executable, "-c", script, str(self.store), *[str(item.path) for item in later]],
                                 check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(crashed.returncode, 23, crashed.stdout + crashed.stderr)
        self.assertEqual((self.store / "baseline.json").read_bytes(), before)
        self.assertEqual(set(self.store.glob("*.json")), objects)
        self.assertTrue(list(self.store.glob(".evidence-*")))
        with BaselineStore(self.store) as store:
            store.promote(later, **APPROVAL)
            self.assertEqual(store.state["revision"], 2)
            self.assertEqual(len(store.reports()), 2)

    def test_recorded_operational_failures_are_never_waived_by_an_approval(self):
        changes = [lambda data: data["outcome"]["startup"].update(state="failed"),
                   lambda data: data["outcome"]["cleanup"].update(state="incomplete"),
                   lambda data: data["outcome"]["cleanup"].update(resume_required=True),
                   lambda data: data["outcome"].update(monitor_errors=["sample failed"]),
                   lambda data: data["outcome"]["target"].update(failed=True),
                   lambda data: data.update(headless={"exit_code": 130}),
                   lambda data: data["outcome"].update(workload={"state": "failed"}),
                   lambda data: data.update(workload=None)]
        for index, change in enumerate(changes):
            with self.subTest(index=index), self.assertRaises(BaselineEvidenceError):
                self.reports(f"failed{index}", change=change)

    def test_initial_recorded_signals_need_explicit_acceptance(self):
        reports = self.reports("signal", change=lambda data: data.update(
            snapshots=_run(3, severity=LeakSeverity.HIGH)["snapshots"]))
        with self.assertRaisesRegex(BaselineError, "recorded signal"):
            self.approve(reports)
        self.assertEqual(self.approve(reports, accept_regression=True)["revision"], 1)

    def test_unknown_programmatic_budget_override_is_rejected(self):
        self.approve(self.reports("a"))
        with BaselineStore(self.store) as store, self.assertRaisesRegex(BaselineError, "overrides"):
            store.check(self.reports("b"), source_commit="b" * 40, budgets={"typo": 10})

    def test_completed_workload_requires_known_successful_readiness_steps_and_cleanup(self):
        def complete(data):
            data["outcome"]["workload"] = {"state": "complete"}
            data["workload"] = {"schema": 1, "outcome": {"state": "complete", "readiness": "ready",
                                  "exercise": [{"index": 1, "state": "complete", "exit_code": 0}],
                                  "exercise_cleanup": {"state": "complete", "remaining_count": 0}}}
        self.assertEqual(len(self.reports("success", change=complete)), 2)
        for field, value in (("readiness", "failed"), ("exercise_cleanup", None),
                             ("exercise", [{"state": "complete", "exit_code": False}])):
            def invalid(data, field=field, value=value):
                complete(data)
                data["workload"]["outcome"][field] = value
            with self.subTest(field=field), self.assertRaises(BaselineEvidenceError):
                self.reports(f"unknown-{field}", change=invalid)

    def test_held_writer_lock_prevents_a_second_writer(self):
        self.approve(self.reports("a"))
        with BaselineStore(self.store), self.assertRaises(OSError):
            BaselineStore(self.store)

    def test_damaged_content_or_audit_chain_is_rejected(self):
        self.approve(self.reports("a"))
        with BaselineStore(self.store) as store:
            path = self.store / f"{store.state['reports'][0]}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["report_id"] = "altered"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(BaselineError, "hash"):
                store.reports()
        manifest = self.store / "baseline.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["audit"][0]["reason"] = "changed reason"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(BaselineError, "chain"):
            BaselineStore(self.store)

    def test_group_bytes_are_bounded_before_parsing_and_duplicates_rejected(self):
        reports = self.reports("a")
        with mock.patch("src.baseline_control.MAX_GROUP_BYTES", 1), \
                mock.patch("src.baseline_control.load_report") as reader:
            with self.assertRaisesRegex(BaselineError, "128 MiB"):
                load_runs([item.path for item in reports])
            reader.assert_not_called()
        with self.assertRaises(BaselineError):
            load_runs([reports[0].path, reports[0].path])

    def test_cli_promotes_checks_and_prints_a_reviewable_audit(self):
        old, later = self.reports("a"), self.reports("b", (3, 3))
        runs = [item for report in old for item in ("--run", str(report.path))]
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            code = main(["promote", str(self.store), *runs, "--approved-by", "reviewer",
                         "--reason", "Reviewed steady import", "--source-commit", "a" * 40])
        self.assertEqual(code, 0, error.getvalue())
        self.assertEqual(json.loads(out.getvalue())["revision"], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["check", str(self.store), *[item for report in later for item in ("--run", str(report.path))],
                         "--source-commit", "b" * 40])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())["passed"])
        before = (self.store / "baseline.json").read_bytes()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["audit", str(self.store)]), 0)
        self.assertEqual(json.loads(out.getvalue())["audit"][0]["approved_by"], "reviewer")
        self.assertEqual((self.store / "baseline.json").read_bytes(), before)

    def test_cli_invalid_input_is_structured_stderr_and_partial_evidence_is_code_three(self):
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            self.assertEqual(main(["check", str(self.store)]), 2)
        self.assertFalse(out.getvalue())
        self.assertEqual(json.loads(error.getvalue())["exit_code"], 2)
        reports = self.reports("a")
        data = json.loads(reports[0].path.read_text(encoding="utf-8"))
        data["outcome"]["measurement"]["full_coverage"] = False
        reports[0].path.write_text(json.dumps(data), encoding="utf-8")
        error = io.StringIO()
        with redirect_stderr(error):
            code = main(["check", str(self.store), "--run", str(reports[0].path), "--run", str(reports[1].path),
                         "--source-commit", "b" * 40])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(error.getvalue())["exit_code"], 3)

    def test_diff_report_budget_opt_in_and_explicit_override(self):
        old, later = self.reports("a", budgets={"handles": 150}), self.reports("b")
        args = [str(old[0].path), str(later[0].path), "--json"]
        self.assertEqual(run_diff(args, make_console()), 0)
        output = make_console()
        self.assertEqual(run_diff([*args, "--report-budgets"], output), 4)
        self.assertTrue(json.loads(output.file.getvalue())["over_budget"])
        self.assertEqual(run_diff([*args, "--report-budgets", "--budget-handles", "300"], make_console()), 0)


if __name__ == "__main__":
    unittest.main()
