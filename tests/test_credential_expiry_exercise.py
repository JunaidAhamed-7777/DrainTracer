"""Credential-expiry injection preserves real clocks and the same cached actor."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

from src.service_exercise import (
    exercise_authority,
    exercise_credential_expiry,
    require_fresh_workspace,
)
from src.service_operations import ExerciseEvidence, OperatingBudgets
from src.team_models import TeamError
from src.team_store import TeamStore


class CredentialExpiryExerciseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None)
        self.addCleanup(temporary.cleanup)
        self.root = require_fresh_workspace(Path(temporary.name) / "exercise")

    def test_real_issuance_delay_cannot_expire_the_isolated_setup_actor(self):
        evidence = ExerciseEvidence(OperatingBudgets())
        original_issue, original_authenticate, original_read = (
            TeamStore.issue_credential, TeamStore.authenticate, TeamStore.get_policy)
        issued_fixture, reads, logins, observed = {}, [], [], {}
        with TeamStore.create(self.root / "team") as main:
            measured_before = main._now()

            def issue(store, actor, role, *, ttl_seconds=86400):
                result = original_issue(store, actor, role, ttl_seconds=ttl_seconds)
                if store.directory.name == "credential-expiry" and ttl_seconds == 1:
                    issued_fixture.update(result)
                    fixture_before, wall_before, monotonic_before = store._now(), time.time(), time.monotonic()
                    # Real elapsed delay AFTER committed issuance, before the
                    # caller can authenticate. No system or main-store clock patch.
                    time.sleep(1.2)
                    observed.update(delay=time.monotonic() - monotonic_before,
                                    wall_progress=time.time() - wall_before,
                                    fixture_before=fixture_before, fixture_after=store._now(),
                                    measured_progress=main._now() - measured_before)
                return result

            def authenticate(store, token, project):
                tracked = store.directory.name == "credential-expiry" and token == issued_fixture.get("token")
                try:
                    actor = original_authenticate(store, token, project)
                except TeamError as error:
                    if tracked:
                        logins.append({"error": error.code})
                    raise
                if tracked:
                    logins.append({"actor": actor})
                return actor

            def read(store, actor):
                if store.directory.name != "credential-expiry":
                    return original_read(store, actor)
                observation = {"actor": actor, "fixture_time": store._now()}
                reads.append(observation)
                try:
                    result = original_read(store, actor)
                except TeamError as error:
                    observation["error"] = error.code
                    raise
                observation["project_id"] = result["project_id"]
                return result

            with (mock.patch.object(TeamStore, "issue_credential", issue),
                  mock.patch.object(TeamStore, "authenticate", authenticate),
                  mock.patch.object(TeamStore, "get_policy", read)):
                admin, viewer, operator = exercise_authority(main, evidence)
            self.assertGreater(main._now() - measured_before, 1)
            self.assertEqual(main.whoami(admin)["role"], "admin")
            self.assertEqual(main.get_policy(viewer)["project_id"], admin.project_id)
            self.assertEqual(main.whoami(operator)["role"], "operator")
        self.assertGreaterEqual(observed["delay"], 1)
        self.assertGreater(observed["wall_progress"], 1)
        self.assertGreater(observed["measured_progress"], 1)
        self.assertEqual(observed["fixture_before"], observed["fixture_after"])
        cutoff = datetime.fromisoformat(issued_fixture["credential"]["expires_at"]).timestamp()
        self.assertEqual(len(reads), 2)
        self.assertIs(reads[0]["actor"], reads[1]["actor"])
        self.assertEqual(reads[0]["actor"].credential_id, issued_fixture["credential"]["id"])
        self.assertEqual(reads[0]["project_id"], issued_fixture["project_id"])
        self.assertLess(reads[0]["fixture_time"], cutoff)
        self.assertGreater(reads[1]["fixture_time"], cutoff)
        self.assertEqual(reads[1]["error"], "unauthorized")
        self.assertEqual(len(logins), 2)
        self.assertIs(logins[0]["actor"], reads[0]["actor"])
        self.assertEqual(logins[1]["error"], "unauthorized")
        document = evidence.document()
        self.assertTrue(document["checks_passed"], document)
        self.assertEqual(document["timings_seconds"].keys(), {"read"})
        self.assertEqual(document["rates"]["authorized_read_availability"],
                         {"successful": 5, "total": 5, "ratio": 1.0})
        self.assertEqual(asdict(evidence.budgets), asdict(OperatingBudgets()))

    def test_failed_protected_read_before_expiry_cannot_pass_the_drill(self):
        evidence = ExerciseEvidence(OperatingBudgets())
        with mock.patch.object(TeamStore, "get_policy", side_effect=TeamError("unauthorized", "controlled refusal")):
            exercise_credential_expiry(self.root, evidence)
        probes = {probe["name"]: probe["passed"] for probe in evidence.document()["probes"]}
        self.assertFalse(probes["stepped_clock_credential_read_before_expiry"])
        self.assertFalse(probes["stepped_clock_cached_actor_matches_issued_credential"])
        self.assertFalse(evidence.document()["checks_passed"])


if __name__ == "__main__":
    unittest.main()
