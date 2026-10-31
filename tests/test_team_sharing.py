"""Real private authority and TLS: two credentials, uncertain commits and outages."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from test_privacy import SECRET, private_fixture
from test_team_records import approval, receipt

from src.api import Session, SessionOptions
from src.team_client import TeamClient, prepare_upload
from src.team_models import TeamError
from src.team_queue import UploadQueue
from src.team_security import secure_directory, write_credential_file
from src.team_server import make_server
from src.team_store import TeamStore
from src.team_transport import Endpoint, SharingError


class SharedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None,
                                                prefix=".draintracer-sharing-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = secure_directory(self.root / "private", create=True)
        fixtures = Path(__file__).parent / "fixtures" / "team-tls"
        self.cert, self.key = self.private / "cert.pem", self.private / "key.pem"
        for source, destination in ((fixtures / "cert.pem", self.cert), (fixtures / "key.pem", self.key)):
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(source.read_bytes())
        self.store = TeamStore.create(self.root / "store")
        self.issued = self.store.bootstrap_project("sharing-fixture")
        self.actor = self.store.authenticate(self.issued["token"], self.issued["project_id"])
        self.operator_issued = self.store.issue_credential(self.actor, "operator")
        self.viewer_issued = self.store.issue_credential(self.actor, "viewer")
        self.server = make_server(self.store, self.cert, self.key, port=0, timeout=2)
        self.endpoint = Endpoint.parse(f"tls://localhost:{self.server.server_address[1]}")
        self.thread = self.start_listener()
        self.admin = self.client(self.issued, "admin")
        self.writer = self.client(self.operator_issued, "operator")
        self.reader = self.client(self.viewer_issued, "viewer")
        self.raw = json.dumps(private_fixture()).encode()

    def start_listener(self):
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        return thread

    def client(self, issued, label):
        path = write_credential_file(self.private / f"{label}.json", {
            "team_credentials_schema": 1, "project_id": issued["project_id"], "token": issued["token"]})
        return TeamClient(self.endpoint, path, cafile=self.cert, timeout=3)

    def tearDown(self):
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        deadline = time.monotonic() + 3
        while self.server._slots._value != 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.store.close()

    def test_two_authorized_credentials_read_the_same_project_approved_comparison(self):
        from src import team_transport

        approved = self.admin.call("create_record", {"kind": "baseline_approval", "value": approval(),
                                                      "request_id": uuid4().hex})
        value = receipt()
        value.update(approval_id=approved["record_id"], source_commit="f" * 40)
        queue = UploadQueue(self.root / "queue", self.actor.project_id, self.endpoint, create=True)
        queue.enqueue_receipt(value)
        send = team_transport.send_frame
        dropped = []

        def lose_receipt_ack(connection, reply, deadline):
            if not dropped and reply.get("result", {}).get("kind") == "comparison_receipt":
                dropped.append(True)
                raise ConnectionResetError("injected receipt acknowledgement loss")
            return send(connection, reply, deadline)

        with mock.patch("src.team_transport.send_frame", side_effect=lose_receipt_ack):
            self.assertEqual(queue.flush(self.writer)["pending"], 1)
        delivered = UploadQueue(queue.directory, self.actor.project_id, self.endpoint).flush(self.writer, retry_blocked=True)
        identity = delivered["history"][0]["object_id"]
        first = self.reader.call("read_record", {"record_id": identity})
        second = self.writer.call("read_record", {"record_id": identity})
        self.assertEqual(first["value"], second["value"])
        self.assertEqual(first["value"], value)
        self.assertEqual(first["value"]["basis"], "client-computed")
        self.assertEqual(first["value"]["approval_id"], approved["record_id"])
        self.assertEqual(len(self.reader.call("list_records", {"kind": "comparison_receipt"})["records"]), 1)
        with self.assertRaises(TeamError) as refused:
            self.reader.call("create_record", {"kind": "baseline_approval", "value": approval()})
        self.assertEqual(refused.exception.code, "forbidden")

    def test_raw_upload_is_validated_and_privacy_transformed_on_the_server(self):
        stored = self.writer.ingest_prepared(self.raw, uuid4().hex)
        exported = self.reader.call("export_report", {"report_id": stored["report_id"]})
        raw = base64.b64decode(exported["report"], validate=True)
        self.assertNotIn(SECRET.encode(), raw)
        self.assertEqual(json.loads(raw)["privacy"]["profile"], "share")
        for operation in ("bootstrap_project", "recover_admin", "connection", "__dict__"):
            with self.subTest(operation=operation), self.assertRaises(TeamError) as refused:
                self.admin.call(operation)
            self.assertEqual(refused.exception.code, "invalid")

    def test_cross_project_and_revocation_between_login_and_commit_are_refused(self):
        other = self.store.bootstrap_project("another-project")
        wrong = self.client({**other, "project_id": self.actor.project_id}, "wrong-project")
        with self.assertRaises(TeamError) as refused:
            wrong.call("whoami")
        self.assertEqual(refused.exception.code, "unauthorized")
        authenticate = self.store.authenticate

        def revoke_after_login(token, project_id):
            actor = authenticate(token, project_id)
            if token == self.operator_issued["token"]:
                self.store.revoke_credential(self.actor, self.operator_issued["credential"]["id"])
            return actor

        with mock.patch.object(self.store, "authenticate", side_effect=revoke_after_login), self.assertRaises(TeamError) as refused:
            self.writer.ingest_prepared(prepare_upload(self.raw), uuid4().hex)
        self.assertEqual(refused.exception.code, "unauthorized")
        self.assertEqual(self.reader.call("list_reports")["reports"], [])

    def test_connection_lost_after_real_commit_replays_one_logical_report(self):
        from src import team_transport

        queue = UploadQueue(self.root / "queue", self.actor.project_id, self.endpoint, create=True)
        job = queue.enqueue(self.raw)
        send = team_transport.send_frame
        dropped = []

        def lose_success(connection, value, deadline):
            if not dropped and value.get("ok") and value.get("result", {}).get("report_id"):
                dropped.append(True)
                raise ConnectionResetError("injected loss after durable commit")
            return send(connection, value, deadline)

        with mock.patch("src.team_transport.send_frame", side_effect=lose_success):
            self.assertEqual(queue.flush(self.writer)["pending"], 1)
        recovered = UploadQueue(queue.directory, self.actor.project_id, self.endpoint)
        result = recovered.flush(self.writer, retry_blocked=True)
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["history"][0]["id"], job["id"])
        self.assertEqual(len(self.reader.call("list_reports")["reports"]), 1)

    def test_outage_leaves_local_monitoring_export_and_cleanup_independent(self):
        queue = UploadQueue(self.root / "queue", self.actor.project_id, self.endpoint, create=True)
        queue.enqueue(self.raw)
        self.server.shutdown()
        self.thread.join(2)
        self.assertEqual(queue.flush(self.writer, deadline_seconds=0.2)["pending"], 1)
        script = self.root / "worker.py"
        script.write_text("import time; time.sleep(.4)", encoding="ascii")
        with (mock.patch("src.team_client.request", side_effect=AssertionError("monitor attempted network")),
              Session.launch(str(script), options=SessionOptions(sample_interval_ms=100, windows_seconds=(1,),
                                                                timeout_seconds=3, report_dir=self.root / "reports")) as session):
            result = session.wait(5)
        self.assertEqual(result.startup, "started")
        self.assertEqual(result.cleanup, "complete")
        self.assertTrue(result.exports.json_path.is_file())
        self.assertTrue(result.exports.html_path.is_file())
        self.thread = self.start_listener()
        self.assertEqual(queue.flush(self.writer, retry_blocked=True)["delivered"], 1)

    def test_wrong_negotiated_service_never_receives_a_credential_frame(self):
        self.server.context.set_alpn_protocols(["h2"])
        with mock.patch.object(self.store, "authenticate", wraps=self.store.authenticate) as authenticate:
            with self.assertRaises(SharingError) as refused:
                self.reader.call("whoami")
            self.assertEqual(refused.exception.code, "certificate")
            authenticate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
