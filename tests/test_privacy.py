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
