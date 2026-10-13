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
