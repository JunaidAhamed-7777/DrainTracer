"""Explicit disposable-fixture drills; never accepts an existing authority store."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .service_operations import ExerciseEvidence, _require
from .team_models import ProjectPolicy
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


def fixture_report() -> bytes:
    """Synthetic private markers, never evidence from a user's monitored run."""
    return json.dumps({"report_schema": 3, "target_path": "operating-private-seed",
                       "snapshots": [{"timestamp": 1700000000 + index,
                                      "processes": [{"pid": 89741, "name": "operating-private-seed",
                                                     "private_bytes": 100 + index, "create_time": 1699999999}],
                                      "totals": {"private_bytes": 100 + index}} for index in range(2)]}).encode()


def exercise_evidence_recovery(store: TeamStore, admin, viewer, operator, root: Path, evidence: ExerciseEvidence):
    """Restore damaged payloads under current authority, never roll authority back."""
    raw, request_id = fixture_report(), uuid4().hex
    metadata = evidence.probe("sanitized_ingest_committed", lambda: store.ingest(operator, raw, request_id=request_id),
                              timing="ingest")
    if metadata is None:
        evidence.rate("committed_report_durability", 0, 1)
        return False
    identity = metadata["report_id"]
    payload = store.export_report(viewer, identity)
    evidence.check("server_privacy_removed_seed", b"operating-private-seed" not in payload
                   and b"operating-private-seed" not in store.path.read_bytes())
    evidence.probe("malformed_upload_refused", lambda: store.ingest(operator, b"{broken"), expected_code="invalid")
    evidence.probe("viewer_upload_refused", lambda: store.ingest(viewer, raw), expected_code="forbidden")
    with TeamStore.open(store.directory) as reopened:
        durable = reopened.export_report(viewer, identity)
    matched = durable == payload and hashlib.sha256(durable).hexdigest() == metadata["sha256"]
    evidence.check("committed_report_survives_reopen", matched)
    evidence.rate("committed_report_durability", int(matched), 1)
    snapshot = store.backup(admin)
    backup_id = snapshot["backup_id"]
    revoked = store.issue_credential(admin, "viewer")
    cached_revoked = store.authenticate(revoked["token"], admin.project_id)
    store.revoke_credential(admin, revoked["credential"]["id"])
    evidence.probe("viewer_restore_refused", lambda: store.restore(viewer, backup_id), expected_code="forbidden")
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE reports SET payload=? WHERE project_id=? AND id=?",
                           (b"injected damaged fixture", admin.project_id, identity))
        connection.commit()
    evidence.probe("damaged_report_detected", lambda: store.read_report(viewer, identity), expected_code="storage")
    restored = evidence.probe("current_admin_restores_payload", lambda: store.restore(admin, backup_id))
    after = store.read_report(operator, identity)
    verified = (restored is not None and identity in restored["report_ids"]
                and store.export_report(viewer, identity) == payload and after["expires_at"] == metadata["expires_at"]
                and after["sha256"] == metadata["sha256"])
    evidence.check("restored_hash_expiry_and_two_reader_permissions", verified)
    evidence.probe("restore_does_not_revive_revoked_actor", lambda: store.read_report(cached_revoked, identity),
                   expected_code="unauthorized")
    audit = store.audit(admin)
    evidence.check("restore_has_authoritative_audit", any(row["action"] == "backup_restore" for row in audit["events"]))
    # Loss of current authority must block even a formerly valid admin. Keep
    # the exact file available locally; never replace it from an old snapshot.
    unavailable = store.path.with_name("authority-unavailable.sqlite3")
    store.path.rename(unavailable)
    try:
        evidence.probe("restore_without_current_authority_refused", lambda: store.restore(admin, backup_id),
                       expected_code="storage")
        evidence.check("restore_does_not_recreate_authority", not store.path.exists())
    finally:
        unavailable.rename(store.path)
    evidence.probe("access_recovers_with_exact_current_authority", lambda: store.read_report(viewer, identity))
    store.delete_report(admin, identity)
    evidence.probe("deleted_evidence_backup_cannot_restore", lambda: store.restore(admin, backup_id),
                   expected_code="not_found")
    evidence.probe("deleted_evidence_retry_cannot_resurrect", lambda: store.ingest(operator, raw, request_id=request_id),
                   expected_code="conflict")
    evidence.probe("deleted_evidence_unavailable", lambda: store.read_report(viewer, identity), expected_code="not_found")
    # A stricter current privacy policy invalidates an older, less restrictive
    # backup instead of allowing a later restore to reverse the policy.
    second = store.ingest(operator, raw)
    old = store.backup(admin)
    store.update_policy(admin, ProjectPolicy(privacy_profile="minimal"))
    evidence.probe("old_privacy_snapshot_cannot_restore", lambda: store.restore(admin, old["backup_id"]),
                   expected_code="not_found")
    evidence.check("current_stricter_privacy_preserved", store.read_report(viewer, second["report_id"])["report"]["privacy"]["profile"] == "minimal")
    # Retention is a deterministic elapsed-time fault using the supported
    # private clock seam, explicitly distinct from measured wall-time latency.
    clock = [time.time()]
    with TeamStore.create(root / "retention", _clock=lambda: clock[0]) as retained:
        issued = retained.bootstrap_project("retention-fixture", policy=ProjectPolicy(retention_seconds=60))
        actor = retained.authenticate(issued["token"], issued["project_id"])
        report = retained.ingest(actor, raw)
        backup = retained.backup(actor)
        clock[0] += 61
        evidence.probe("stepped_clock_expired_report_refused", lambda: retained.read_report(actor, report["report_id"]),
                       expected_code="not_found")
        evidence.probe("stepped_clock_expired_snapshot_refused", lambda: retained.restore(actor, backup["backup_id"]),
                       expected_code="not_found")
    return verified


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
