"""Real private-file checks protect SQLite-style and credential sidecars."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.team_models import TeamError
from src.team_security import (
    private_file_lock,
    read_credential_file,
    secure_directory,
    verify_data_file,
    write_credential_file,
)


class PrivatePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None,
                                                prefix=".draintracer-team-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = secure_directory(self.root / "store", create=True)

    def test_inherited_private_files_and_exclusive_credentials_work_natively(self):
        token = {"team_credentials_schema": 1, "project_id": "a" * 32, "token": "private-test-token"}
        destination = self.private / "credential.json"
        write_credential_file(destination, token)
        self.assertEqual(read_credential_file(destination), token)
        self.assertEqual(verify_data_file(destination), destination)
        with self.assertRaises(FileExistsError):
            write_credential_file(destination, token)
        self.assertEqual(read_credential_file(destination), token)
        self.assertFalse(any(item.name.endswith(".lock") for item in self.private.iterdir()))

    def test_lock_rejects_concurrent_writer_and_releases_after_exception(self):
        path = self.private / "writer.lock"
        with private_file_lock(path), self.assertRaises(TeamError) as caught, private_file_lock(path):
            self.fail("second writer acquired the lock")
        self.assertEqual(caught.exception.code, "busy")
        with self.assertRaisesRegex(OSError, "body failure"), private_file_lock(path):
            raise OSError("body failure")
        with private_file_lock(path):
            self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"\0")

    def test_malformed_duplicate_or_oversize_credential_file_is_never_accepted(self):
        path = self.private / "credentials.json"
        for payload in (b"\xff", b" " * 8193,
                        b'{"team_credentials_schema":1,"project_id":"a","token":"x","token":"y"}',
                        json.dumps({"team_credentials_schema": True, "project_id": "a", "token": "x"}).encode()):
            with self.subTest(size=len(payload)):
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                with self.assertRaises(TeamError):
                    read_credential_file(path)

    def test_failed_credential_write_is_explicit_and_does_not_overwrite_on_retry(self):
        path = self.private / "credentials.json"
        token = {"team_credentials_schema": 1, "project_id": "a" * 32, "token": "private-test-token"}
        with mock.patch("src.team_security.os.fsync", side_effect=OSError("flush failed")), self.assertRaises(OSError):
            write_credential_file(path, token)
        self.assertTrue(path.exists())
        with self.assertRaises(FileExistsError):
            write_credential_file(path, token)

    @unittest.skipIf(os.name == "nt", "POSIX mode and symlink checks")
    def test_public_writable_parent_symlink_and_readable_file_are_rejected_without_repair(self):
        public = self.root / "public"
        public.mkdir(mode=0o777)
        public.chmod(0o777)
        with self.assertRaises(TeamError):
            secure_directory(public / "new-private", create=True)
        self.assertFalse((public / "new-private").exists())
        link = self.root / "link"
        link.symlink_to(self.private, target_is_directory=True)
        with self.assertRaises(TeamError):
            secure_directory(link)
        path = self.private / "public.json"
        path.write_text("private", encoding="utf-8")
        path.chmod(0o644)
        with self.assertRaises(TeamError):
            verify_data_file(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
