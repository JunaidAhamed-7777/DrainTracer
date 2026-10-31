"""Private queue persistence, uncertain replies, expiry and explicit retry bounds."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_privacy import SECRET, private_fixture

from src.team_models import TeamError
from src.team_queue import MAX_ENTRIES, UploadQueue
from src.team_security import private_file_lock, secure_directory
from src.team_transport import Endpoint, SharingError


class DeduplicatingClient:
    project_id = "a" * 32
    endpoint = Endpoint.parse("tls://localhost:8443")

    def __init__(self):
        self.calls, self.accepted, self.lose_reply = [], {}, False

    def ingest_prepared(self, raw, request_id, *, timeout):
        self.calls.append((raw, request_id, timeout))
        digest = hashlib.sha256(raw).hexdigest()
        if request_id in self.accepted and self.accepted[request_id] != digest:
            raise TeamError("conflict", "Changed request")
        self.accepted[request_id] = digest
        if self.lose_reply:
            self.lose_reply = False
            raise SharingError("unavailable")
        return {"team_schema": 1, "report_id": "b" * 32}


class DurableQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None,
                                                prefix=".draintracer-queue-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue_path = self.root / "queue"
        self.endpoint = DeduplicatingClient.endpoint
        self.queue = UploadQueue(self.queue_path, "a" * 32, self.endpoint, create=True)
        self.raw = json.dumps(private_fixture()).encode()

    def reopen(self):
        return UploadQueue(self.queue_path, "a" * 32, self.endpoint)

    def test_lost_reply_survives_reopen_and_reuses_identical_private_prepared_payload(self):
        job = self.queue.enqueue(self.raw)
        self.assertNotIn(SECRET.encode(), (self.queue_path / "queue.json").read_bytes())
        client = DeduplicatingClient()
        client.lose_reply = True
        failed = self.queue.flush(client)
        self.assertEqual(failed["pending"], 1)
        reopened = self.reopen()
        item = reopened.inspect()["entries"][0]
        self.assertEqual(item["id"], job["id"])
        self.assertEqual(item["last_error"], "unavailable")
        self.assertEqual(reopened.flush(client)["attempted"], 0)
        result = reopened.flush(client, retry_blocked=True)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(len(client.accepted), 1)
        self.assertEqual(client.calls[0][:2], client.calls[1][:2])
        self.assertNotIn("entries", result)
        self.assertEqual(result["history"][0]["report_id"], "b" * 32)

    def test_interrupted_ack_save_keeps_retry_identity_and_prior_valid_journal(self):
        self.queue.enqueue(self.raw)
        client = DeduplicatingClient()
        replace = os.replace
        calls = 0

        def fail_after_accept(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk failure")
            return replace(source, destination)

        with mock.patch("src.team_queue.os.replace", side_effect=fail_after_accept), self.assertRaises(OSError):
            self.queue.flush(client)
        reopened = self.reopen()
        self.assertEqual(reopened.inspect()["entries"][0]["id"], client.calls[0][1])
        self.assertEqual(reopened.flush(client, retry_blocked=True)["delivered"], 1)
        self.assertEqual(len(client.accepted), 1)
        self.assertEqual(client.calls[0][:2], client.calls[1][:2])

    def test_entry_and_byte_quotas_preserve_prior_queue(self):
        with mock.patch("src.team_queue.MAX_ENTRIES", 2):
            self.queue.enqueue(self.raw)
            self.queue.enqueue(self.raw)
            before = self.queue.path.read_bytes()
            with self.assertRaises(SharingError) as refused:
                self.queue.enqueue(self.raw)
            self.assertEqual(refused.exception.code, "quota")
            self.assertEqual(self.queue.path.read_bytes(), before)
        with mock.patch("src.team_queue.MAX_QUEUE_BYTES", len(before) + 10), self.assertRaises(SharingError):
            self.queue.enqueue(self.raw)
        self.assertEqual(self.queue.path.read_bytes(), before)
        self.assertEqual(MAX_ENTRIES, 64)

    def test_terminal_auth_failure_requires_explicit_retry_and_expiry_discards_payload(self):
        with mock.patch("src.team_queue.time.time", return_value=1000):
            self.queue.enqueue(self.raw, ttl_seconds=10)
        client = DeduplicatingClient()
        with (mock.patch("src.team_queue.time.time", return_value=1001),
              mock.patch.object(client, "ingest_prepared", side_effect=TeamError("unauthorized", "Revoked"))):
            self.queue.flush(client)
            self.assertEqual(self.queue.flush(client)["attempted"], 0)
        with mock.patch("src.team_queue.time.time", return_value=1011):
            view = self.reopen().inspect()
        self.assertEqual(view["entries"], [])
        self.assertEqual(view["expired"], 1)
        self.assertEqual(view["history"][0]["state"], "expired")

    def test_project_endpoint_corruption_and_concurrent_writer_fail_closed(self):
        self.queue.enqueue(self.raw)
        before = self.queue.path.read_bytes()
        with self.assertRaises(SharingError):
            UploadQueue(self.queue_path, "c" * 32, self.endpoint)
        with self.assertRaises(SharingError):
            UploadQueue(self.queue_path, "a" * 32, Endpoint.parse("tls://localhost:8444"))
        with private_file_lock(self.queue.lock), self.assertRaises(TeamError):
            self.queue.enqueue(self.raw)
        self.assertEqual(self.queue.path.read_bytes(), before)
        state = json.loads(before)
        state["entries"][0]["digest"] = "0" * 64
        self.queue.path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(SharingError):
            self.reopen()

    def test_queue_preparation_starts_no_network_and_keeps_private_ancestry(self):
        with mock.patch("src.team_client.request", side_effect=AssertionError("unexpected network")):
            result = self.queue.enqueue(self.raw, profile="minimal")
            self.assertEqual(result["state"], "pending")
            self.assertEqual(len(self.queue.inspect()["entries"]), 1)
        self.assertEqual(secure_directory(self.queue_path), self.queue_path)


if __name__ == "__main__":
    unittest.main()
