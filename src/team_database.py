"""Private SQLite authority storage; opening never bootstraps missing permissions."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .team_models import StoreLimits, TeamError
from .team_security import private_file_lock, secure_directory, verify_data_file

DATABASE_NAME = "team.sqlite3"
SCHEMA_SQL = """
CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL,
 deleted_at REAL, policy TEXT NOT NULL, audit_seen INTEGER NOT NULL DEFAULT 0,
 audit_dropped INTEGER NOT NULL DEFAULT 0);
CREATE TABLE credentials (
 project_id TEXT NOT NULL, id TEXT NOT NULL, digest BLOB NOT NULL CHECK(length(digest)=32),
 role TEXT NOT NULL CHECK(role IN ('viewer','operator','admin')), created_at REAL NOT NULL, expires_at REAL NOT NULL,
 PRIMARY KEY(project_id,id), FOREIGN KEY(project_id) REFERENCES projects(id));
CREATE TABLE reports (
 project_id TEXT NOT NULL, id TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 payload BLOB, summary BLOB, sha256 TEXT NOT NULL, stored_bytes INTEGER NOT NULL,
 privacy_profile TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','deleted','expired')), history_until REAL NOT NULL,
 PRIMARY KEY(project_id,id), FOREIGN KEY(project_id) REFERENCES projects(id));
CREATE TABLE requests (
 project_id TEXT NOT NULL, request_id TEXT NOT NULL, input_sha256 TEXT NOT NULL,
 target_id TEXT NOT NULL, operation TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('committed','deleted','expired')),
 PRIMARY KEY(project_id,request_id), FOREIGN KEY(project_id) REFERENCES projects(id));
CREATE TABLE backups (
 project_id TEXT NOT NULL, id TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 payload BLOB NOT NULL, sha256 TEXT NOT NULL, stored_bytes INTEGER NOT NULL,
 PRIMARY KEY(project_id,id), FOREIGN KEY(project_id) REFERENCES projects(id));
CREATE TABLE backup_members (
 project_id TEXT NOT NULL, backup_id TEXT NOT NULL, report_id TEXT NOT NULL,
 PRIMARY KEY(project_id,backup_id,report_id),
 FOREIGN KEY(project_id,backup_id) REFERENCES backups(project_id,id) ON DELETE CASCADE);
CREATE TABLE project_records (
 project_id TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('baseline_approval','comparison_receipt')),
 record_key TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL, payload BLOB NOT NULL,
 sha256 TEXT NOT NULL, stored_bytes INTEGER NOT NULL, approval_id TEXT,
 PRIMARY KEY(project_id,id), FOREIGN KEY(project_id) REFERENCES projects(id),
 FOREIGN KEY(project_id,approval_id) REFERENCES project_records(project_id,id) ON DELETE CASCADE);
CREATE TABLE record_report_links (
 project_id TEXT NOT NULL, record_id TEXT NOT NULL, report_id TEXT NOT NULL,
 PRIMARY KEY(project_id,record_id,report_id),
 FOREIGN KEY(project_id,record_id) REFERENCES project_records(project_id,id) ON DELETE CASCADE);
CREATE TABLE audit_events (
 project_id TEXT NOT NULL, sequence INTEGER NOT NULL, timestamp REAL NOT NULL,
 credential_id TEXT, action TEXT NOT NULL, result TEXT NOT NULL, details TEXT NOT NULL, stored_bytes INTEGER NOT NULL,
 PRIMARY KEY(project_id,sequence), FOREIGN KEY(project_id) REFERENCES projects(id));
