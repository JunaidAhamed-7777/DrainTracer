"""Actual local credentials never carry stale or cross-project authority."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.team_models import StoreLimits, TeamError, _make_actor
from src.team_store import TeamStore


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None,
                                                prefix=".draintracer-team-auth-")
        self.addCleanup(self.temp.cleanup)
        self.clock = [1700000000.0]
        self.store = TeamStore.create(Path(self.temp.name) / "store", _clock=lambda: self.clock[0])
        self.addCleanup(self.store.close)
        self.initial = self.store.bootstrap_project("private-project")
        self.project = self.initial["project_id"]
        self.admin = self.store.authenticate(self.initial["token"], self.project)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(TeamError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_bootstrap_stores_only_digests_and_roles_require_current_database_authority(self):
        self.assertEqual(self.store.whoami(self.admin)["role"], "admin")
        issued = self.store.issue_credential(self.admin, "viewer")
        viewer = self.store.authenticate(issued["token"], self.project)
        self.assertEqual(self.store.whoami(viewer)["role"], "viewer")
        self.assert_code("forbidden", self.store.issue_credential, viewer, "admin")
        self.assertNotIn(self.initial["token"].encode(), self.store.path.read_bytes())
        self.assertNotIn(issued["token"], json.dumps(self.store.audit(self.admin)))
        self.assertNotIn(issued["token"], json.dumps(self.store.list_credentials(self.admin)))

    def test_cross_project_token_or_forged_actor_never_changes_scope(self):
        other = self.store.bootstrap_project("another-project")
        self.assert_code("unauthorized", self.store.authenticate, self.initial["token"], other["project_id"])
        forged = _make_actor(other["project_id"], self.admin.credential_id, self.initial["token"])
        self.assert_code("unauthorized", self.store.whoami, forged)
        self.assert_code("unauthorized", self.store.whoami, {"project_id": self.project, "role": "admin"})
        for project in (None, "日本", "../project", "x" * 100000):
            self.assert_code("unauthorized", self.store.authenticate, self.initial["token"], project)

    def test_revocation_and_expiry_apply_to_an_already_cached_actor(self):
        issued = self.store.issue_credential(self.admin, "operator", ttl_seconds=5)
        actor = self.store.authenticate(issued["token"], self.project)
        self.store.revoke_credential(self.admin, issued["credential"]["id"])
        self.assert_code("unauthorized", self.store.whoami, actor)
        issued = self.store.issue_credential(self.admin, "viewer", ttl_seconds=5)
        actor = self.store.authenticate(issued["token"], self.project)
        self.clock[0] += 6
        self.assert_code("unauthorized", self.store.whoami, actor)
        self.clock[0] -= 6
        self.assert_code("unauthorized", self.store.whoami, actor)

    def test_explicit_local_recovery_revokes_prior_keys_without_recreating_a_project(self):
        replacement = self.store.recover_admin(self.project)
        self.assert_code("unauthorized", self.store.whoami, self.admin)
        recovered = self.store.authenticate(replacement["token"], self.project)
        self.assertEqual(self.store.whoami(recovered)["role"], "admin")
        self.assert_code("not_found", self.store.recover_admin, "0" * 32)

    def test_credential_and_project_quotas_are_transactional_and_role_input_is_strict(self):
        with TeamStore.open(self.store.directory, _clock=lambda: self.clock[0]) as second:
            self.assertEqual(second.whoami(self.admin)["credential_id"], self.admin.credential_id)
        for role in (True, {}, "owner"):
            self.assert_code("invalid", self.store.issue_credential, self.admin, role)
        for ttl in (True, float("nan"), 30 * 86400 + 1):
            self.assert_code("invalid", self.store.issue_credential, self.admin, "viewer", ttl_seconds=ttl)
        limits = StoreLimits(max_credentials=1, max_projects=1)
        limited = TeamStore.create(Path(self.temp.name) / "limited", limits=limits, _clock=lambda: self.clock[0])
        self.addCleanup(limited.close)
        initial = limited.bootstrap_project("limited")
        actor = limited.authenticate(initial["token"], initial["project_id"])
        self.assert_code("quota", limited.issue_credential, actor, "viewer")
        self.assert_code("quota", limited.bootstrap_project, "extra")
        self.assertEqual(len(limited.list_credentials(actor)["credentials"]), 1)

    def test_project_deletion_revokes_cached_actors_and_local_recovery_cannot_undo_it(self):
        other = self.store.issue_credential(self.admin, "viewer")
        actor = self.store.authenticate(other["token"], self.project)
        self.assertTrue(self.store.delete_project(self.admin)["deleted"])
        self.assert_code("unauthorized", self.store.whoami, actor)
        self.assert_code("unauthorized", self.store.whoami, self.admin)
        self.assert_code("not_found", self.store.recover_admin, self.project)
        self.assertIsNotNone(self.store.list_projects_local()["projects"][0]["deleted_at"])
        self.assertEqual(self.store.purge_local()["purged"], {"reports": 0, "records": 0, "backups": 0})

    def test_audit_is_bounded_with_exact_omission_counts_and_no_secrets(self):
        limited = TeamStore.create(Path(self.temp.name) / "small-audit", limits=StoreLimits(max_audit_events=2),
                                   _clock=lambda: self.clock[0])
        self.addCleanup(limited.close)
        initial = limited.bootstrap_project("audited")
        actor = limited.authenticate(initial["token"], initial["project_id"])
        for _ in range(4):
            limited.issue_credential(actor, "viewer")
        audit = limited.audit(actor)
        self.assertEqual((audit["observed"], audit["dropped"], len(audit["events"])), (5, 3, 2))
        self.assertNotIn(initial["token"], json.dumps(audit))


if __name__ == "__main__":
    unittest.main()
