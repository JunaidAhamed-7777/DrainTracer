"""Optional transport operating drills, after the local operating baseline."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

from src.service_exercise import require_fresh_workspace, run_exercise
from src.service_operations import ExerciseEvidence, OperatingBudgets
from src.service_team_exercise import exercise_transport
from src.team_store import TeamStore


class TransportOperatingTests(unittest.TestCase):
    def test_explicit_transport_adds_mandatory_network_observations_to_the_passing_local_baseline(self):
        with tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None) as temporary:
            root = require_fresh_workspace(Path(temporary) / "fixture-inputs")
            fixtures = Path(__file__).parent / "fixtures" / "team-tls"
            for name in ("cert.pem", "key.pem"):
                path = root / name
                path.write_bytes((fixtures / name).read_bytes())
                if os.name != "nt":
                    path.chmod(0o600)
            _path, document = run_exercise(Path(temporary) / "exercise", operator="Fixture operator", contact="Local reference",
                                           with_transport=True, certificate=root / "cert.pem", key=root / "key.pem",
                                           ca=root / "cert.pem", include_agent=False)
            self.assertTrue(document["operating_prerequisites_met"], document)
            self.assertIn("transport", document["components"])
            self.assertEqual(document["rates"]["tls_authorized_read_availability"]["successful"], 3)
            self.assertIn("upload", document["timings_seconds"])

    def test_actual_tls_upload_retry_outage_and_local_collection_recovery(self):
        with tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None) as temporary:
            root = require_fresh_workspace(Path(temporary) / "exercise")
            fixtures = Path(__file__).parent / "fixtures" / "team-tls"
            for name in ("cert.pem", "key.pem"):
                path = root / name
                path.write_bytes((fixtures / name).read_bytes())
                if os.name != "nt":
                    path.chmod(0o600)
            evidence = ExerciseEvidence(OperatingBudgets())
            with TeamStore.create(root / "team") as store:
                issued = store.bootstrap_project("transport-fixture")
                admin = store.authenticate(issued["token"], issued["project_id"])
                self.assertTrue(exercise_transport(store, admin, root, root / "cert.pem", root / "key.pem",
                                                   root / "cert.pem", evidence))
            self.assertTrue(evidence.document()["checks_passed"], evidence.document())


    def test_bound_tls_socket_is_closed_when_listener_thread_cannot_start(self):
        from src.team_server import make_server

        with tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None) as temporary:
            root = require_fresh_workspace(Path(temporary) / "exercise")
            fixtures = Path(__file__).parent / "fixtures" / "team-tls"
            for name in ("cert.pem", "key.pem"):
                path = root / name
                path.write_bytes((fixtures / name).read_bytes())
                if os.name != "nt":
                    path.chmod(0o600)
            bound = []

            def capture(*args, **kwargs):
                server = make_server(*args, **kwargs)
                bound.append(server)
                return server

            with TeamStore.create(root / "team") as store:
                issued = store.bootstrap_project("listener-start-fault")
                admin = store.authenticate(issued["token"], issued["project_id"])
                with (mock.patch("src.service_team_exercise.make_server", side_effect=capture),
                      mock.patch("src.service_team_exercise.threading.Thread.start", side_effect=RuntimeError("injected")),
                      self.assertRaises(RuntimeError)):
                    exercise_transport(store, admin, root, root / "cert.pem", root / "key.pem", root / "cert.pem",
                                       ExerciseEvidence(OperatingBudgets()))
            self.assertEqual(len(bound), 1)
            self.assertEqual(bound[0].socket.fileno(), -1, "bound TLS listener leaked after thread startup failure")


    def test_started_tls_thread_is_owned_when_start_reports_a_partial_failure(self):
        from src.team_server import make_server

        with tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None) as temporary:
            root = require_fresh_workspace(Path(temporary) / "exercise")
            fixtures = Path(__file__).parent / "fixtures" / "team-tls"
            for name in ("cert.pem", "key.pem"):
                path = root / name
                path.write_bytes((fixtures / name).read_bytes())
                if os.name != "nt":
                    path.chmod(0o600)
            bound, started = [], []
            actual_start = threading.Thread.start

            def capture(*args, **kwargs):
                server = make_server(*args, **kwargs)
                bound.append(server)
                return server

            def partial_start(worker):
                actual_start(worker)
                started.append(worker)
                raise RuntimeError("injected after actual thread start")

            with TeamStore.create(root / "team") as store:
                issued = store.bootstrap_project("listener-partial-start-fault")
                admin = store.authenticate(issued["token"], issued["project_id"])
                with (mock.patch("src.service_team_exercise.make_server", side_effect=capture),
                      mock.patch("src.service_team_exercise.threading.Thread.start", autospec=True, side_effect=partial_start),
                      self.assertRaises(RuntimeError)):
                    exercise_transport(store, admin, root, root / "cert.pem", root / "key.pem", root / "cert.pem",
                                       ExerciseEvidence(OperatingBudgets()))
            self.assertEqual(len(started), 1)
            self.assertFalse(started[0].is_alive(), "partially started listener thread leaked")
            self.assertEqual(bound[0].socket.fileno(), -1)



if __name__ == "__main__":
    unittest.main()