CREATE INDEX credentials_expiry ON credentials(project_id,expires_at);
CREATE INDEX reports_expiry ON reports(project_id,expires_at);
CREATE INDEX requests_expiry ON requests(project_id,expires_at);
CREATE INDEX backups_expiry ON backups(project_id,expires_at);
CREATE INDEX records_expiry ON project_records(project_id,expires_at);
CREATE INDEX records_key ON project_records(project_id,kind,record_key);
PRAGMA user_version=1;
"""
SCHEMA_HASH = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()
_TABLES = {"metadata", "projects", "credentials", "reports", "requests", "backups", "backup_members",
           "project_records", "record_report_links", "audit_events"}


def sqlite_error(error: sqlite3.Error) -> TeamError:
    message = str(error).lower()
    if "locked" in message or "busy" in message:
        return TeamError("busy", "Team storage is busy; retry with the same request ID")
    if "full" in message or "too big" in message:
        return TeamError("quota", "Team storage has reached a physical size limit")
    return TeamError("storage", "Team storage could not complete the operation")


def _configure(connection: sqlite3.Connection, limits: StoreLimits) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA synchronous=EXTRA")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA cache_size=-2048")
    connection.execute("PRAGMA mmap_size=0")
    connection.execute("PRAGMA journal_size_limit=0")
    connection.execute(f"PRAGMA busy_timeout={round(limits.busy_timeout_seconds * 1000)}")
    if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
        raise TeamError("storage", "Team storage requires DELETE journaling")
    if connection.execute("PRAGMA page_size").fetchone()[0] != 4096:
        raise TeamError("storage", "Team storage requires 4096-byte pages")
    if connection.execute(f"PRAGMA max_page_count={limits.max_sqlite_bytes // 4096}").fetchone()[0] > limits.max_sqlite_bytes // 4096:
        raise TeamError("storage", "Team database exceeds its configured physical cap")
    if connection.execute("PRAGMA synchronous").fetchone()[0] != 3:
        raise TeamError("storage", "Team storage could not enforce its synchronous policy")


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    sizes = connection.execute("SELECT name,length(CAST(value AS BLOB)) AS size FROM metadata LIMIT 5").fetchall()
    if len(sizes) != 4 or any(row["size"] > 8192 for row in sizes):
        raise TeamError("storage", "Invalid team authority metadata")
    return {row["name"]: row["value"] for row in connection.execute("SELECT name,value FROM metadata")}


def _metadata_hash(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


class _Database:
    """No persistent SQLite connection, implicit schema migration or network work."""

    def __init__(self, directory: str | Path, *, _clock=None):
        self.directory = secure_directory(directory)
        self.path = self.directory / DATABASE_NAME
        self._clock = _clock or time.time
        self._closed = False
        self.limits = StoreLimits()
        self._verify_files()
        try:
            with self.connection() as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise TeamError("storage", "Unsupported team authority schema")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables != _TABLES or connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
                    raise TeamError("storage", "Unexpected team authority schema objects")
                values = _metadata(connection)
                if set(values) != {"schema", "schema_hash", "store_id", "limits"} or values["schema"] != "1" or values["schema_hash"] != SCHEMA_HASH:
                    raise TeamError("storage", "Unsupported team authority metadata")
                identifier = uuid.UUID(hex=values["store_id"])
                if identifier.hex != values["store_id"]:
                    raise TeamError("storage", "Invalid team store identity")
                self.store_id = identifier.hex
                self.limits = StoreLimits(**json.loads(values["limits"]))
                self._metadata_hash = _metadata_hash(values)
                self._schema_cookie = connection.execute("PRAGMA schema_version").fetchone()[0]
        except (sqlite3.Error, ValueError, TypeError) as error:
            if isinstance(error, TeamError):
                raise
            raise TeamError("storage", "Team authority is missing, damaged or unsupported") from error
        self._verify_files()

    @classmethod
    def open(cls, directory: str | Path, *, _clock=None):
        return cls(directory, _clock=_clock)

    @classmethod
    def create(cls, directory: str | Path, *, limits: StoreLimits | None = None, _clock=None):
        limits = limits or StoreLimits()
        if not isinstance(limits, StoreLimits):
            raise TeamError("invalid", "limits must be StoreLimits")
        root = secure_directory(directory, create=True)
        destination = root / DATABASE_NAME
        temporary = None
        with private_file_lock(root / "create.lock"):
            if destination.exists() or destination.is_symlink():
                raise TeamError("conflict", "Team authority already exists; open it without replacing permissions")
            leftovers = [item for item in root.iterdir() if item.name.startswith(".team-init-")]
            if len(leftovers) >= 16:
                raise TeamError("storage", "Interrupted initialization files require local inspection")
            try:
                with tempfile.NamedTemporaryFile(prefix=".team-init-", suffix=".sqlite3", dir=root, delete=False) as handle:
                    temporary = Path(handle.name)
                verify_data_file(temporary)
                connection = sqlite3.connect(str(temporary), timeout=limits.busy_timeout_seconds, isolation_level=None)
                try:
                    connection.execute("PRAGMA page_size=4096")
                    _configure(connection, limits)
                    connection.executescript(SCHEMA_SQL)
                    connection.execute("BEGIN IMMEDIATE")
                    values = {"schema": "1", "schema_hash": SCHEMA_HASH, "store_id": uuid.uuid4().hex,
                              "limits": json.dumps(limits.to_dict(), allow_nan=False)}
                    connection.executemany("INSERT INTO metadata(name,value) VALUES(?,?)", values.items())
                    connection.commit()
                finally:
                    connection.close()
                verify_data_file(temporary)
                if destination.exists() or destination.is_symlink():
                    raise TeamError("conflict", "Team authority appeared during initialization")
                os.replace(temporary, destination)
            except sqlite3.Error as error:
                raise sqlite_error(error) from error
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
        return cls(root, _clock=_clock)

    def _verify_files(self) -> None:
        secure_directory(self.directory)
        verify_data_file(self.path)
        if self.path.stat().st_size > self.limits.max_sqlite_bytes:
            raise TeamError("storage", "Team database exceeds its physical cap")
        with self.path.open("rb") as handle:
            header = handle.read(100)
        if header[:16] != b"SQLite format 3\0" or header[18:20] != b"\1\1":
            raise TeamError("storage", "Team database has an unsupported header or journal mode")
        for suffix in ("-journal", "-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            try:
                path.lstat()
                verify_data_file(path)
                if suffix != "-journal" or path.stat().st_size > 2 * self.limits.max_sqlite_bytes + 65536:
                    raise TeamError("storage", "Team storage has an unsupported or oversized journal")
            except FileNotFoundError:
                pass
            except TeamError as error:
                if not isinstance(error.__cause__, FileNotFoundError):
                    raise

    @contextmanager
    def connection(self):
        if self._closed:
            raise TeamError("storage", "Team store is closed")
        self._verify_files()
        connection = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=self.limits.busy_timeout_seconds, isolation_level=None)
            _configure(connection, self.limits)
            if hasattr(self, "_schema_cookie") and (
                connection.execute("PRAGMA user_version").fetchone()[0] != 1
                or connection.execute("PRAGMA schema_version").fetchone()[0] != self._schema_cookie
                or _metadata_hash(_metadata(connection)) != self._metadata_hash
            ):
                raise TeamError("storage", "Team authority changed; reopen only after local inspection")
            yield connection
        except sqlite3.Error as error:
            raise sqlite_error(error) from error
        finally:
            if connection is not None:
                connection.close()

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


__all__ = []
