"""Seeded private data must never survive either privacy export profile."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from helpers import make_alert, make_console, make_process, make_snapshot

from src.privacy import export_private_report, preview_json, redact_report
from src.report_contract import main, session_summary
from src.session import MonitorSession

SECRET = "SensitiveSeedABC987"


def private_fixture() -> dict:
    from dataclasses import replace

    class PrivateSession(MonitorSession):
        VERSION = "v5.4.0"

    session = PrivateSession(f"C:/Users/{SECRET}/worker.exe", console=make_console())
    for index in range(2):
        process = replace(make_process(543219, private=(10 + index) * 1024**2),
                          name=SECRET, create_time=1700000123.5)
        session._accept_snapshot(make_snapshot(timestamp=1700001000 + index, processes=(process,),
                                alerts=(make_alert(),), measured_coverage=True))
    report = json.loads(json.dumps(session.build_report(), default=str))
    report["created_at"] = "2023-11-14T22:30:00+00:00"
    report["session_started_at"] = "2023-11-14T22:30:00+00:00"
    report["session_ended_at"] = "2023-11-14T22:30:02+00:00"
    report["settings"].update(config_path=SECRET, profile=SECRET, unknown=SECRET)
    report["experiment"] = {"workload_id": SECRET, "build": SECRET, "note": SECRET,
                            "arguments": {"count": 1, "fingerprint": SECRET},
                            "environment": {"os": "Windows", "host": SECRET, "user": SECRET}}
    report["event_log"] = [{"timestamp": 1700001000, "level": "info", "message": SECRET}]
    report["outcome"]["monitor_errors"] = [SECRET]
    report["markers"] = [{"timestamp": 1700001001, "index": 1, "label": SECRET}]
    report["captured_logs"] = SECRET
    report[SECRET] = SECRET
    return report


class PrivacyTests(unittest.TestCase):
    def test_all_current_evidence_states_survive_the_typed_profile(self) -> None:
        from src.events import EvidenceState

        for state in EvidenceState:
            with self.subTest(state=state.value):
                report = private_fixture()
                report["snapshots"][0]["evidence"] = {"memory": {"state": state.value}}
                self.assertEqual(redact_report(report)["snapshots"][0]["evidence"]["memory"]["state"],
                                 state.value)
    def test_numeric_identifiers_in_opaque_and_free_text_contexts_are_removed(self) -> None:
        from src.investigation import investigation_summary

        seeds = (987654321, 1122334455, 9988776655, 6677889900)
        report = private_fixture()
        report["metadata"] = {"value": seeds[0], "operations": seeds[1], "timestamp": 1}
        report["snapshots"][0]["metadata"] = {"value": seeds[0], "operations": seeds[1], "timestamp": 1}
        report["markers"][0]["label"] = seeds[2]
        report["markers"][0]["index"] = seeds[3]
        report["event_log"][0]["message"] = {"value": seeds[3]}
        report["outcome"]["measurement"]["reason"] = [seeds[0], seeds[1]]
        report["outcome"]["target"]["description"] = seeds[2]
        report["experiment"]["environment"]["os"] = seeds[3]
        report["settings"]["config_path"] = {"value": seeds[0]}
        for profile in ("share", "minimal"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temp:
                transformed = redact_report(report, profile)
                self.assertNotIn("metadata", transformed)
                self.assertEqual(transformed["snapshots"][0]["metadata"], {})
                self.assertEqual(transformed["snapshots"][0]["timestamp"], 946684800)
                paths = export_private_report(report, temp, profile)
                outputs = [json.dumps(transformed), investigation_summary(report, profile=profile)["text"],
                           *(path.read_text(encoding="utf-8") for path in paths)]
                for output in outputs:
                    for seed in seeds:
                        self.assertNotIn(str(seed), output)

    def test_ownership_and_recovery_limits_survive_without_checkpoint_identities(self) -> None:
        report = private_fixture()
        report["checkpoint"] = {"monitor_pid": 987654321, "session_uuid": SECRET, "digest": SECRET}
        report["recovery"] = {"partial": True, "final_outcome_confirmed": False,
                              "exact_through_cutoff": True, "monitor_pid": 987654321, "session_uuid": SECRET}
        for mode in ("launch", "attach"):
            with self.subTest(mode=mode):
                report["mode"] = mode
                report["experiment"]["mode"] = mode
                transformed = redact_report(report)
                self.assertEqual(transformed["mode"], mode)
                self.assertEqual(transformed["experiment"]["mode"], mode)
                self.assertEqual(transformed["recovery"], {"partial": True, "final_outcome_confirmed": False,
                                                           "exact_through_cutoff": True})
                self.assertNotIn("checkpoint", transformed)
                self.assertNotIn("987654321", json.dumps(transformed))
        report["recovery"] = {"partial": 987654321, "final_outcome_confirmed": None,
                              "exact_through_cutoff": SECRET}
        self.assertEqual(redact_report(report)["recovery"], {})

    def test_partial_cleanup_and_startup_evidence_survive_without_raw_diagnostics(self) -> None:
        report = private_fixture()
        report["outcome"]["startup"] = {"state": "failed", "error": {"value": 9988776655}}
        report["outcome"]["cleanup"] = {"state": "incomplete", "ownership": "attached",
            "attempted_actions": {"detach": 1, "terminate": 0, "kill": 0}, "remaining_count": 1,
            "remaining_processes": [{"pid": 543219, "create_time": 1700000123.5, "resume_required": True}],
            "omitted_processes": 0, "resume_required": True, "errors": [SECRET, 9988776655]}
        transformed = redact_report(report)
        cleanup = transformed["outcome"]["cleanup"]
        self.assertEqual(cleanup["state"], "incomplete")
        self.assertTrue(cleanup["resume_required"])
        self.assertEqual(cleanup["remaining_count"], 1)
        self.assertEqual(cleanup["remaining_processes"][0]["pid"], transformed["snapshots"][0]["processes"][0]["pid"])
        self.assertNotIn(SECRET, json.dumps(transformed))
        self.assertNotIn("9988776655", json.dumps(transformed))
        self.assertNotIn("543219", json.dumps(transformed))
    def test_profiles_remove_seeded_secrets_and_identifiers_without_mutating_input(self) -> None:
        report = private_fixture()
        original = copy.deepcopy(report)
        for profile in ("share", "minimal"):
            with self.subTest(profile=profile):
                transformed = redact_report(report, profile)
                text = json.dumps(transformed)
                for private in (SECRET, "543219", "1700000123.5", "1700001000", "2023-11-14"):
                    self.assertNotIn(private, text)
                self.assertEqual(transformed["snapshots"][1]["timestamp"] -
                                 transformed["snapshots"][0]["timestamp"], 1)
                self.assertEqual(transformed["snapshots"][0]["processes"][0]["pid"],
                                 transformed["snapshots"][1]["processes"][0]["pid"])
                self.assertEqual(transformed["snapshots"][0]["processes"][0]["create_time"],
                                 transformed["snapshots"][1]["processes"][0]["create_time"])
                summary = session_summary(transformed)
                self.assertEqual(summary["worst_severity"], "high")
                self.assertEqual(transformed["session_facts"]["signals"]["memory"]["worst"], "high")
                self.assertEqual(summary["peaks"]["private_bytes"]["value"], 11 * 1024**2)
                self.assertEqual(summary["measurement"]["partial_coverage"], False)
        self.assertEqual(report, original)

    def test_minimal_profile_removes_event_and_attribution_detail(self) -> None:
        transformed = redact_report(private_fixture(), "minimal")
        self.assertNotIn("event_log", transformed)
        self.assertNotIn("markers", transformed)
        self.assertNotIn("processes", transformed)
        sample = transformed["snapshots"][0]
        self.assertNotIn("private_bytes", sample["processes"][0])
        self.assertIn("private_bytes", sample["totals"])
        self.assertNotIn("contributors", sample["alerts"][0])

    def test_wall_clock_shifting_preserves_offsets_and_reused_pid_identities(self) -> None:
        report = private_fixture()
        report["snapshots"][1]["processes"][0]["create_time"] += 1
        report["session_started_at"] = "2023-11-15T04:00:00+05:30"
        transformed = redact_report(report)
        first, second = [sample["processes"][0] for sample in transformed["snapshots"]]
        self.assertEqual(first["pid"], second["pid"])
        self.assertNotEqual(first["create_time"], second["create_time"])
        self.assertEqual(transformed["session_started_at"], "2000-01-01T00:00:00+00:00")

    def test_fact_only_reports_still_remove_original_wall_clock_dates(self) -> None:
        report = private_fixture()
        report["snapshots"] = []
        report["event_log"] = None
        transformed = redact_report(report)
        self.assertEqual(transformed["session_facts"]["first_sample_at"], 946684800)
        self.assertEqual(transformed["session_started_at"], "2000-01-01T00:00:00+00:00")
        self.assertNotIn("1700001000", json.dumps(transformed))

    def test_json_html_and_preview_are_all_redacted(self) -> None:
        report = private_fixture()
        for profile in ("share", "minimal"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temp:
                json_path, html_path = export_private_report(report, temp, profile)
                for text in (json_path.read_text(encoding="utf-8"),
                             html_path.read_text(encoding="utf-8"), preview_json(report, profile)):
                    self.assertNotIn(SECRET, text)
                    self.assertNotIn("543219", text)
                saved = json.loads(json_path.read_text(encoding="utf-8"))
                document = html_path.read_text(encoding="utf-8")
                self.assertIn(saved["report_id"], document)
                self.assertIn(f"{profile} · review before sharing", document)

    def test_transformed_identity_is_a_strict_comparison_caveat(self) -> None:
        from src.analysis import summarize
        from src.report_diff import NOT_COMPARABLE, LoadedReport, compare_reports

        report = redact_report(private_fixture())
        report["detection_ran"] = True
        loaded = LoadedReport(Path("shared.json"), report, summarize(report))
        comparison = compare_reports(loaded, loaded, strict=True)
        self.assertEqual(comparison.verdict, NOT_COMPARABLE)
        self.assertIn("privacy transformed", " | ".join(comparison.warnings))

    def test_cli_defaults_to_local_preview_and_only_writes_when_requested(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private.json"
            path.write_text(json.dumps(private_fixture()), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["redact", str(path)]), 0)
            self.assertNotIn(SECRET, output.getvalue())
            self.assertEqual(list(Path(temp).iterdir()), [path])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["redact", str(path), "--output-dir", str(Path(temp) / "shared")]), 0)
            self.assertEqual(len(list((Path(temp) / "shared").glob("*.json"))), 1)
            self.assertEqual(len(list((Path(temp) / "shared").glob("*.html"))), 1)


if __name__ == "__main__":
    unittest.main()
