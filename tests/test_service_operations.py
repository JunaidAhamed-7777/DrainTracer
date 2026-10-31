"""Operating evidence keeps failures, denominators and finite bounds honest."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
import psutil

from src.service_agent_exercise import exercise_agent
from src.service_exercise import (
    exercise_authority,
    exercise_authority_faults,
    exercise_evidence_recovery,
    require_fresh_workspace,
    run_exercise,
    write_evidence,
)
from src.service_operations import ExerciseEvidence, OperatingBudgets, ResourceSampler
from src.team_models import TeamError
from src.team_store import TeamStore


class OperatingEvidenceTests(unittest.TestCase):
    def test_expected_refusals_are_separate_from_baseline_and_never_echo_private_errors(self):
        evidence = ExerciseEvidence(OperatingBudgets())
        evidence.probe("authorized_read", lambda: 1, timing="read")
        evidence.probe("revoked_refused", lambda: (_ for _ in ()).throw(TeamError("unauthorized", "SECRET")),
                       expected_code="unauthorized")
        evidence.probe("unexpected_failure", lambda: (_ for _ in ()).throw(OSError("SECRET")))
        document = evidence.document()
        self.assertFalse(document["checks_passed"])
        self.assertEqual([row["kind"] for row in document["probes"]],
                         ["baseline", "expected_refusal", "baseline"])
        self.assertNotIn("SECRET", json.dumps(document))
        self.assertEqual(document["timings_seconds"]["read"]["samples"], 1)

    def test_zero_observations_are_unknown_and_missing_expected_refusal_fails(self):
        evidence = ExerciseEvidence(OperatingBudgets())
        evidence.rate("availability", 0, 0)
        evidence.probe("revoked_refused", lambda: True, expected_code="unauthorized")
        self.assertIsNone(evidence.document()["rates"]["availability"]["ratio"])
        self.assertFalse(evidence.document()["checks_passed"])
        with self.assertRaises(ValueError):
            evidence.rate("availability", 2, 1)

    def test_slow_samples_and_absent_resource_observations_fail_declared_budgets(self):
        evidence = ExerciseEvidence(OperatingBudgets(read_seconds=1))
        with mock.patch("src.service_operations.time.monotonic", side_effect=[1, 3]):
            evidence.probe("authorized_read", lambda: True, timing="read")
        evidence.resources["agent"] = {"samples": 0, "sampled_peak_rss_bytes": 0, "cpu_seconds": 0}
        self.assertEqual(evidence.document()["budget_checks"], {"read": False, "agent_sampled_rss": False})

    def test_real_resource_sampler_records_only_known_processes_and_releases_its_thread(self):
        sampler = ResourceSampler()
        sampler.add("driver", os.getpid())
        with sampler:
            pass
        observed = sampler.close()
        self.assertFalse(sampler._thread.is_alive())
        self.assertGreater(observed["driver"]["samples"], 0)
        self.assertGreater(observed["driver"]["sampled_peak_rss_bytes"], 0)
        self.assertGreaterEqual(observed["driver"]["cpu_seconds"], 0)
        with self.assertRaises(ValueError):
            sampler.add("arbitrary", os.getpid())

    def test_sampler_partial_context_entry_stops_the_actual_started_thread(self):
        sampler = ResourceSampler()
        start = sampler._thread.start

        def interrupted_start():
            start()
            raise RuntimeError("injected after actual sampler start")

        with (mock.patch.object(sampler._thread, "start", side_effect=interrupted_start),
              self.assertRaises(RuntimeError), sampler):
            self.fail("context entry should fail")
        self.assertTrue(sampler._stop.is_set())
        self.assertFalse(sampler._thread.is_alive())

    def test_invalid_or_unbounded_budgets_are_refused(self):
        for value in (True, float("nan"), float("inf"), 0, -1, 61):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OperatingBudgets(read_seconds=value)
        for value in (True, 0, 4 * 1024**3 + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OperatingBudgets(sampled_rss_bytes=value)

    def test_actual_authority_fault_drills_preserve_permissions_and_keep_secrets_out_of_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None) as temporary:
            root = require_fresh_workspace(Path(temporary) / "exercise")
            evidence = ExerciseEvidence(OperatingBudgets())
            with TeamStore.create(root / "team") as store:
                admin, viewer, operator = exercise_authority(store, evidence)
                exercise_authority_faults(store, admin, root, evidence)
                self.assertTrue(exercise_evidence_recovery(store, admin, viewer, operator, root, evidence))
                token = admin._token
            document = evidence.document()
            self.assertTrue(document["checks_passed"], document)
            self.assertEqual(document["rates"]["authorized_read_availability"],
                             {"successful": 5, "total": 5, "ratio": 1.0})
            path = write_evidence(root, document)
            self.assertNotIn(token, path.read_text())
            self.assertNotIn("injected fixture interruption", path.read_text())
            with self.assertRaises(FileExistsError):
                write_evidence(root, document)
            with self.assertRaises(ValueError):
                require_fresh_workspace(root)

    @unittest.skipUnless(os.name == "nt", "real Windows agent acceptance and owned-job crash recovery")
    def test_actual_agent_freshness_completion_cleanup_and_crash_prefix_are_measured(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            root = require_fresh_workspace(Path(temporary) / "exercise")
            evidence = ExerciseEvidence(OperatingBudgets(readiness_seconds=10, cleanup_seconds=20))
            with ResourceSampler() as sampler:
                sampler.add("driver", os.getpid())
                self.assertTrue(exercise_agent(root, evidence, sampler))
            evidence.resources = sampler.close()
            document = evidence.document()
            self.assertTrue(document["checks_passed"], document)
            self.assertEqual(document["rates"]["agent_completed_runs"],
                             {"successful": 2, "total": 2, "ratio": 1.0})
            self.assertGreater(document["resources"]["agent"]["samples"], 0)


    def test_public_complete_exercise_writes_private_readiness_evidence_and_preserves_failures(self):
        with tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None) as temporary:
            # The baseline must run with the entire optional transport absent.
            blocked = {f"src.{name}": None for name in
                       ("team_client", "team_transport", "team_queue", "team_server", "service_team_exercise")}
            with mock.patch.dict(sys.modules, blocked):
                path, document = run_exercise(Path(temporary) / "exercise", operator="Fixture operator", contact="Local reference",
                                               include_agent=False)
            self.assertTrue(document["operating_prerequisites_met"], document)
            self.assertEqual(document["components"], ["team", "local_collection"])
            self.assertEqual(document["rates"]["local_completed_runs"]["successful"], 1)
            self.assertNotIn("upload", document["timings_seconds"])
            self.assertNotIn("tls_authorized_read_availability", document["rates"])
            self.assertEqual(json.loads(path.read_bytes()), document)
            with mock.patch("src.service_exercise.exercise_authority", side_effect=OSError("private failure SECRET")):
                failed_path, failed = run_exercise(Path(temporary) / "failed", operator="Fixture operator", contact="Local reference",
                                                   include_agent=False)
            self.assertFalse(failed["operating_prerequisites_met"])
            self.assertFalse(failed["readiness_gates"]["required_observations_complete"])
            self.assertNotIn("SECRET", failed_path.read_text())

    @unittest.skipUnless(os.name == "nt", "actual owned Windows agent handle")
    def test_agent_is_reaped_when_resource_identity_query_fails_after_spawn(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            root = require_fresh_workspace(Path(temporary) / "exercise")
            evidence, sampler, spawned = ExerciseEvidence(OperatingBudgets()), ResourceSampler(), []
            popen = subprocess.Popen

            def capture(*args, **kwargs):
                process = popen(*args, **kwargs)
                spawned.append(process)
                return process

            with (mock.patch("src.service_agent_exercise.subprocess.Popen", side_effect=capture),
                  mock.patch.object(sampler, "add", side_effect=psutil.AccessDenied(os.getpid())),
                  self.assertRaises(psutil.AccessDenied)):
                exercise_agent(root, evidence, sampler)
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].poll(), "owned foreground agent leaked after resource query failure")




if __name__ == "__main__":
    unittest.main()
