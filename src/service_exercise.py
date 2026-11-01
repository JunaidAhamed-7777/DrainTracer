"""Explicit disposable-fixture drills; never accepts an existing authority store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import unicodedata
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .service_operations import (
    ExerciseEvidence,
    OperatingBudgets,
    ResourceSampler,
    _require,
    reference_environment,
)
from .team_models import ProjectPolicy
from .team_security import secure_directory, verify_data_file
from .team_store import TeamStore


def exercise_credential_expiry(root: Path, evidence: ExerciseEvidence):
    """Inject expiry in a separate committed store without changing measured clocks."""
    clock = [time.time()]
    with TeamStore.create(root / "credential-expiry", _clock=lambda: clock[0]) as fixture:
        initial = fixture.bootstrap_project("credential-expiry-fixture")
        project = initial["project_id"]
        admin = fixture.authenticate(initial["token"], project)
        issued = fixture.issue_credential(admin, "viewer", ttl_seconds=1)
        cached = fixture.authenticate(issued["token"], project)
        policy = evidence.probe("stepped_clock_credential_read_before_expiry", lambda: fixture.get_policy(cached))
        evidence.check("stepped_clock_cached_actor_matches_issued_credential",
                       policy is not None and policy["project_id"] == project
                       and cached.credential_id == issued["credential"]["id"])
        # Step the supported private clock past this actual issued credential's
        # returned cutoff. Scheduling delays cannot expire the setup actor.
        cutoff = datetime.fromisoformat(issued["credential"]["expires_at"])
        if cutoff.tzinfo is None:
            raise ValueError("Credential expiry fixture needs an offset timestamp")
        clock[0] = cutoff.timestamp() + 1
        evidence.probe("stepped_clock_expired_cached_actor_refused", lambda: fixture.get_policy(cached),
                       expected_code="unauthorized")
        evidence.probe("stepped_clock_expired_credential_login_refused", lambda: fixture.authenticate(issued["token"], project),
                       expected_code="unauthorized")


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
    exercise_credential_expiry(store.directory.parent, evidence)
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


def exercise_local_collection(root: Path, evidence: ExerciseEvidence):
    """Measure a completed local monitor independently of optional transport."""
    import sys

    from .api import Session, SessionOptions
    from .report_contract import read_report

    reports = secure_directory(root / "local-reports", create=True)
    session = Session.launch(getattr(sys, "_base_executable", None) or sys.executable,
                             ["-c", "import time;time.sleep(.4)"],
                             options=SessionOptions(sample_interval_ms=100, windows_seconds=(1,),
                                                    timeout_seconds=5, report_dir=reports))
    try:
        session.start()
        result = evidence.probe("local_monitor_completed", lambda: session.wait(10), timing="cleanup")
        complete = result is not None and result.startup == "started" and result.cleanup == "complete"
        evidence.check("local_monitor_cleanup_complete", complete)
        persisted = (result is not None and result.exports is not None and result.exports.json_path is not None
                     and result.exports.html_path is not None and result.exports.json_path.is_file()
                     and result.exports.html_path.is_file())
        if persisted:
            read_report(result.exports.json_path)
        evidence.check("local_monitor_reports_readable", persisted)
        evidence.rate("local_completed_runs", int(complete), 1)
        evidence.rate("local_report_persistence", int(persisted), 1)
        return complete and persisted
    finally:
        session.close(timeout=10)


def _label(value):
    if (type(value) is not str or not value or value != value.strip() or len(value) > 256
            or any(unicodedata.category(character).startswith("C") for character in value)):
        raise ValueError("Operator and contact must be bounded printable text")
    return value


def run_exercise(workspace, *, operator: str, contact: str, include_agent: bool | None = None,
                 budgets: OperatingBudgets | None = None, with_transport: bool = False,
                 certificate=None, key=None, ca=None):
    """Create only new private fixtures; return a private measured evidence artifact."""
    from .api import _version
    from .service_agent_exercise import exercise_agent

    operator, contact = _label(operator), _label(contact)
    if include_agent is None:
        include_agent = os.name == "nt"
    if type(include_agent) is not bool or include_agent and os.name != "nt":
        raise ValueError("The local agent exercise requires Windows")
    if budgets is None:
        budgets = OperatingBudgets()
    if type(budgets) is not OperatingBudgets:
        raise ValueError("Operating budgets must be OperatingBudgets")
    if type(with_transport) is not bool:
        raise ValueError("Transport extension must be explicitly selected")
    if with_transport:
        # Optional extension: no transport import or TLS requirement belongs
        # to the default local authority/agent operating baseline.
        from .service_team_exercise import exercise_transport
        certificate, key, ca = (verify_data_file(path, writable=False) for path in (certificate, key, ca))
    elif any(path is not None for path in (certificate, key, ca)):
        raise ValueError("TLS files require the explicit transport extension")
    root = require_fresh_workspace(workspace)
    started = datetime.now(timezone.utc).isoformat()
    evidence = ExerciseEvidence(budgets)
    restored = False
    with ResourceSampler() as sampler:
        sampler.add("driver", os.getpid())

        def team_phase():
            nonlocal restored
            store = evidence.probe("team_authority_ready", lambda: TeamStore.create(root / "team"), timing="readiness")
            if store is None:
                return False
            with store:
                admin, viewer, writer = exercise_authority(store, evidence)
                exercise_authority_faults(store, admin, root, evidence)
                restored = exercise_evidence_recovery(store, admin, viewer, writer, root, evidence)
                transported = exercise_transport(store, admin, root, certificate, key, ca, evidence) if with_transport else True
                return restored and transported

        team_completed = evidence.probe("team_exercise_completed", team_phase) is True
        evidence.check("team_exercise_invariants", team_completed)
        local_completed = evidence.probe("local_collection_exercise_completed", lambda: exercise_local_collection(root, evidence)) is True
        evidence.check("local_collection_exercise_invariants", local_completed)
        agent_completed = not include_agent
        if include_agent:
            agent_completed = evidence.probe("agent_exercise_completed", lambda: exercise_agent(root, evidence, sampler)) is True
            evidence.check("agent_exercise_invariants", agent_completed)
    evidence.resources = sampler.close()
    document = evidence.document()
    required_rates = {"authorized_read_availability", "committed_report_durability", "local_completed_runs", "local_report_persistence"}
    required_timings = {"readiness", "ingest", "read", "cleanup"}
    if with_transport:
        required_rates |= {"tls_authorized_read_availability", "durable_queue_recovery"}
        required_timings.add("upload")
    if include_agent:
        required_rates |= {"agent_completed_runs", "agent_report_persistence"}
        required_timings.add("freshness")
    complete_observations = (required_rates <= set(document["rates"])
                             and all(document["rates"][name]["total"] > 0
                                     and document["rates"][name]["ratio"] == 1 for name in required_rates)
                             and required_timings <= set(document["timings_seconds"]))
    gates = {"named_operator": bool(operator), "support_contact": bool(contact), "runbook_identified": True,
             "successful_restore_exercise": restored, "team_exercise_completed": team_completed,
             "local_collection_exercise_completed": local_completed,
             "requested_agent_exercise_completed": agent_completed, "required_observations_complete": complete_observations,
             "all_checks_and_budgets_passed": document["checks_passed"]}
    document = {"operations_schema": 1, "version": _version(), "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(), "operator": operator, "support_contact": contact,
                "runbook": "https://github.com/JunaidAhamed-7777/DrainTracer/blob/main/docs/service-operations.md",
                "components": ["team", "local_collection"] + (["agent"] if include_agent else [])
                + (["transport"] if with_transport else []), "budgets": asdict(budgets),
                "environment": reference_environment(),
                "qualifications": ["Disposable local reference; recheck actual deployment prerequisites before a pilot",
                                   "Credential and retention expiry use separate explicitly stepped fixture clocks; elapsed expiry latency is not measured",
                                   "Managed restore repairs evidence payloads under current authority; it cannot recover lost authority"]
                                   + (["TLS service runs in the driver process; driver CPU/RSS includes the service",
                                       "Acknowledgement loss is injected after a real TLS commit"] if with_transport else []),
                **document, "readiness_gates": gates, "operating_prerequisites_met": all(gates.values())}
    return write_evidence(root, document), document


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measure local reference operations using only a NEW disposable private workspace.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--operator", required=True, help="Named deployment operator; not a consent or human-pilot record.")
    parser.add_argument("--contact", required=True)
    parser.add_argument("--with-transport", action="store_true", help="Explicitly add the optional loopback TLS extension.")
    parser.add_argument("--certificate", help="With transport: private local TLS certificate with localhost identity.")
    parser.add_argument("--key", help="With transport: private local TLS key; never copied into evidence.")
    parser.add_argument("--ca", help="With transport: private local CA/certificate trust file.")
    parser.add_argument("--components", choices=("team", "all"), default="all" if os.name == "nt" else "team")
    parser.add_argument("--readiness-seconds", type=float, default=2)
    parser.add_argument("--freshness-seconds", type=float, default=2)
    parser.add_argument("--upload-seconds", type=float, default=2)
    parser.add_argument("--read-seconds", type=float, default=2)
    parser.add_argument("--cleanup-seconds", type=float, default=20)
    parser.add_argument("--sampled-rss-mib", type=int, default=256)
    args = parser.parse_args(argv)
    try:
        budgets = OperatingBudgets(args.readiness_seconds, args.freshness_seconds, args.upload_seconds,
                                    args.read_seconds, args.cleanup_seconds, args.sampled_rss_mib * 1024 * 1024)
        path, document = run_exercise(args.workspace, operator=args.operator, contact=args.contact,
                                      certificate=args.certificate, key=args.key, ca=args.ca,
                                      with_transport=args.with_transport,
                                      include_agent=args.components == "all", budgets=budgets)
        print(json.dumps({"operations_schema": 1, "evidence": str(path),
                          "operating_prerequisites_met": document["operating_prerequisites_met"]}, ensure_ascii=True))
        return 0 if document["operating_prerequisites_met"] else 2
    except Exception:  # noqa: BLE001 - command boundary emits only a fixed error, never private failure text
        print(json.dumps({"operations_schema": 1, "operating_prerequisites_met": False,
                          "error": {"code": "exercise_failed"}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
