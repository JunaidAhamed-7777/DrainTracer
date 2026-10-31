"""Typed client receipts cannot acquire stale, cross-project or wider authority."""

from __future__ import annotations

import unittest
from uuid import uuid4

import test_team_evidence as fixture

from src.team_models import ProjectPolicy, StoreLimits
from src.team_store import TeamStore


def approval(**changes):
    return {"record_schema": 1, "repository": "team/repo", "workload_id": "steady", "source_commit": "a" * 40,
            "baseline_revision": 1, "audit_head": "b" * 64, "note": "Reviewed bounded baseline", **changes}


def receipt(approval_id, **changes):
    value = approval()
    value.pop("note")
    return {**value, "source_commit": "c" * 40, "approval_id": approval_id, "outcome": "pass",
            "comparison_sha256": "d" * 64, "basis": "client-computed", "metrics": {
                "private_bytes": {"verdict": "unchanged", "before": 100, "after": 100, "unit": "bytes"}},
            "qualifications": ["Recorded evidence, not source-code proof"], **changes}


class GovernanceTests(unittest.TestCase):
    setUp = fixture.EvidenceTests.setUp
    assert_code = fixture.EvidenceTests.assert_code

    def test_roles_same_project_approval_binding_and_client_computed_claim_are_explicit(self):
        operator = self.store.issue_credential(self.admin, "operator")
        actor = self.store.authenticate(operator["token"], self.project)
        self.assert_code("forbidden", self.store.create_record, actor, "baseline_approval", approval())
        approved = self.store.create_record(self.admin, "baseline_approval", approval(), ttl_seconds=30)
        result = self.store.create_record(actor, "comparison_receipt", receipt(approved["record_id"]), ttl_seconds=300)
        self.assertEqual(result["expires_at"], approved["expires_at"])
        self.assertEqual(result["value"]["basis"], "client-computed")
        self.assert_code("invalid", self.store.create_record, actor, "comparison_receipt",
                         receipt(approved["record_id"], repository="different/repository"))
        other = self.store.bootstrap_project("other")
        other_actor = self.store.authenticate(other["token"], other["project_id"])
        self.assert_code("not_found", self.store.create_record, other_actor, "comparison_receipt", receipt(approved["record_id"]))
        self.assert_code("not_found", self.store.read_record, other_actor, approved["record_id"])

    def test_receipt_retry_is_idempotent_and_approval_delete_invalidates_retries_and_receipts(self):
        approved = self.store.create_record(self.admin, "baseline_approval", approval())
        request_id = uuid4().hex
        value = receipt(approved["record_id"])
        original = self.store.create_record(self.admin, "comparison_receipt", value, request_id=request_id)
        replay = self.store.create_record(self.admin, "comparison_receipt", value, request_id=request_id)
        self.assertEqual(original["record_id"], replay["record_id"])
        self.assertTrue(replay["replayed"])
        self.assert_code("conflict", self.store.create_record, self.admin, "comparison_receipt", {**value, "outcome": "failure"}, request_id=request_id)
        self.store.delete_record(self.admin, approved["record_id"])
        self.assert_code("not_found", self.store.read_record, self.admin, original["record_id"])
        self.assert_code("conflict", self.store.create_record, self.admin, "comparison_receipt", value, request_id=request_id)

    def test_linked_report_expiry_and_deletion_bound_governance_lifetime(self):
        report = self.store.ingest(self.admin, fixture.report_bytes())
        approved = self.store.create_record(self.admin, "baseline_approval", approval(), report_ids=[report["report_id"]])
        result = self.store.create_record(self.admin, "comparison_receipt", receipt(approved["record_id"]))
        self.assertEqual(approved["expires_at"], report["expires_at"])
        self.store.update_policy(self.admin, ProjectPolicy(retention_seconds=60))
        approved = self.store.read_record(self.admin, approved["record_id"])
        result = self.store.read_record(self.admin, result["record_id"])
        self.assertEqual(approved["expires_at"], result["expires_at"])
        self.store.delete_report(self.admin, report["report_id"])
        self.assertEqual(self.store.list_records(self.admin)["records"], [])

    def test_expired_approval_is_irreversible_and_record_pagination_returns_detached_values(self):
        first = self.store.create_record(self.admin, "baseline_approval", approval(), key="history", ttl_seconds=1)
        second = self.store.create_record(self.admin, "baseline_approval", approval(note="second"), key="history")
        page = self.store.list_records(self.admin, kind="baseline_approval", key="history", limit=1)
        self.assertIsNotNone(page["next_cursor"])
        other = self.store.list_records(self.admin, limit=1, cursor=page["next_cursor"])
        self.assertNotEqual(page["records"][0]["record_id"], other["records"][0]["record_id"])
        second["value"]["note"] = "changed"
        self.assertEqual(self.store.read_record(self.admin, second["record_id"])["value"]["note"], "second")
        self.clock[0] += 2
        self.assert_code("not_found", self.store.read_record, self.admin, first["record_id"])
        self.clock[0] -= 2
        self.assert_code("not_found", self.store.read_record, self.admin, first["record_id"])

    def test_corrupt_record_bytes_cannot_subsidize_evidence_admission(self):
        record = self.store.create_record(self.admin, "baseline_approval", approval())
        with self.store.connection() as connection:
            connection.execute("UPDATE project_records SET stored_bytes=-10000 WHERE id=?", (record["record_id"],))
        self.assert_code("storage", self.store.ingest, self.admin, fixture.report_bytes())
        self.assertEqual(self.store.list_reports(self.admin)["reports"], [])

    def test_malformed_values_linked_ids_and_record_quota_are_enforced_before_commit(self):
        self.assert_code("invalid", self.store.create_record, self.admin, "baseline_approval", approval(unknown="unsafe"))
        self.assert_code("invalid", self.store.create_record, self.admin, "baseline_approval", approval(), report_ids=["a" * 32, "a" * 32])
        self.assert_code("not_found", self.store.create_record, self.admin, "baseline_approval", approval(), report_ids=["a" * 32])
        limited = TeamStore.create(self.store.directory.parent / "limited-records", limits=StoreLimits(max_records=1))
        self.addCleanup(limited.close)
        initial = limited.bootstrap_project("limited")
        actor = limited.authenticate(initial["token"], initial["project_id"])
        limited.create_record(actor, "baseline_approval", approval())
        self.assert_code("quota", limited.create_record, actor, "baseline_approval", approval())
        self.assertEqual(len(limited.list_records(actor)["records"]), 1)
