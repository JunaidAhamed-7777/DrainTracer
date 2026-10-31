"""Real loopback TLS upload, retry and outage drills against disposable authority."""

from __future__ import annotations

import base64
import hashlib
import sys
import threading
from pathlib import Path
from uuid import uuid4

from .api import Session, SessionOptions
from .service_exercise import fixture_report
from .service_operations import ExerciseEvidence
from .team_client import TeamClient, prepare_upload
from .team_queue import UploadQueue
from .team_security import secure_directory, write_credential_file
from .team_server import make_server
from .team_transport import Endpoint, SharingError


def exercise_transport(store, admin, root: Path, certificate, key, ca, evidence: ExerciseEvidence) -> bool:
    credentials = secure_directory(root / "transport-credentials", create=True)
    operator = store.issue_credential(admin, "operator")
    viewer = store.issue_credential(admin, "viewer")
    paths = []
    for name, issued in (("operator", operator), ("viewer", viewer)):
        paths.append(write_credential_file(credentials / f"{name}.json",
                                          {"team_credentials_schema": 1, "project_id": issued["project_id"],
                                           "token": issued["token"]}))
    server = make_server(store, certificate, key, host="127.0.0.1", port=0, timeout=2)
    thread = None

    def start_listener():
        nonlocal thread
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        return thread

    successful = False
    try:
        thread = start_listener()
        endpoint = Endpoint.parse(f"tls://localhost:{server.server_address[1]}")
        writer = TeamClient(endpoint, paths[0], cafile=ca, timeout=2)
        reader = TeamClient(endpoint, paths[1], cafile=ca, timeout=2)
        evidence.probe("tls_authorized_readiness", lambda: reader.call("whoami"), timing="readiness")
        prepared = prepare_upload(fixture_report())
        receipt = evidence.probe("tls_upload_commit", lambda: writer.ingest_prepared(prepared, uuid4().hex),
                                 timing="upload")
        if receipt is None:
            return False
        available = 0
        for _index in range(3):
            result = evidence.probe("tls_authorized_export", lambda: reader.call("export_report", {"report_id": receipt["report_id"]}),
                                    timing="read")
            if result is not None:
                exported = base64.b64decode(result["report"], validate=True)
                available += hashlib.sha256(exported).hexdigest() == receipt["sha256"]
        evidence.rate("tls_authorized_read_availability", available, 3)
        evidence.check("tls_same_committed_hash_for_reader", available == 3)
        queue = UploadQueue(root / "upload-queue", admin.project_id, endpoint, create=True)
        queue.enqueue(fixture_report(), ttl_seconds=60)

        class LostAcknowledgement:
            project_id = admin.project_id
            endpoint = writer.endpoint

            def __init__(self):
                self.lost = False

            def ingest_prepared(self, raw, request_id, *, timeout):
                reply = writer.ingest_prepared(raw, request_id, timeout=timeout)
                if not self.lost:
                    self.lost = True
                    # Explicit synthetic acknowledgement loss AFTER an actual
                    # TLS commit; replay still traverses the real transport.
                    raise SharingError("unavailable")
                return reply

        count_before = len(store.list_reports(admin)["reports"])
        first = queue.flush(LostAcknowledgement())
        reopened = UploadQueue(queue.directory, admin.project_id, endpoint)
        second = reopened.flush(writer, retry_blocked=True)
        evidence.check("uncertain_tls_commit_replayed_once", first["pending"] == 1 and second["pending"] == 0
                       and len(store.list_reports(admin)["reports"]) == count_before + 1)
        evidence.rate("durable_queue_recovery", int(second["delivered"] == 1), 1)
        # Stop accepting while keeping the same bound socket available for an
        # explicit restart. TLS handshake deadline makes the outage observable.
        server.shutdown()
        thread.join(2)
        evidence.check("listener_stopped_for_outage", not thread.is_alive())
        evidence.probe("endpoint_outage_detected", lambda: reader.call("whoami", timeout=0.2),
                       expected_code="unavailable")
        queue.enqueue(fixture_report(), ttl_seconds=60)
        during = queue.flush(writer, deadline_seconds=0.2)
        evidence.check("outage_preserves_pending_upload", during["pending"] == 1)
        local = secure_directory(root / "outage-local-reports", create=True)
        session = Session.launch(getattr(sys, "_base_executable", None) or sys.executable,
                                 ["-c", "import time;time.sleep(.4)"],
                                 options=SessionOptions(sample_interval_ms=100, windows_seconds=(1,),
                                                        timeout_seconds=5, report_dir=local))
        try:
            session.start()
            result = evidence.probe("local_run_finishes_during_outage", lambda: session.wait(10), timing="cleanup")
            evidence.check("outage_does_not_block_local_collection", result is not None and result.startup == "started"
                           and result.cleanup == "complete" and result.exports is not None
                           and result.exports.json_path is not None and result.exports.json_path.is_file())
        finally:
            session.close(timeout=10)
        thread = start_listener()
        recovered = queue.flush(writer, retry_blocked=True)
        evidence.check("endpoint_restart_drains_same_durable_queue", recovered["pending"] == 0
                       and recovered["delivered"] == 2)
        store.revoke_credential(admin, operator["credential"]["id"])
        evidence.probe("revoked_transport_credential_refused", lambda: writer.call("whoami"), expected_code="unauthorized")
        successful = available == 3 and recovered["delivered"] == 2
        return successful
    finally:
        if thread is not None and thread.is_alive():
            server.shutdown()
        server.server_close()
        if thread is not None and thread.ident is not None:
            thread.join(3)
        evidence.check("tls_listener_thread_released", thread is None or not thread.is_alive())


__all__ = ["exercise_transport"]
