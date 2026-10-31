"""CLI consent, private operating evidence, token boundaries and real TLS requests."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401
from test_team_evidence import report_bytes
from test_team_receipts import check_result

from src import share_command
from src.team_receipts import prepare_receipt
from src.team_security import secure_directory, write_credential_file
from src.team_server import make_server
from src.team_store import TeamStore


def evidence_document():
    return {"operations_schema": 1, "version": share_command._version(), "operating_prerequisites_met": True,
            "operator": "Named reference operator", "support_contact": "Local reference contact",
            "components": ["team", "local_collection"], "finished_at": datetime.now(timezone.utc).isoformat(),
            "readiness_gates": {key: True for key in ("named_operator", "support_contact", "runbook_identified",
                                "successful_restore_exercise", "team_exercise_completed", "required_observations_complete",
                                "all_checks_and_budgets_passed")}}


class ShareCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None,
                                                prefix=".draintracer-share-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = secure_directory(Path(self.temp.name) / "private", create=True)
        self.store = TeamStore.create(self.root / "store")
        self.addCleanup(self.store.close)
        self.issued = self.store.bootstrap_project("cli-fixture")
        self.actor = self.store.authenticate(self.issued["token"], self.issued["project_id"])
        self.credential = self.root / "credential.json"
        write_credential_file(self.credential, {"team_credentials_schema": 1, "project_id": self.issued["project_id"],
                                                "token": self.issued["token"]})

    def call(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = share_command.main(list(map(str, arguments)))
        self.assertNotIn(self.issued["token"], output.getvalue() + errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def private_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return path

    @contextmanager
    def server(self):
        fixtures = Path(__file__).parent / "fixtures" / "team-tls"
        for name in ("cert.pem", "key.pem"):
            path = self.root / name
            path.write_bytes((fixtures / name).read_bytes())
            if os.name != "nt":
                path.chmod(0o600)
        server = make_server(self.store, self.root / "cert.pem", self.root / "key.pem", port=0, timeout=1)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield ["--endpoint", f"tls://localhost:{server.server_address[1]}", "--credentials", self.credential,
                   "--ca", self.root / "cert.pem"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_help_version_and_invalid_arguments_never_connect_or_echo_inputs(self):
        with mock.patch("src.team_client.request", side_effect=AssertionError("SECRET unexpected network")):
            for argument in ("--help", "--version"):
                with self.subTest(argument=argument), self.assertRaises(SystemExit) as caught:
                    self.call([argument])
                self.assertEqual(caught.exception.code, 0)
            code, output, error = self.call(["--token", "SECRET"])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn("SECRET", error)

    def test_offline_preview_and_enqueue_start_no_network_and_keep_private_inputs_out(self):
        path = self.root / "raw.json"
        path.write_bytes(report_bytes())
        with mock.patch("src.team_client.request", side_effect=AssertionError("unexpected network")):
            code, output, errors = self.call(["preview", path])
            self.assertEqual((code, errors), (0, ""))
            self.assertNotIn("UntrustedPrivatePath98765", output)
            args = ["--endpoint", "tls://localhost:8443", "--credentials", self.credential]
            code, _output, _errors = self.call([*args, "enqueue-report", "--queue", self.root / "queue", path])
            self.assertEqual(code, 0)
            code, output, _errors = self.call([*args, "queue", "--queue", self.root / "queue"])
        self.assertEqual(code, 2)
        self.assertEqual(len(json.loads(output)["entries"]), 1)
        self.assertNotIn("payload", output)
        self.assertNotIn("UntrustedPrivatePath98765", output)

    def test_private_operating_gate_rejects_malformed_old_future_versions_and_false_booleans(self):
        valid = evidence_document()
        path = self.private_json("operating.json", valid)
        self.assertEqual(share_command.operating_evidence(path)["operator"], valid["operator"])
        for change in ({"operations_schema": True}, {"version": "v0.0.0"}, {"operator": ""},
                       {"operating_prerequisites_met": 1}, {"readiness_gates": {}}, {"components": ["agent"]},
                       {"finished_at": "2020-01-01T00:00:00+00:00"},
                       {"finished_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
                       {"finished_at": datetime.now().replace(tzinfo=None).isoformat()}):
            altered = {**valid, **change}
            path.write_text(json.dumps(altered))
            with self.subTest(change=change), self.assertRaises(ValueError):
                share_command.operating_evidence(path)
        altered = copy.deepcopy(valid)
        altered["readiness_gates"]["successful_restore_exercise"] = 1
        path.write_text(json.dumps(altered))
        with self.assertRaises(ValueError):
            share_command.operating_evidence(path)
        path.write_bytes(b'{"operations_schema":1,"operations_schema":1}')
        with self.assertRaises(ValueError):
            share_command.operating_evidence(path)

    def test_serve_requires_operating_gate_before_store_or_listener_and_nonloopback_optin(self):
        args = ["serve", "--store", self.store.directory, "--certificate", self.root / "cert.pem", "--key", self.root / "key.pem"]
        with mock.patch("src.team_server.make_server") as create:
            self.assertEqual(self.call(args)[0], 2)
            path = self.private_json("operating.json", evidence_document())
            code, _output, _errors = self.call([*args, "--operating-evidence", path, "--host", "0.0.0.0"])
            self.assertEqual(code, 2)
        create.assert_not_called()

    def test_real_tls_upload_flush_history_export_and_revoked_outage_exits(self):
        path = self.root / "raw.json"
        path.write_bytes(report_bytes())
        with self.server() as args:
            self.assertEqual(self.call([*args, "enqueue-report", "--queue", self.root / "queue", path])[0], 0)
            code, output, errors = self.call([*args, "flush", "--queue", self.root / "queue"])
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(json.loads(output)["pending"], 0)
            code, output, _errors = self.call([*args, "history", "--limit", "1"])
            self.assertEqual(code, 0)
            report = json.loads(output)["reports"][0]
            destination = self.root / "export.json"
            self.assertEqual(self.call([*args, "export", report["report_id"], "--output", destination])[0], 0)
            self.assertEqual(destination.read_bytes(), self.store.export_report(self.actor, report["report_id"]))
            self.assertEqual(self.call([*args, "export", report["report_id"], "--output", destination])[0], 2)
            issued = self.store.issue_credential(self.actor, "viewer")
            revoked = self.root / "revoked.json"
            write_credential_file(revoked, {"team_credentials_schema": 1, "project_id": issued["project_id"], "token": issued["token"]})
            self.store.revoke_credential(self.actor, issued["credential"]["id"])
            invalid_args = list(args)
            invalid_args[3] = revoked
            self.assertEqual(self.call([*invalid_args, "whoami"])[0], 2)
        self.assertEqual(self.call([*args, "whoami"])[0], 2)

    def test_remote_credentials_are_delivered_privately_and_revoked_if_delivery_fails(self):
        with self.server() as args:
            destination = self.root / "issued.json"
            code, output, errors = self.call([*args, "issue-credential", "viewer", "--output", destination])
            self.assertEqual((code, errors), (0, ""))
            self.assertNotIn("token", output)
            self.assertTrue(destination.is_file())
            before = {row["id"] for row in self.store.list_credentials(self.actor)["credentials"]}
            with mock.patch("src.share_command.write_credential_file", side_effect=OSError("SECRET")):
                code, output, errors = self.call([*args, "issue-credential", "viewer", "--output", self.root / "failed.json"])
            self.assertEqual(code, 2)
            self.assertNotIn("SECRET", output + errors)
            self.assertEqual({row["id"] for row in self.store.list_credentials(self.actor)["credentials"]}, before)
            with mock.patch("src.share_command.write_credential_file", side_effect=KeyboardInterrupt):
                code, _output, _errors = self.call([*args, "issue-credential", "viewer", "--output", self.root / "interrupted.json"])
            self.assertEqual(code, 130)
            self.assertEqual({row["id"] for row in self.store.list_credentials(self.actor)["credentials"]}, before)

    def test_peer_secret_fields_and_bearer_strings_never_reach_cli_output(self):
        with mock.patch("src.share_command.TeamClient.call", return_value={"token": self.issued["token"],
                        "note": "credential " + self.issued["token"], "secret": "SECRET"}):
            code, output, errors = self.call(["--endpoint", "tls://localhost:8443", "--credentials", self.credential, "whoami"])
        self.assertEqual((code, errors), (0, ""))
        self.assertNotIn("SECRET", output)
        self.assertIn("[credential]", output)


    def test_typed_receipt_queue_filtered_history_pagination_and_literal_comparison(self):
        repository = "<b>Owner/Repo</b>"
        approval = self.store.create_record(self.actor, "baseline_approval",
                    {"record_schema": 1, "repository": repository, "workload_id": "workload", "source_commit": "a" * 40,
                     "baseline_revision": 1, "audit_head": "b" * 64, "note": ""})
        receipt = prepare_receipt(check_result(), repository=repository, workload_id="workload", approval_id=approval["record_id"])
        path = self.private_json("receipt.json", receipt)
        with self.server() as args:
            for _index in range(2):
                self.assertEqual(self.call([*args, "enqueue-comparison", "--queue", self.root / "receipts", path])[0], 0)
            self.assertEqual(self.call([*args, "flush", "--queue", self.root / "receipts"])[0], 0)
            filters = ["history", "--repository", repository, "--source-commit", "c" * 40, "--workload-id", "workload", "--limit", "1"]
            code, output, _errors = self.call([*args, *filters])
            first = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(len(first["records"]), 1)
            self.assertIsNotNone(first["next_cursor"])
            code, output, _errors = self.call([*args, *filters, "--cursor", first["next_cursor"]])
            second = json.loads(output)
            self.assertEqual(code, 0)
            self.assertNotEqual(first["records"][0]["record_id"], second["records"][0]["record_id"])
            code, text, _errors = self.call([*args, "comparison", first["records"][0]["record_id"], "--text"])
            self.assertEqual(code, 0)
            self.assertIn(repository, text)
            self.assertIn("Client-computed", text)
            self.assertNotIn("\x1b", text)

    def test_offline_w4_check_uses_actual_store_workload_and_preserves_inconclusive_exit(self):
        result = check_result()
        result.update(exit_code=3, passed=False)
        result["comparison"]["measured_verdict"] = "inconclusive"
        result["comparison"]["resources"]["memory"].update(verdict="unknown", drift_after={"median": None})
        store = mock.MagicMock()
        store.__enter__.return_value = store
        store.state = {"workload_id": "actual-controlled-workload"}
        store.check.return_value = (3, result)
        runs = object()
        module = SimpleNamespace(BaselineStore=mock.Mock(return_value=store), load_runs=mock.Mock(return_value=runs))
        output = self.root / "prepared.json"
        with (mock.patch.dict("sys.modules", {"src.baseline_control": module}),
              mock.patch("src.team_client.request", side_effect=AssertionError("unexpected network"))):
            code, text, errors = self.call(["prepare-comparison", self.root / "baseline", "--run", self.root / "run.json",
                            "--repository", "Owner/Repo", "--source-commit", "c" * 40,
                            "--approval-id", "d" * 32, "--output", output])
        self.assertEqual((code, errors), (3, ""))
        self.assertEqual(json.loads(text)["check_exit_code"], 3)
        value = json.loads(output.read_bytes())
        self.assertEqual(value["workload_id"], "actual-controlled-workload")
        self.assertEqual(value["outcome"], "inconclusive")
        self.assertNotIn("PrivateSeed123", output.read_text())
        store.check.assert_called_once_with(runs, source_commit="c" * 40)

    def test_approve_baseline_validates_local_store_before_sending_stable_retry_identity(self):
        validated = []
        store = mock.MagicMock()
        store.__enter__.return_value = store
        store.state = {"workload_id": "actual-workload", "revision": 1}
        store.audit = [{"action": "promote", "source_commit": "a" * 40, "digest": "b" * 64}]
        store.reports.side_effect = lambda: validated.append(True)
        module = SimpleNamespace(BaselineStore=mock.Mock(return_value=store))
        with (mock.patch.dict("sys.modules", {"src.baseline_control": module}),
              mock.patch("src.share_command.TeamClient.call", return_value={"record_id": "d" * 32}) as call):
            code, _output, _errors = self.call(["--endpoint", "tls://localhost:8443", "--credentials", self.credential,
                    "approve-baseline", self.root / "baseline", "--repository", "Owner/Repo", "--request-id", "e" * 32])
        self.assertEqual(code, 0)
        self.assertEqual(validated, [True])
        operation, parameters = call.call_args.args
        self.assertEqual(operation, "create_record")
        self.assertEqual(parameters["request_id"], "e" * 32)
        self.assertEqual(parameters["value"]["workload_id"], "actual-workload")

    def test_serve_interrupt_closes_listener_without_bootstrap_and_returns130(self):
        path = self.private_json("operating.json", evidence_document())
        listener = mock.Mock()
        listener.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch("src.team_server.make_server", return_value=listener):
            code, output, errors = self.call(["serve", "--store", self.store.directory, "--certificate", self.root / "cert.pem",
                                 "--key", self.root / "key.pem", "--operating-evidence", path])
        self.assertEqual((code, errors), (130, ""))
        self.assertTrue(json.loads(output)["listening"])
        listener.server_close.assert_called_once()

    def test_invalid_history_bindings_are_refused_before_network(self):
        with mock.patch("src.team_client.request", side_effect=AssertionError("unexpected network")):
            args = ["--endpoint", "tls://localhost:8443", "--credentials", self.credential, "history"]
            for filters in (["--repository", "Owner/Repo"], ["--repository", "Owner/Repo", "--source-commit", "HEAD", "--workload-id", "w"]):
                self.assertEqual(self.call([*args, *filters])[0], 2)


if __name__ == "__main__":
    unittest.main()
