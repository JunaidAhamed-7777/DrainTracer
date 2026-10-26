"""Experiment provenance: redaction, environment details, and lenient loading."""

from __future__ import annotations

import json
import unittest

from src.experiment import (
    REDACTED,
    ExperimentInfo,
    arguments_record,
    clean_label,
    environment_record,
    experiment_record,
    load_experiment,
    redact_text,
)


def _record(**overrides):
    values = dict(
        args=["--password", "hunter2", "--port", "8080"],
        mode="launch",
        duration_seconds=60.04321,
        sample_interval_ms=500,
        windows_seconds=(10.0, 30.0),
        warmup_seconds=5.0,
        timeout_seconds=60.0,
        version="v9.9.9",
    )
    values.update(overrides)
    info = values.pop("info", ExperimentInfo(workload_id="import-1k", build="1.4.2", operations=1000))
    return experiment_record(info, **values)


class RedactionTests(unittest.TestCase):
    def test_secret_assignments_and_tokens_are_redacted(self) -> None:
        self.assertEqual(redact_text("db password=hunter2 port=1"), f"db password={REDACTED} port=1")
        self.assertEqual(redact_text("API_KEY: 'abc def'"), f"API_KEY: {REDACTED}")
        self.assertEqual(redact_text("x client_secret=s3cr3t;y"), f"x client_secret={REDACTED};y")
        self.assertEqual(redact_text("Bearer abcdefghijk123"), f"Bearer {REDACTED}")
        self.assertEqual(redact_text("Basic dXNlcjpwYXNz"), f"Basic {REDACTED}")
        self.assertEqual(
            redact_text("https://user:pa55@host/db"), f"https://{REDACTED}@host/db"
        )
        self.assertEqual(redact_text("build 1.4.2 nightly"), "build 1.4.2 nightly")

    def test_secret_options_pasted_from_a_command_line_are_redacted(self) -> None:
        self.assertEqual(redact_text("ran with --password hunter2 -v"),
                         f"ran with --password {REDACTED} -v")
        self.assertEqual(redact_text("--api-key 'a b' --db-token xyz"),
                         f"--api-key {REDACTED} --db-token {REDACTED}")
        self.assertEqual(redact_text("-pwd s3cr3t"), f"-pwd {REDACTED}")
        # A following option is not a value, and plain words are left alone.
        self.assertEqual(redact_text("--token --verbose"), "--token --verbose")
        self.assertEqual(redact_text("token bucket sizing"), "token bucket sizing")

    def test_short_basic_credentials_are_redacted(self) -> None:
        cases = (
            ("Authorization: Basic dTpw", f"Authorization: Basic {REDACTED}"),
            ("basic dTo=", f"basic {REDACTED}"),
            ("BASIC Og==", f"BASIC {REDACTED}"),
            ("bAsIc\tOnA=; next", f"bAsIc\t{REDACTED}; next"),
            ("Basic dTpw.", f"Basic {REDACTED}."),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), expected)
                self.assertEqual(clean_label(text), " ".join(expected.split()))

    def test_short_auth_matching_preserves_ordinary_labels(self) -> None:
        for text in (
            "basic test", "basic sort", "basic setup",
            "Bearer dTpw", "Bearer short", "Basic dTpwX", "Basic dTpw==",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)

    def test_labels_are_trimmed_and_blank_ones_dropped(self) -> None:
        self.assertIsNone(clean_label("   "))
        self.assertIsNone(clean_label(None))
        self.assertEqual(clean_label("  a\n b "), "a b")
        self.assertEqual(len(clean_label("x" * 500)), 200)

    def test_arguments_are_counted_and_fingerprinted_never_stored(self) -> None:
        record = arguments_record(["--password", "hunter2"])
        self.assertEqual(record["count"], 2)
        self.assertEqual(len(record["fingerprint"]), 16)
        self.assertNotIn("hunter2", json.dumps(record))
        self.assertNotEqual(arguments_record(["a b"])["fingerprint"],
                            arguments_record(["a", "b"])["fingerprint"])
        self.assertEqual(arguments_record(["a"]), arguments_record(["a"]))
        self.assertEqual(arguments_record(None), {"count": 0, "fingerprint": None, "redacted": True})


class RecordTests(unittest.TestCase):
    def test_record_holds_labels_sampling_and_environment_without_secrets(self) -> None:
        record = _record()
        text = json.dumps(record)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("8080", text)
        self.assertEqual(record["workload_id"], "import-1k")
        self.assertEqual(record["operations"], 1000)
        self.assertEqual(record["duration_seconds"], 60.043)
        self.assertEqual(record["sampling"]["windows_seconds"], [10.0, 30.0])
        self.assertEqual(record["arguments"]["count"], 4)
        environment = record["environment"]
        self.assertEqual(environment["draintracer_version"], "v9.9.9")
        self.assertGreater(environment["cpu_count"], 0)
        self.assertGreater(environment["total_memory_bytes"], 0)
        for key in ("hostname", "node", "user", "environ"):
            self.assertNotIn(key, environment)

    def test_attached_runs_record_no_arguments(self) -> None:
        self.assertIsNone(_record(mode="attach", args=None)["arguments"])

    def test_environment_probes_never_raise(self) -> None:
        self.assertIn("python_version", environment_record())

    def test_loading_round_trips_and_tolerates_older_or_malformed_reports(self) -> None:
        data = load_experiment({"experiment": _record()})
        self.assertEqual((data.workload_id, data.build, data.operations),
                         ("import-1k", "1.4.2", 1000.0))
        self.assertEqual(data.argument_count, 4)
        self.assertIsNone(load_experiment({}))
        odd = load_experiment({"experiment": {"operations": "many", "workload_id": 3,
                                              "arguments": [], "environment": "x"}})
        self.assertEqual((odd.operations, odd.workload_id, odd.argument_count, odd.environment),
                         (None, None, None, None))
        self.assertIsNone(load_experiment({"experiment": {"operations": -1}}).operations)


if __name__ == "__main__":
    unittest.main()
