"""Report identity, timestamps, and atomic storage."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from helpers import make_alert, make_snapshot

from src.report import (
    iso_timestamp,
    new_report_id,
    report_directory,
    report_filename,
    snapshot_to_dict,
    write_json_report,
)


class ReportIdentityTests(unittest.TestCase):
    def test_ids_are_unique_uuid4_strings(self) -> None:
        ids = {new_report_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for report_id in ids:
            self.assertEqual(uuid.UUID(report_id).version, 4)

    def test_filename_embeds_id(self) -> None:
        self.assertEqual(report_filename("abc"), "leak_report_abc.json")
        self.assertEqual(report_filename("abc", ".html"), "leak_report_abc.html")

    def test_iso_timestamp_round_trips_with_offset(self) -> None:
        stamp = iso_timestamp(1_700_000_000.25)
        parsed = datetime.fromisoformat(stamp)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertAlmostEqual(parsed.timestamp(), 1_700_000_000.25, places=3)


class ReportDirectoryTests(unittest.TestCase):
    def test_groups_by_filename_and_recorded_offset_date(self) -> None:
        report = {
            "target_path": "C:\\targets\\worker.exe",
            "created_at": "2026-10-21T00:15:00.000+14:00",
        }
        self.assertEqual(report_directory(report, "reports"),
                         Path("reports") / "worker.exe_2026-10-21")

    def test_handles_posix_paths_unicode_and_custom_roots(self) -> None:
        report = {"target_path": "/work/日本 worker.py", "created_at": "2026-10-21T12:00:00+00:00"}
        self.assertEqual(report_directory(report, Path("custom") / "out"),
                         Path("custom") / "out" / "日本 worker.py_2026-10-21")

    def test_sanitizes_windows_invalid_names_and_bounds_component_length(self) -> None:
        for filename in ('bad<>:"|?*\x01\udc80.exe. ', "x" * 300 + ".exe", "CON"):
            with self.subTest(filename=filename):
                report = {"target_path": filename, "created_at": "2026-10-21T12:00:00+05:30"}
                path = report_directory(report, "reports")
                self.assertEqual(path.parent, Path("reports"))
                self.assertLessEqual(len(path.name), 111)
                self.assertTrue(path.name.endswith("_2026-10-21"))
                self.assertFalse(any(character in '<>:"/\\|?*' for character in path.name))
                self.assertTrue(path.name.isprintable())

    def test_rejects_missing_or_invalid_metadata_instead_of_making_placeholders(self) -> None:
        base = {"target_path": "worker.exe", "created_at": "2026-10-21T12:00:00+05:30"}
        for override in ({"target_path": ""}, {"target_path": ".."}, {"target_path": None},
                         {"created_at": None}, {"created_at": "not a date"},
                         {"created_at": "2026-10-21T12:00:00"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                report_directory(base | override, "reports")

    def test_reserved_windows_device_names_with_extensions_are_safe(self) -> None:
        for filename in ("CON.exe", "con.txt", "LPT1.log", "COM9.exe", "COM¹.exe", "CONIN$.exe"):
            with self.subTest(filename=filename):
                report = {"target_path": filename, "created_at": "2026-10-21T12:00:00+05:30"}
                self.assertEqual(report_directory(report, "reports").name,
                                 f"_{filename}_2026-10-21")


class ReportStorageTests(unittest.TestCase):
    def test_write_creates_directory_and_named_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "nested" / "leak_reports"
            report = {"report_id": new_report_id(), "snapshots": []}
            path = write_json_report(report, directory)
            self.assertEqual(path.name, f"leak_report_{report['report_id']}.json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
            self.assertEqual(sorted(p.name for p in directory.iterdir()), [path.name])

    def test_failed_write_leaves_no_temporary_file(self) -> None:
        from unittest import mock

        from src.report import write_text_atomic

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "report.json"
            with mock.patch("src.report.os.replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    write_text_atomic(target, "{}")
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_exact_newlines_are_preserved_on_request(self) -> None:
        from src.report import write_text_atomic

        with tempfile.TemporaryDirectory() as temp:
            target = write_text_atomic(Path(temp) / "page.html", "a\nb\n", newline="\n")
            self.assertEqual(target.read_bytes(), b"a\nb\n")

    def test_reports_are_strict_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = {
                "report_id": "abc",
                "snapshots": [{"cpu": float("nan"), "values": (1.0, float("inf"))}],
                "path": Path("C:/x"),
            }
            path = write_json_report(report, temp)
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        loaded = json.loads(text)
        self.assertEqual(loaded["snapshots"][0], {"cpu": None, "values": [1.0, None]})
        self.assertEqual(Path(loaded["path"]), Path("C:/x"))

    def test_write_requires_an_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                write_json_report({"snapshots": []}, temp)

    def test_snapshot_dict_is_json_safe(self) -> None:
        data = snapshot_to_dict(make_snapshot(alerts=(make_alert(),)))
        encoded = json.loads(json.dumps(data))
        self.assertEqual(encoded["severity"], "high")
        self.assertEqual(encoded["alerts"][0]["resource"], "memory")
        self.assertEqual(encoded["alerts"][0]["metric"], "private_bytes")


class SnapshotCoverageTests(unittest.TestCase):
    def test_coverage_and_evidence_are_serialized(self) -> None:
        from dataclasses import replace

        from helpers import make_denied, make_process
        from src.events import EvidenceState, LeakResource, ResourceEvidence

        snapshot = replace(
            make_snapshot(processes=(make_process(1), make_denied(2)), measured_coverage=True),
            evidence=(ResourceEvidence(LeakResource.MEMORY, EvidenceState.COLLECTING,
                                       valid_samples=3, elapsed_seconds=1.23456),),
        )
        encoded = json.loads(json.dumps(snapshot_to_dict(snapshot)))
        self.assertEqual(encoded["coverage"]["handles"],
                         {"measured": 1, "total": 2, "unmeasured_pids": [2]})
        self.assertEqual(encoded["evidence"]["memory"]["state"], "collecting")
        self.assertEqual(encoded["evidence"]["memory"]["elapsed_seconds"], 1.235)
        self.assertIn("handles", encoded["processes"][1]["unmeasured"])
        self.assertIsNone(snapshot_to_dict(make_snapshot())["coverage"])


if __name__ == "__main__":
    unittest.main()
