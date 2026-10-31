"""Actual local commands never print secrets or implicitly activate networking."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from test_team_evidence import report_bytes
from test_team_governance import approval

from src.team_command import main
from src.team_security import read_credential_file
from src.team_store import TeamStore


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None,
                                                prefix=".draintracer-team-command-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "store"
        self.credentials = self.path / "admin.json"

    def call(self, *args, authenticated=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["--store", str(self.path)]
        if authenticated:
            argv += ["--credentials", str(self.credentials)]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([*argv, *map(str, args)])
        return code, stdout.getvalue(), stderr.getvalue()

    def ok(self, *args, authenticated=False):
        code, stdout, stderr = self.call(*args, authenticated=authenticated)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        return json.loads(stdout)

    def initialize(self):
        self.assertTrue(self.ok("init")["created"])
        result = self.ok("bootstrap", "cli-project", "--credential-output", self.credentials)
        self.assertTrue(result["credential_saved"])
        return result

    def test_bootstrap_ingest_export_backups_and_project_deletion_are_real_json_operations(self):
        self.initialize()
        self.assertEqual(self.ok("whoami", authenticated=True)["role"], "admin")
        raw = self.root / "run.json"
        raw.write_bytes(report_bytes())
        metadata = self.ok("ingest", raw, "--request-id", uuid4().hex, authenticated=True)
        self.assertEqual(len(self.ok("reports", authenticated=True)["reports"]), 1)
        backup = self.ok("backup", authenticated=True)
        self.assertEqual(self.ok("restore", backup["backup_id"], authenticated=True)["restored"], 1)
        exported = self.path / "export.json"
        self.ok("export", metadata["report_id"], "--output", exported, authenticated=True)
        self.assertEqual(json.loads(exported.read_bytes())["privacy"]["profile"], "share")
        code, _, stderr = self.call("export", metadata["report_id"], "--output", exported, authenticated=True)
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", stderr)
        self.assertTrue(self.ok("project-delete", authenticated=True)["deleted"])
        self.assertEqual(self.call("whoami", authenticated=True)[0], 2)
        self.assertEqual(self.call("admin-recover", metadata["project_id"], "--credential-output", self.path / "again.json")[0], 2)

    def test_credential_commands_write_exclusively_and_do_not_print_tokens(self):
        initial = self.initialize()
        issued_path = self.path / "viewer.json"
        issued = self.ok("credential-issue", "viewer", "--credential-output", issued_path, authenticated=True)
        token = read_credential_file(issued_path)["token"]
        self.assertNotIn(token, json.dumps(initial))
        self.assertNotIn(token, json.dumps(issued))
        self.assertEqual(self.call("credential-issue", "viewer", "--credential-output", issued_path, authenticated=True)[0], 2)
        self.assertTrue(self.ok("credential-revoke", issued["credential"]["id"], authenticated=True)["revoked"])
        self.assertNotIn(token, json.dumps(self.ok("audit", authenticated=True)))

    def test_failed_credential_delivery_revokes_the_new_key_and_leaves_partial_file_for_inspection(self):
        self.initialize()
        path = self.path / "uncertain.json"
        with patch("src.team_security.os.fsync", side_effect=OSError("flush lost")):
            code, stdout, stderr = self.call("credential-issue", "viewer", "--credential-output", path, authenticated=True)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("revoked", json.loads(stderr)["error"]["message"])
        partial = read_credential_file(path)
        with TeamStore.open(self.path) as store, self.assertRaises(ValueError):
            store.authenticate(partial["token"], partial["project_id"])
        self.assertEqual(len(self.ok("credentials", authenticated=True)["credentials"]), 1)

    def test_strict_errors_hide_unknown_arguments_input_and_missing_authority(self):
        secret = "NeverEchoThisPrivateToken98765"
        code, stdout, stderr = self.call("--token", secret)
        self.assertEqual((code, stdout), (2, ""))
        self.assertNotIn(secret, stderr)
        self.assertEqual(json.loads(stderr)["error"]["code"], "invalid")
        self.assertEqual(self.call("whoami")[0], 2)
        self.assertFalse(self.path.exists())
        self.initialize()
        malformed = self.root / "bad.json"
        malformed.write_text('{"retention_seconds":60,"retention_seconds":120}', encoding="utf-8")
        self.assertEqual(self.call("policy-set", malformed, authenticated=True)[0], 2)

    def test_typed_record_commands_use_current_credentials_and_bounded_json(self):
        self.initialize()
        value = self.root / "approval.json"
        value.write_text(json.dumps(approval()), encoding="utf-8")
        created = self.ok("record-create", "baseline_approval", value, "--key", "cli",
                          "--request-id", uuid4().hex, authenticated=True)
        self.assertEqual(self.ok("record", created["record_id"], authenticated=True)["value"]["baseline_revision"], 1)
        self.assertEqual(len(self.ok("records", "--key", "cli", authenticated=True)["records"]), 1)
        self.assertTrue(self.ok("record-delete", created["record_id"], authenticated=True)["deleted"])
        self.assertEqual(self.ok("purge-local")["purged"], {"reports": 0, "records": 0, "backups": 0})

    @unittest.skipIf(os.name == "nt", "POSIX nonregular input check")
    def test_fifo_input_is_rejected_without_opening_or_blocking(self):
        self.initialize()
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        self.assertEqual(self.call("ingest", fifo, "--request-id", uuid4().hex, authenticated=True)[0], 2)
