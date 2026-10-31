"""Explicit disposable-fixture drills; never accepts an existing authority store."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .service_operations import ExerciseEvidence, _require
from .team_security import secure_directory, verify_data_file
from .team_store import TeamStore


def exercise_authority(store: TeamStore, evidence: ExerciseEvidence):
    """Exercise real committed credentials and cached-handle refusal semantics."""
    issued = store.bootstrap_project("operating-fixture")
    project = issued["project_id"]
    admin = store.authenticate(issued["token"], project)
    viewer_issued = store.issue_credential(admin, "viewer")
    viewer = store.authenticate(viewer_issued["token"], project)
    operator_issued = store.issue_credential(admin, "operator")
    operator = store.authenticate(operator_issued["token"], project)
    other = store.bootstrap_project("other-operating-fixture")
    evidence.probe("cross_project_credential_refused",
                   lambda: store.authenticate(viewer_issued["token"], other["project_id"]),
                   expected_code="unauthorized")
    evidence.probe("viewer_cannot_issue_credential", lambda: store.issue_credential(viewer, "admin"),
                   expected_code="forbidden")
    available = 0
    for _index in range(5):
        result = evidence.probe("authorized_policy_read", lambda: store.get_policy(viewer), timing="read")
        available += bool(result is not None and result["project_id"] == project)
    evidence.rate("authorized_read_availability", available, 5)
    revoked = store.issue_credential(admin, "viewer")
    cached = store.authenticate(revoked["token"], project)
    store.revoke_credential(admin, revoked["credential"]["id"])
    evidence.probe("revoked_cached_actor_refused", lambda: store.get_policy(cached), expected_code="unauthorized")
    expiring = store.issue_credential(admin, "viewer", ttl_seconds=1)
    cached_expiring = store.authenticate(expiring["token"], project)
    # This is actual elapsed expiry, not a patched clock or a human exercise.
    time.sleep(1.05)
    evidence.probe("expired_cached_actor_refused", lambda: store.get_policy(cached_expiring),
                   expected_code="unauthorized")
    evidence.check("two_distinct_technical_credentials", viewer.credential_id != operator.credential_id)
    return admin, viewer, operator


def exercise_authority_faults(store: TeamStore, admin, root: Path, evidence: ExerciseEvidence):
    """Mutations are confined to newly created fixture copies, never real state."""
    original = store.path.read_bytes()
    missing = secure_directory(root / "authority-missing", create=True)
    evidence.probe("missing_authority_refused", lambda: TeamStore.open(missing), expected_code="storage")
    evidence.check("missing_authority_not_recreated", not (missing / store.path.name).exists())
    for fault in ("corrupt", "future", "unrelated"):
        directory = secure_directory(root / f"authority-{fault}", create=True)
        path = directory / store.path.name
        with path.open("xb") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(b"corrupt authority" if fault == "corrupt" else original)
        verify_data_file(path)
        if fault == "future":
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA user_version=999")
                connection.commit()
        if fault == "unrelated":
            # The opaque actor remains scoped to the original project and
            # credential; a separately bootstrapped store cannot accept it.
            path.unlink()
            with TeamStore.create(directory) as unrelated:
                unrelated.bootstrap_project("unrelated-fixture")
                evidence.probe("unrelated_authority_refused", lambda: unrelated.get_policy(admin),
                               expected_code="unauthorized")
        else:
            evidence.probe(f"{fault}_authority_refused", lambda directory=directory: TeamStore.open(directory),
                           expected_code="storage")
    # Roll back an interrupted SQL write by closing the original transaction.
    try:
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE projects SET name='uncommitted' WHERE id=?", (admin.project_id,))
            raise OSError("injected fixture interruption")
    except OSError:
        pass
    with TeamStore.open(store.directory) as reopened:
        evidence.probe("authority_recovers_after_interrupted_write", lambda: reopened.get_policy(admin))
        with reopened.connection() as connection:
            name = connection.execute("SELECT name FROM projects WHERE id=?", (admin.project_id,)).fetchone()[0]
        evidence.check("interrupted_write_rolled_back", name == "operating-fixture")
    evidence.check("authority_faults_preserve_original_access", store.whoami(admin)["role"] == "admin")


def require_fresh_workspace(path) -> Path:
    """Refuse all existing paths; create no fixture underneath a live deployment."""
    root = Path(path).absolute()
    if root.exists() or root.is_symlink():
        raise ValueError("Operating exercises require a new disposable workspace path")
    return secure_directory(root, create=True)


def write_evidence(root: Path, document: dict) -> Path:
    """One private, exclusively created artifact; never overwrite prior results."""
    encoded = json.dumps(document, ensure_ascii=True, allow_nan=False, indent=2).encode("ascii")
    _require(len(encoded) <= 1024 * 1024)
    path = root / "operating-evidence.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        verify_data_file(path)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return path
