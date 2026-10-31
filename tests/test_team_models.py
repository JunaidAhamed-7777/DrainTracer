"""Strict local team limits do not accept Boolean budgets or JSON actor claims."""

from __future__ import annotations

import pickle
import unittest
from dataclasses import FrozenInstanceError

from src.team_models import Actor, ProjectPolicy, StoreLimits, TeamError, _make_actor


class ModelTests(unittest.TestCase):
    def test_limits_are_finite_bounded_and_coherent(self):
        for changes in ({"max_projects": True}, {"max_upload_bytes": 0}, {"max_records": 1025},
                        {"busy_timeout_seconds": float("nan")}, {"max_sqlite_bytes": 513 * 1024 * 1024},
                        {"max_project_bytes": 129 * 1024 * 1024}):
            with self.subTest(changes=changes), self.assertRaises(TeamError):
                StoreLimits(**changes)
        limits = StoreLimits()
        self.assertLess(limits.max_global_bytes, limits.max_sqlite_bytes)
        with self.assertRaises(FrozenInstanceError):
            limits.max_projects = 100

    def test_project_cannot_exceed_local_admin_policy_or_disable_privacy(self):
        limits = StoreLimits(max_projects=2, max_reports=3)
        self.assertEqual(ProjectPolicy().resolved(limits).max_reports, 3)
        for changes in ({"privacy_profile": "full"}, {"retention_seconds": 0},
                        {"max_reports": True}, {"backup_ttl_seconds": 86401},
                        {"audit_retention_seconds": float("inf")}):
            with self.subTest(changes=changes), self.assertRaises(TeamError):
                ProjectPolicy(**changes)
        with self.assertRaises(TeamError):
            ProjectPolicy(max_reports=4).resolved(limits)

    def test_actor_is_opaque_immutable_unserializable_and_does_not_print_tokens(self):
        with self.assertRaises(TypeError):
            Actor("project", "credential", "private-token")
        actor = _make_actor("project", "credential", "private-token")
        self.assertEqual(actor.project_id, "project")
        self.assertNotIn("private-token", repr(actor))
        self.assertFalse(hasattr(actor, "__dict__"))
        with self.assertRaises(AttributeError):
            actor._project_id = "other"
        with self.assertRaises(TypeError):
            pickle.dumps(actor)


if __name__ == "__main__":
    unittest.main()
