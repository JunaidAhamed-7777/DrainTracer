"""Private authority initialization is atomic and never repairs missing permissions."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from src.team_database import _Database
from src.team_models import TeamError
from src.team_security import secure_directory


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None,
                                                prefix=".draintracer-team-db-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private-store"

    def test_creation_uses_private_sqlite_with_physical_and_durability_limits(self):
        with _Database.create(self.path) as store:
            identity = store.store_id
            with store.connection() as connection:
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 3)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA max_page_count").fetchone()[0] * 4096,
                                 store.limits.max_sqlite_bytes)
        self.assertEqual(_Database.open(self.path).store_id, identity)
        self.assertFalse((self.path / "team.sqlite3-wal").exists())
        self.assertFalse((self.path / "team.sqlite3-shm").exists())

    def test_missing_authority_is_not_bootstrapped_and_existing_authority_is_not_replaced(self):
        secure_directory(self.path, create=True)
        with self.assertRaises(TeamError):
            _Database.open(self.path)
        self.assertFalse((self.path / "team.sqlite3").exists())
        original = _Database.create(self.path)
        before = original.path.read_bytes()
        with self.assertRaises(TeamError) as caught:
            _Database.create(self.path)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(original.path.read_bytes(), before)

    def test_failed_atomic_publication_leaves_no_partial_authority_and_retry_succeeds(self):
        with mock.patch("src.team_database.os.replace", side_effect=OSError("write unavailable")), self.assertRaises(OSError):
            _Database.create(self.path)
        self.assertFalse((self.path / "team.sqlite3").exists())
        self.assertFalse(list(self.path.glob(".team-init-*")))
        self.assertTrue(_Database.create(self.path).store_id)

    def test_unknown_schema_or_changed_authority_is_rejected_without_auto_repair(self):
        store = _Database.create(self.path)
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaises(TeamError):
            _Database.open(self.path)
        with self.assertRaises(TeamError), store.connection():
            self.fail("cached store accepted changed authority")
        with closing(sqlite3.connect(store.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_publicly_readable_database_is_refused_without_acl_repair(self):
        store = _Database.create(self.path)
        if os.name == "nt":
            subprocess.run(["icacls", str(store.path), "/grant", "*S-1-1-0:(R)"],
                           check=True, capture_output=True)
        else:
            store.path.chmod(0o644)
        with self.assertRaises(TeamError):
            _Database.open(self.path)

    def test_reopened_unsupported_columns_constraints_or_indexes_fail_closed(self):
        for index, statement in enumerate((
            "ALTER TABLE credentials ADD COLUMN unexpected TEXT",
            "CREATE INDEX unexpected_lookup ON reports(id)",
            "CREATE INDEX sqliteX_extra ON reports(id)",
            "DROP INDEX reports_expiry",
        )):
            with self.subTest(statement=statement):
                path = self.path.parent / f"shape-{index}"
                store = _Database.create(path)
                store.close()
                with closing(sqlite3.connect(store.path)) as connection:
                    connection.execute(statement)
                    connection.commit()
                with self.assertRaises(TeamError):
                    _Database.open(path)


if __name__ == "__main__":
    unittest.main()
