"""Server-side privacy and transactionally bounded report authority."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from src.report_contract import parse_report
from src.team_models import ProjectPolicy, StoreLimits, TeamError
from src.team_store import TeamStore

SECRET = "UntrustedPrivatePath98765"


def report_bytes(**extra):
    return json.dumps({"report_schema": 3, "target_path": SECRET, "report_id": SECRET,
                       "snapshots": [{"timestamp": 1700000000, "processes": [{"pid": 89741, "name": SECRET,
                         "private_bytes": 100, "create_time": 1699999999}], "totals": {"private_bytes": 100}}],
                       "arbitrary_extension": {"target_path": SECRET}, "privacy": {"profile": "already-safe"},
                       **extra}).encode()


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None,
                                                prefix=".draintracer-team-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.clock = [1700000000.0]
        self.store = TeamStore.create(Path(self.temp.name) / "store", _clock=lambda: self.clock[0])
        self.addCleanup(self.store.close)
        initial = self.store.bootstrap_project("evidence")
        self.project = initial["project_id"]
        self.admin = self.store.authenticate(initial["token"], self.project)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(TeamError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_ingest_always_redacts_before_persistence_and_detaches_read_values(self):
        metadata = self.store.ingest(self.admin, report_bytes())
        self.assertEqual(len(metadata["report_id"]), 32)
        self.assertEqual(metadata["privacy_profile"], "share")
        self.assertNotIn(SECRET, json.dumps(metadata))
        response = self.store.read_report(self.admin, metadata["report_id"])
        self.assertEqual(response["report"]["privacy"]["profile"], "share")
        self.assertNotIn(SECRET, self.store.export_report(self.admin, metadata["report_id"]).decode())
        self.assertNotIn(SECRET.encode(), self.store.path.read_bytes())
        response["report"]["target_path"] = "changed"
        self.assertEqual(self.store.read_report(self.admin, metadata["report_id"])["report"]["target_path"], "shared-target")
        parse_report(self.store.export_report(self.admin, metadata["report_id"]))

    def test_role_scope_and_authentication_are_checked_before_parsing_and_again_at_commit(self):
        issued = self.store.issue_credential(self.admin, "viewer")
        viewer = self.store.authenticate(issued["token"], self.project)
        with patch("src.team_evidence.parse_report", side_effect=AssertionError("must not decode")):
            self.assert_code("forbidden", self.store.ingest, viewer, report_bytes())
        other = self.store.bootstrap_project("other")
        other_actor = self.store.authenticate(other["token"], other["project_id"])
        metadata = self.store.ingest(self.admin, report_bytes())
        self.assert_code("not_found", self.store.read_report, other_actor, metadata["report_id"])
        self.assert_code("not_found", self.store.delete_report, other_actor, metadata["report_id"])
        from src.team_evidence import parse_report as original
        def revoke_then_parse(raw):
            self.store.revoke_credential(self.admin, issued["credential"]["id"])
            return original(raw)
        issued = self.store.issue_credential(self.admin, "operator")
        operator = self.store.authenticate(issued["token"], self.project)
        with patch("src.team_evidence.parse_report", side_effect=revoke_then_parse):
            self.assert_code("unauthorized", self.store.ingest, operator, report_bytes())
        self.assertEqual(len(self.store.list_reports(self.admin)["reports"]), 1)

    def test_malformed_and_oversized_uploads_leave_no_report_or_retry_authority(self):
        invalid = [b"{", b'{"snapshots":[],"snapshots":[]}', b'{"snapshots":[],"x":NaN}',
                   b'{"report_schema":999,"snapshots":[]}', bytearray(b"{}"), report_bytes() * 20000]
        for raw in invalid:
            with self.subTest(type=type(raw), size=len(raw)):
                self.assert_code("invalid", self.store.ingest, self.admin, raw, request_id=uuid4().hex)
        self.assertEqual(self.store.list_reports(self.admin)["reports"], [])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_idempotent_lost_ack_retry_conflicts_on_changes_or_deleted_target(self):
        request_id = uuid4().hex
        metadata = self.store.ingest(self.admin, report_bytes(), request_id=request_id)
        replay = self.store.ingest(self.admin, report_bytes(), request_id=request_id)
        self.assertTrue(replay["replayed"])
        self.assertEqual(metadata["report_id"], replay["report_id"])
        self.assert_code("conflict", self.store.ingest, self.admin, report_bytes(detection_ran=True), request_id=request_id)
        self.store.delete_report(self.admin, metadata["report_id"])
        self.assert_code("conflict", self.store.ingest, self.admin, report_bytes(), request_id=request_id)
        self.assertEqual(self.store.list_reports(self.admin)["reports"], [])
        self.assertNotIn(SECRET.encode(), self.store.path.read_bytes())

    def test_retention_is_irreversible_once_observed_even_after_wall_clock_rollback(self):
        self.store.update_policy(self.admin, ProjectPolicy(retention_seconds=60))
        request_id = uuid4().hex
        metadata = self.store.ingest(self.admin, report_bytes(), request_id=request_id)
        self.clock[0] += 61
        self.assert_code("not_found", self.store.read_report, self.admin, metadata["report_id"])
        self.clock[0] -= 61
        self.assert_code("not_found", self.store.read_report, self.admin, metadata["report_id"])
        self.assert_code("conflict", self.store.ingest, self.admin, report_bytes(), request_id=request_id)

    def test_transaction_rolls_back_evidence_and_retry_if_final_audit_fails(self):
        with patch.object(self.store, "_audit", side_effect=OSError("disk fault")), self.assertRaises(OSError):
            self.store.ingest(self.admin, report_bytes(), request_id=uuid4().hex)
        self.assertEqual(self.store.list_reports(self.admin)["reports"], [])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_concurrent_admission_and_request_ledger_are_bounded(self):
        self.store.update_policy(self.admin, ProjectPolicy(max_reports=1))
        def upload(_):
            try:
                return self.store.ingest(self.admin, report_bytes())["report_id"]
            except TeamError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(upload, range(2)))
        self.assertEqual(results.count("quota"), 1)
        self.assertEqual(len(self.store.list_reports(self.admin)["reports"]), 1)
        limited = TeamStore.create(Path(self.temp.name) / "retry-limit", limits=StoreLimits(max_requests=1))
        self.addCleanup(limited.close)
        initial = limited.bootstrap_project("retry-bounds")
        actor = limited.authenticate(initial["token"], initial["project_id"])
        metadata = limited.ingest(actor, report_bytes(), request_id=uuid4().hex)
        limited.delete_report(actor, metadata["report_id"])
        self.assert_code("quota", limited.ingest, actor, report_bytes(), request_id=uuid4().hex)

    def test_corrupt_accounting_cannot_subsidize_project_or_global_admission(self):
        metadata = self.store.ingest(self.admin, report_bytes())
        self.store.update_policy(self.admin, ProjectPolicy(max_bytes=4096))
        with self.store.connection() as connection:
            connection.execute("UPDATE reports SET stored_bytes=-100000 WHERE id=?", (metadata["report_id"],))
        self.assert_code("storage", self.store.ingest, self.admin, report_bytes())
        with self.store.connection() as connection:
            connection.execute("UPDATE reports SET stored_bytes=length(payload)+length(summary)")
        backup = self.store.backup(self.admin)
        with self.store.connection() as connection:
            connection.execute("UPDATE backups SET stored_bytes=1 WHERE id=?", (backup["backup_id"],))
        self.assert_code("storage", self.store.ingest, self.admin, report_bytes())
        self.assertEqual(len(self.store.list_reports(self.admin)["reports"]), 1)

    def test_policy_rejects_unknown_fields_and_tightens_existing_privacy_without_extending_expiry(self):
        metadata = self.store.ingest(self.admin, report_bytes())
        self.assert_code("invalid", self.store.update_policy, self.admin, {"arbitrary": True})
        self.store.update_policy(self.admin, ProjectPolicy(privacy_profile="minimal", retention_seconds=60))
        changed = self.store.read_report(self.admin, metadata["report_id"])
        self.assertEqual(changed["privacy_profile"], "minimal")
        process = changed["report"]["snapshots"][0]["processes"][0]
        self.assertNotIn("private_bytes", process)
        self.store.update_policy(self.admin, ProjectPolicy(retention_seconds=7 * 86400))
        self.assertEqual(changed["expires_at"], self.store.read_report(self.admin, metadata["report_id"])["expires_at"])


if __name__ == "__main__":
    unittest.main()
