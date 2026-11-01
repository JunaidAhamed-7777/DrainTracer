"""A late client observation must not discard an adequately recorded natural run."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

from src.agent_command import AgentClient
from src.service_agent_exercise import exercise_agent
from src.service_exercise import require_fresh_workspace
from src.service_operations import ExerciseEvidence, OperatingBudgets, ResourceSampler


@unittest.skipUnless(os.name == "nt", "actual Windows agent and natural target completion")
class NaturalCompletionTests(unittest.TestCase):
    def test_natural_fixture_waits_for_observed_samples_before_its_natural_exit(self):
        original = AgentClient.request
        natural, observed, final = {}, [], []
        delayed = False

        def delayed_observation(client, operation, params=None, **kwargs):
            nonlocal delayed
            row = original(client, operation, params, **kwargs)
            if operation == "start" and params.get("args", [None])[0] == "1.3":
                arguments = params["args"]
                natural[row["session_id"]] = Path(arguments[1]) if len(arguments) > 1 else None
            if operation == "status" and params.get("session_id") in natural:
                if not delayed:
                    # Longer than the natural fixture's original 1.3 s work:
                    # this must delay observation, not exhaust its recording.
                    delayed = True
                    time.sleep(1.5)
                    row = original(client, operation, params, **kwargs)
                gate = natural[params["session_id"]]
                observed.append({"sample_count": row["sample_count"], "state": row["state"],
                                 "released": gate is not None and gate.exists(), "result": row["result"]})
                if row["result"] is not None:
                    final.append(row["result"])
            return row

        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            root = require_fresh_workspace(Path(temporary) / "exercise")
            evidence = ExerciseEvidence(OperatingBudgets(readiness_seconds=10, cleanup_seconds=20))
            with ResourceSampler() as sampler, mock.patch.object(AgentClient, "request", delayed_observation):
                sampler.add("driver", os.getpid())
                try:
                    completed = exercise_agent(root, evidence, sampler)
                except RuntimeError as error:
                    self.fail(f"Natural completion rejected: {error}; observations={observed!r}")
            evidence.resources = sampler.close()
            self.assertTrue(completed)
            self.assertTrue(observed)
            self.assertFalse(observed[0]["released"])
            self.assertIsNone(observed[0]["result"], observed)
            self.assertTrue(any(row["sample_count"] >= 3 and row["result"] is None and not row["released"]
                                for row in observed), observed)
            self.assertTrue(all(gate is not None and gate.is_file() for gate in natural.values()))
            self.assertTrue(final)
            self.assertTrue(all(row["reason"] == "target_exited" and row["target_exit_code"] == 0 for row in final))
            document = evidence.document()
            self.assertTrue(document["checks_passed"], document)
            self.assertEqual(document["rates"]["agent_completed_runs"]["successful"], 2)
            self.assertEqual(document["rates"]["agent_report_persistence"]["successful"], 2)
            self.assertEqual(document["timings_seconds"]["freshness"]["samples"], 2)
            self.assertTrue(any(row["name"] == "agent_driver_handle_reaped" and row["passed"]
                                for row in document["probes"]))


if __name__ == "__main__":
    unittest.main()
