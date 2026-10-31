"""Logical snapshots preserve current deletion, expiry and credential authority."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import test_team_evidence as fixture
from test_team_evidence import SECRET, report_bytes

from src.team_models import ProjectPolicy
from src.team_store import TeamStore


class BackupTests(unittest.TestCase):
    setUp = fixture.EvidenceTests.setUp
    assert_code = fixture.EvidenceTests.assert_code
    # Reuse only the real private-store fixture, not its report tests.
    def test_snapshot_repairs_payload_but_never_rolls_back_authority(self):
        metadata = self.store.ingest(self.admin, report_bytes())
        backup = self.store.backup(self.admin)
        self.assertNotIn(SECRET, json.dumps(backup))
        with self.store.connection() as connection:
            archive = connection.execute("SELECT payload FROM backups").fetchone()[0]
            self.assertNotIn(SECRET.encode(), archive)
            self.assertNotIn(b"dt1.", archive)
            connection.execute("UPDATE reports SET payload=NULL,summary=NULL WHERE id=?", (metadata["report_id"],))
        self.assert_code("storage", self.store.read_report, self.admin, metadata["report_id"])
        self.assertEqual(self.store.restore(self.admin, backup["backup_id"])["restored"], 1)
        self.assertEqual(self.store.read_report(self.admin, metadata["report_id"])["privacy_profile"], "share")
        self.assertEqual(self.store.restore(self.admin, backup["backup_id"])["report_ids"], [metadata["report_id"]])

    def test_delete_invalidates_containing_snapshot_and_replay_cannot_resurrect(self):
        metadata = self.store.ingest(self.admin, report_bytes())
        backup = self.store.backup(self.admin)
        self.store.delete_report(self.admin, metadata["report_id"])
        self.assertEqual(self.store.list_backups(self.admin)["backups"], [])
        self.assert_code("not_found", self.store.restore, self.admin, backup["backup_id"])
        self.assertEqual(self.store.list_reports(self.admin)["reports"], [])

    def test_missing_authority_fails_closed_even_with_a_valid_logical_snapshot(self):
        metadata = self.store.ingest(self.admin, report_bytes())
        backup = self.store.backup(self.admin)
        with self.store.connection() as connection:
            connection.execute("DELETE FROM reports WHERE id=?", (metadata["report_id"],))
        self.assert_code("storage", self.store.restore, self.admin, backup["backup_id"])
        self.assertEqual(self.store.list_reports(self.admin)["reports"], [])
        with self.store.connection() as connection:
            connection.execute("DELETE FROM metadata WHERE name='store_id'")
        self.assert_code("storage", self.store.restore, self.admin, backup["backup_id"])
        self.assert_code("storage", TeamStore.open, self.store.directory)

    def test_expiry_revocation_and_other_project_refuse_snapshot_operations(self):
        self.store.update_policy(self.admin, ProjectPolicy(backup_ttl_seconds=2))
        self.store.ingest(self.admin, report_bytes())
        backup = self.store.backup(self.admin)
        other = self.store.bootstrap_project("other")
        actor = self.store.authenticate(other["token"], other["project_id"])
        self.assert_code("not_found", self.store.restore, actor, backup["backup_id"])
        viewer = self.store.issue_credential(self.admin, "viewer")
        actor = self.store.authenticate(viewer["token"], self.project)
        self.assert_code("forbidden", self.store.backup, actor)
        admin = self.store.issue_credential(self.admin, "admin")
        actor = self.store.authenticate(admin["token"], self.project)
        self.store.revoke_credential(self.admin, admin["credential"]["id"])
        self.assert_code("unauthorized", self.store.restore, actor, backup["backup_id"])
        self.clock[0] += 3
        self.assert_code("not_found", self.store.restore, self.admin, backup["backup_id"])
        self.clock[0] -= 3
        self.assert_code("not_found", self.store.restore, self.admin, backup["backup_id"])

    def test_backup_quota_privacy_tightening_and_failed_restore_are_atomic(self):
        metadata = self.store.ingest(self.admin, report_bytes())
        self.store.update_policy(self.admin, ProjectPolicy(max_backups=1))
        backup = self.store.backup(self.admin)
        self.assert_code("quota", self.store.backup, self.admin)
        original = self.store.export_report(self.admin, metadata["report_id"])
        with patch.object(self.store, "_audit", side_effect=OSError("disk fault")), self.assertRaises(OSError):
            self.store.restore(self.admin, backup["backup_id"])
        self.assertEqual(self.store.export_report(self.admin, metadata["report_id"]), original)
        self.store.update_policy(self.admin, ProjectPolicy(privacy_profile="minimal"))
        self.assertEqual(self.store.list_backups(self.admin)["backups"], [])
        self.assert_code("not_found", self.store.restore, self.admin, backup["backup_id"])
