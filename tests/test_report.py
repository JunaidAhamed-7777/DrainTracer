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


if __name__ == "__main__":
    unittest.main()
