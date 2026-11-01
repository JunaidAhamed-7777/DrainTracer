"""Deterministic deadline counterfactuals; no real monitoring or wall-time waits."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401

from src.service_exercise import exercise_local_collection
from src.service_operations import ExerciseEvidence, OperatingBudgets


class LocalCollectionDeadlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.attempt = 0

    def run_fixture(self, *, completion_at=11.0, start_seconds=0.0, close_seconds=2.0,
                    close_cleanup="complete", close_confirmed=True, close_error=False):
        clock = [0.0]
        self.attempt += 1
        root = self.root / f"attempt-{self.attempt}"
        root.mkdir()
        json_path, html_path = root / "recording.json", root / "recording.html"
        json_path.write_text("{}", encoding="ascii")
        html_path.write_text("fixture", encoding="ascii")
        result = SimpleNamespace(startup="started", cleanup="complete",
                                 exports=SimpleNamespace(json_path=json_path, html_path=html_path))
        waited, closed = [], []

        class FakeSession:
            def start(self):
                clock[0] += start_seconds

            def wait(self, timeout):
                waited.append(timeout)
                remaining = max(0.0, completion_at - clock[0])
                if remaining <= timeout:
                    clock[0] += remaining
                    return result
                clock[0] += timeout
                return None

            def close(self, timeout):
                closed.append(timeout)
                clock[0] += close_seconds
                if close_error:
                    raise OSError("controlled safety-close failure")
                # A late cancellation may finish safely but cannot replace
                # the original wait's missing natural completion result.
                return SimpleNamespace(cleanup=close_cleanup) if close_confirmed else None

        def directory(path, *, create):
            self.assertTrue(create)
            path.mkdir()
            return path

        evidence = ExerciseEvidence(OperatingBudgets())
        with (mock.patch("src.api.Session.launch", return_value=FakeSession()) as launch,
              mock.patch("src.service_exercise.secure_directory", side_effect=directory),
              mock.patch("src.report_contract.read_report") as read,
              mock.patch("src.service_operations.time.monotonic", side_effect=lambda: clock[0])):
            complete = exercise_local_collection(root, evidence)
        options = launch.call_args.kwargs["options"]
        self.assertEqual(options.timeout_seconds, 5)
        self.assertEqual(launch.call_args.args[1], ["-c", "import time;time.sleep(.4)"])
        self.assertEqual(closed, [10])
        return complete, evidence.document(), waited, read.call_count

    def test_natural_eleven_seconds_plus_two_second_close_fits_declared_twenty(self):
        complete, document, waited, reads = self.run_fixture()
        self.assertTrue(complete)
        self.assertEqual(waited, [20])
        self.assertEqual(document["timings_seconds"]["cleanup"]["maximum"], 13)
        self.assertTrue(document["budget_checks"]["cleanup"])
        self.assertTrue(document["checks_passed"])
        self.assertEqual(document["rates"]["local_completed_runs"], {"successful": 1, "total": 1, "ratio": 1.0})
        self.assertEqual(reads, 1)

    def test_missed_twenty_second_wait_plus_safety_close_remains_failed_at_twenty_two(self):
        complete, document, waited, reads = self.run_fixture(completion_at=21)
        self.assertFalse(complete)
        self.assertEqual(waited, [20])
        self.assertEqual(document["timings_seconds"]["cleanup"]["maximum"], 22)
        self.assertFalse(document["budget_checks"]["cleanup"])
        self.assertFalse(document["checks_passed"])
        self.assertFalse(document["probes"][0]["passed"])
        self.assertEqual(document["rates"]["local_completed_runs"]["successful"], 0)
        self.assertEqual(reads, 0)

    def test_unconfirmed_or_failed_safety_close_cannot_pass(self):
        for options in ({"close_confirmed": False}, {"close_cleanup": "incomplete"}, {"close_error": True}):
            with self.subTest(options=options):
                complete, document, _waited, reads = self.run_fixture(**options)
                self.assertFalse(complete)
                self.assertFalse(document["checks_passed"])
                self.assertFalse(document["probes"][0]["passed"])
                self.assertEqual(reads, 0)

    def test_startup_consumes_the_same_absolute_deadline(self):
        complete, document, waited, _reads = self.run_fixture(start_seconds=7)
        self.assertTrue(complete)
        self.assertEqual(waited, [13])
        self.assertEqual(document["timings_seconds"]["cleanup"]["maximum"], 13)

    def test_startup_beyond_the_deadline_receives_no_new_wait_allowance(self):
        complete, document, waited, _reads = self.run_fixture(start_seconds=21, completion_at=24)
        self.assertFalse(complete)
        self.assertEqual(waited, [0])
        self.assertEqual(document["timings_seconds"]["cleanup"]["maximum"], 23)
        self.assertFalse(document["budget_checks"]["cleanup"])


if __name__ == "__main__":
    unittest.main()
