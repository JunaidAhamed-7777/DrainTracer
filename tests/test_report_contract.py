"""Public report schemas, bounded ingestion, and dependency-free machine output."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.report_contract import (
    ReportContractError,
    main,
    read_report,
    report_schema,
    session_summary,
    summary_schema,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "reports"


class CompatibilityTests(unittest.TestCase):
    def test_supported_fixtures_and_published_schemas_agree(self) -> None:
        for version in (1, 2, 3):
            with self.subTest(version=version):
                report = read_report(FIXTURES / f"v{version}.json")
                self.assertEqual(session_summary(report)["report_schema"], version)
        for name, schema in (("report", report_schema()), ("session-summary", summary_schema())):
            self.assertEqual(json.loads((ROOT / "schemas" / f"{name}.json").read_text()), schema)

    def test_schemas_validate_with_an_external_json_schema_consumer(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("optional external JSON Schema validator is unavailable")
        for version in (1, 2, 3):
            report = read_report(FIXTURES / f"v{version}.json")
            Draft202012Validator(report_schema()).validate(report)
            Draft202012Validator(summary_schema()).validate(session_summary(report))

    def test_legacy_unknowns_remain_unknown_and_zero_is_measured(self) -> None:
        summary = session_summary(read_report(FIXTURES / "v1.json"))
        self.assertIsNone(summary["measurement"]["detection_ran"])
        self.assertIsNone(summary["measurement"]["partial_coverage"])
        self.assertIsNone(summary["counts"]["observed_samples"])
        self.assertEqual(summary["peaks"]["handles"]["value"], 0)
        self.assertEqual(summary["peaks"]["handles"]["basis"], "retained_snapshots")
        self.assertIsNone(summary["peaks"]["private_bytes"]["value"])

    def test_exact_facts_and_unavailable_counters_keep_their_meaning(self) -> None:
        report = read_report(FIXTURES / "v3.json")
        summary = session_summary(report)
        self.assertEqual(summary["peaks"]["private_bytes"]["value"], 600000000)
        self.assertEqual(summary["peaks"]["private_bytes"]["basis"], "session_facts")
        self.assertIsNone(summary["peaks"]["handles"]["value"])
        self.assertEqual(summary["worst_severity"], "high")
        self.assertTrue(summary["measurement"]["partial_coverage"])

    def test_new_optional_fields_are_preserved(self) -> None:
        report = {"snapshots": [], "future_optional": {"known": False}}
        self.assertIs(validate_report(report), report)

    def test_observed_count_includes_paused_and_failed_samples(self) -> None:
        counts = {"collected": 5, "paused": 3, "failed": 2}
        for fields in (counts, {**counts, "taken": 10}):
            with self.subTest(fields=fields):
                report = {"snapshots": [], "session_facts": {"samples": fields}}
                self.assertEqual(session_summary(report)["counts"]["observed_samples"], 10)


class IngestionTests(unittest.TestCase):
    def test_unsupported_versions_and_malformed_shapes_are_rejected(self) -> None:
        for version in (0, 4, True, "3"):
            with self.subTest(version=version), self.assertRaisesRegex(ReportContractError, "unsupported report_schema"):
                validate_report({"report_schema": version, "snapshots": []})
        for report in ([], {}, {"snapshots": "bad"}, {"snapshots": [{"timestamp": "soon"}]},
                       {"snapshots": [{"timestamp": 1, "processes": "bad"}]},
                       {"snapshots": [{"timestamp": 1, "totals": {"handles": True}}]}):
            with self.subTest(report=report), self.assertRaises(ReportContractError):
                validate_report(report)

    def test_oversize_and_depth_are_bounded_before_json_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            path.write_text(" " * 129)
            with mock.patch("src.report_contract.MAX_REPORT_BYTES", 128), \
                    self.assertRaisesRegex(ReportContractError, "byte limit"):
                read_report(path)
            path.write_text("[" * 1000 + "]" * 1000)
            with mock.patch("src.report_contract.json.loads") as decode, \
                    self.assertRaisesRegex(ReportContractError, "nested too deeply"):
                read_report(path)
            decode.assert_not_called()

    def test_quoted_brackets_do_not_count_as_json_nesting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            value = {"snapshots": [], "label": '\\"' + "[" * 1000}
            path.write_text(json.dumps(value))
            self.assertEqual(read_report(path), value)

    def test_nonfinite_and_excessive_decoded_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ReportContractError, "non-finite"):
            validate_report({"snapshots": [], "unknown": float("nan")})
        with mock.patch("src.report_contract.MAX_REPORT_VALUES", 3), \
                self.assertRaisesRegex(ReportContractError, "value-count"):
            validate_report({"snapshots": [], "unknown": [1, 2, 3]})

    def test_oversize_decoded_containers_are_rejected_before_expansion(self) -> None:
        class UnexpandableList(list):
            def __iter__(self):
                raise AssertionError("over-limit container was expanded")

        with mock.patch("src.report_contract.MAX_REPORT_VALUES", 3), \
                self.assertRaisesRegex(ReportContractError, "value-count"):
            validate_report(UnexpandableList([0, 0, 0, 0]))

    def test_token_count_is_bounded_before_decoding_and_duplicate_keys_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            path.write_text('{"snapshots": [], "extra": [1, 2, 3, 4]}')
            with mock.patch("src.report_contract.MAX_REPORT_VALUES", 3), \
                    mock.patch("src.report_contract.json.loads") as decode, \
                    self.assertRaisesRegex(ReportContractError, "token limit"):
                read_report(path)
            decode.assert_not_called()
            path.write_text('{"snapshots": [], "report_schema": 3, "report_schema": 1}')
            with self.assertRaisesRegex(ReportContractError, "duplicate JSON object key"):
                read_report(path)

    def test_current_session_report_validates_and_keeps_observed_signals(self) -> None:
        from helpers import make_alert, make_console, make_snapshot

        from src.session import MonitorSession

        class ContractSession(MonitorSession):
            VERSION = "v5.4.0"

        session = ContractSession("worker.exe", console=make_console())
        session._accept_snapshot(make_snapshot(alerts=(make_alert(),)))
        report = json.loads(json.dumps(session.build_report(), default=str))
        self.assertEqual(session_summary(report)["worst_severity"], "high")
        report["session_facts"]["signals"] = {"memory": {"worst": "none"}}
        self.assertEqual(session_summary(report)["worst_severity"], "high")

    def test_diff_loader_rejects_future_schema_versions(self) -> None:
        from src.report_diff import DiffError, load_report

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "future.json"
            path.write_text('{"report_schema": 4, "snapshots": [{"timestamp": 1}]}')
            with self.assertRaisesRegex(DiffError, "unsupported report_schema"):
                load_report(path)


class MachineCommandTests(unittest.TestCase):
    def test_summary_runs_without_site_packages_or_rich(self) -> None:
        result = subprocess.run([sys.executable, "-S", "-m", "src.report_contract", "summary",
                                 str(FIXTURES / "v3.json")], cwd=ROOT, capture_output=True,
                                text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["summary_schema"], 1)
        self.assertEqual(len(result.stdout.splitlines()), 1)

    def test_machine_errors_use_stderr_and_leave_stdout_empty(self) -> None:
        import contextlib
        import io

        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(["validate", str(FIXTURES / "missing.json")])
        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "invalid_report")


if __name__ == "__main__":
    unittest.main()
