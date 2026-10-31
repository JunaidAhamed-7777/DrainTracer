"""Transactional sanitized evidence, quotas, expiry and retry authority."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from .privacy import redact_report
from .report_contract import parse_report, session_summary
from .team_models import ProjectPolicy, TeamError, integer

HISTORY_SECONDS = 30 * 86400


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _iso(value):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _id(value):
    import re
    if type(value) is not str or not re.fullmatch("[0-9a-f]{32}", value):
        raise TeamError("invalid", "Identifier must be lowercase UUID hex")
    return value


class _Evidence:
    def _remove_records(self, connection, project_id, ids):
        for record_id in ids:
            # Deleting an approval cascades its receipts; mark every affected retry.
            affected = [row[0] for row in connection.execute(
                "SELECT id FROM project_records WHERE project_id=? AND (id=? OR approval_id=?)",
                (project_id, record_id, record_id))]
            for target in affected:
                connection.execute("UPDATE requests SET state='deleted' WHERE project_id=? AND target_id=? AND operation='record'",
                                   (project_id, target))
            connection.execute("DELETE FROM project_records WHERE project_id=? AND id=?", (project_id, record_id))

    def _remove_report(self, connection, project_id, report_id, state):
        backups = [row[0] for row in connection.execute(
            "SELECT backup_id FROM backup_members WHERE project_id=? AND report_id=?", (project_id, report_id))]
        for backup_id in backups:
            connection.execute("DELETE FROM backups WHERE project_id=? AND id=?", (project_id, backup_id))
        records = [row[0] for row in connection.execute(
            "SELECT record_id FROM record_report_links WHERE project_id=? AND report_id=?", (project_id, report_id))]
        self._remove_records(connection, project_id, records)
        connection.execute("UPDATE reports SET payload=NULL,summary=NULL,stored_bytes=0,state=? WHERE project_id=? AND id=?",
                           (state, project_id, report_id))
        connection.execute("UPDATE requests SET state=? WHERE project_id=? AND target_id=? AND operation='ingest'",
                           (state, project_id, report_id))

    def _purge(self, connection, project_id, now, policy):
        expired = [row[0] for row in connection.execute(
            "SELECT id FROM reports WHERE project_id=? AND state='active' AND expires_at<=?", (project_id, now))]
        for report_id in expired:
            self._remove_report(connection, project_id, report_id, "expired")
        records = [row[0] for row in connection.execute(
            "SELECT id FROM project_records WHERE project_id=? AND expires_at<=?", (project_id, now))]
        self._remove_records(connection, project_id, records)
        backups = connection.execute("DELETE FROM backups WHERE project_id=? AND expires_at<=?", (project_id, now)).rowcount
        connection.execute("DELETE FROM requests WHERE project_id=? AND expires_at<=?", (project_id, now))
        connection.execute("DELETE FROM reports WHERE project_id=? AND state!='active' AND history_until<=?", (project_id, now))
        connection.execute("DELETE FROM credentials WHERE project_id=? AND expires_at<=?", (project_id, now))
        self._trim_audit(connection, project_id, now, policy)
        return {"reports": len(expired), "records": len(records), "backups": backups}

    def _usage(self, connection, project_id=None):
        total = 0
        for table in ("reports", "backups", "project_records"):
            where, parameters = (" WHERE project_id=?", (project_id,)) if project_id else ("", ())
            total += connection.execute(f"SELECT coalesce(sum(stored_bytes),0) FROM {table}{where}", parameters).fetchone()[0]
        return total

    def _admit_bytes(self, connection, project_id, policy, added):
        if (self._usage(connection, project_id) + added > policy.max_bytes
                or self._usage(connection) + added > self.limits.max_global_bytes):
            raise TeamError("quota", "Stored evidence would exceed a project or global byte limit")

    def _request(self, connection, project_id, request_id, digest, operation):
        if request_id is None:
            return None
        row = connection.execute("SELECT * FROM requests WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
        if row is None:
            if connection.execute("SELECT count(*) FROM requests WHERE project_id=?", (project_id,)).fetchone()[0] >= self.limits.max_requests:
                raise TeamError("quota", "Retry authority is full until retained requests expire")
            return None
        if row["input_sha256"] != digest or row["operation"] != operation:
            raise TeamError("conflict", "Request ID was already used with different content")
        if row["state"] != "committed":
            raise TeamError("conflict", "Request was deleted or expired and cannot be replayed")
        return row["target_id"]

    @staticmethod
    def _remember(connection, project_id, request_id, digest, target_id, operation, now):
        if request_id is not None:
            connection.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)",
                               (project_id, request_id, digest, target_id, operation, now, now + HISTORY_SECONDS, "committed"))

    @staticmethod
    def _report_meta(project_id, row, request_id=None, replayed=False):
        return {"team_schema": 1, "project_id": project_id, "report_id": row["id"],
                "request_id": request_id, "replayed": replayed, "sha256": row["sha256"],
                "stored_bytes": row["stored_bytes"], "created_at": _iso(row["created_at"]),
                "expires_at": _iso(row["expires_at"]), "privacy_profile": row["privacy_profile"],
                "summary": json.loads(row["summary"])}

    @staticmethod
    def _report_row(connection, project_id, report_id):
        row = connection.execute("SELECT * FROM reports WHERE project_id=? AND id=? AND state='active'",
                                 (project_id, report_id)).fetchone()
        if row is None:
            raise TeamError("not_found", "Report is not available")
        return row

    def ingest(self, actor, raw: bytes, *, request_id: str | None = None) -> dict:
        # Never parse unauthenticated payloads. Recheck current role/policy at admission.
        with self._authorized(actor, "operator"):
            pass
        if type(raw) is not bytes or len(raw) > self.limits.max_upload_bytes:
            raise TeamError("invalid", "Upload must be immutable bytes within the configured size limit")
        request_id = _id(request_id) if request_id is not None else None
        digest = hashlib.sha256(raw).hexdigest()
        try:
            source = parse_report(raw)
        except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
            raise TeamError("invalid", "Upload is not a supported bounded report") from None
        details = {}
        with self._authorized(actor, "operator", action="report_ingest", details=details) as (connection, _row, policy, now):
            existing = self._request(connection, actor.project_id, request_id, digest, "ingest")
            if existing is not None:
                row = self._report_row(connection, actor.project_id, existing)
                details.update(report_id=existing, replayed=True)
                return self._report_meta(actor.project_id, row, request_id, True)
            try:
                redacted = redact_report(source, policy.privacy_profile)
                payload, summary = _json(redacted), _json(session_summary(redacted))
            except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
                raise TeamError("invalid", "Upload cannot be transformed into supported private evidence") from None
            size = len(payload) + len(summary)
            self._admit_bytes(connection, actor.project_id, policy, size)
            active, total = connection.execute(
                "SELECT coalesce(sum(state='active'),0),count(*) FROM reports WHERE project_id=?", (actor.project_id,)).fetchone()
            if active >= policy.max_reports or total >= self.limits.max_requests:
                raise TeamError("quota", "Project report or deletion-history limit reached")
            report_id = uuid4().hex
            connection.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (actor.project_id, report_id, now, now + policy.retention_seconds, payload, summary,
                                hashlib.sha256(payload).hexdigest(), size, policy.privacy_profile, "active", now + HISTORY_SECONDS))
            self._remember(connection, actor.project_id, request_id, digest, report_id, "ingest", now)
            details.update(report_id=report_id, stored_bytes=size, replayed=False)
            return self._report_meta(actor.project_id, self._report_row(connection, actor.project_id, report_id), request_id)

    def list_reports(self, actor, *, limit: int = 100, cursor: str | None = None) -> dict:
        limit = integer(limit, "limit", 1, 100)
        cursor = _id(cursor) if cursor is not None else ""
        with self._authorized(actor, "viewer") as (connection, _row, _policy, _now):
            rows = connection.execute("SELECT * FROM reports WHERE project_id=? AND state='active' AND id>? ORDER BY id LIMIT ?",
                                      (actor.project_id, cursor, limit + 1)).fetchall()
            return {"team_schema": 1, "project_id": actor.project_id,
                    "reports": [self._report_meta(actor.project_id, row) for row in rows[:limit]],
                    "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}

    def read_report(self, actor, report_id: str) -> dict:
        report_id = _id(report_id)
        with self._authorized(actor, "viewer", action="report_read", details={"report_id": report_id}) as (connection, _row, _policy, _now):
            row = self._report_row(connection, actor.project_id, report_id)
            if type(row["payload"]) is not bytes or hashlib.sha256(row["payload"]).hexdigest() != row["sha256"]:
                raise TeamError("storage", "Stored report is damaged; inspect authoritative storage locally")
            try:
                report = parse_report(row["payload"])
            except ValueError:
                raise TeamError("storage", "Stored report is no longer readable") from None
            return {**self._report_meta(actor.project_id, row), "report": report}

    def export_report(self, actor, report_id: str) -> bytes:
        return _json(self.read_report(actor, report_id)["report"])

    def delete_report(self, actor, report_id: str) -> dict:
        report_id = _id(report_id)
        with self._authorized(actor, "admin", action="report_delete", details={"report_id": report_id}) as (connection, _row, _policy, _now):
            row = connection.execute("SELECT state FROM reports WHERE project_id=? AND id=?", (actor.project_id, report_id)).fetchone()
            if row is None:
                raise TeamError("not_found", "Report is not available")
            removed = row["state"] == "active"
            self._remove_report(connection, actor.project_id, report_id, "deleted")
            return {"team_schema": 1, "project_id": actor.project_id, "report_id": report_id, "deleted": removed}

    def purge(self, actor) -> dict:
        with self._authorized(actor, "admin", action="retention_purge") as (connection, _row, policy, now):
            counts = self._purge(connection, actor.project_id, now, policy)
            return {"team_schema": 1, "project_id": actor.project_id, "purged": counts}

    def update_policy(self, actor, policy: ProjectPolicy | dict) -> dict:
        if type(policy) is dict:
            try:
                policy = ProjectPolicy(**policy)
            except (ValueError, TypeError):
                raise TeamError("invalid", "Policy fields are not supported") from None
        if type(policy) is not ProjectPolicy:
            raise TeamError("invalid", "Policy must be ProjectPolicy or an exact policy object")
        policy = policy.resolved(self.limits)
        with self._authorized(actor, "admin", action="policy_update") as (connection, _row, previous, now):
            # A stricter profile is applied to existing evidence; no backup may retain its old form.
            if policy.privacy_profile == "minimal" and previous.privacy_profile != "minimal":
                connection.execute("DELETE FROM backups WHERE project_id=?", (actor.project_id,))
                for row in connection.execute("SELECT * FROM reports WHERE project_id=? AND state='active'", (actor.project_id,)).fetchall():
                    try:
                        value = redact_report(parse_report(row["payload"]), "minimal")
                        payload, summary = _json(value), _json(session_summary(value))
                    except (ValueError, TypeError):
                        raise TeamError("storage", "Existing evidence cannot satisfy the stricter privacy policy") from None
                    connection.execute("UPDATE reports SET payload=?,summary=?,sha256=?,stored_bytes=?,privacy_profile='minimal' WHERE project_id=? AND id=?",
                                       (payload, summary, hashlib.sha256(payload).hexdigest(), len(payload) + len(summary), actor.project_id, row["id"]))
            connection.execute("UPDATE reports SET expires_at=min(expires_at,created_at+?) WHERE project_id=?", (policy.retention_seconds, actor.project_id))
            connection.execute("UPDATE backups SET expires_at=min(expires_at,created_at+?) WHERE project_id=?", (policy.backup_ttl_seconds, actor.project_id))
            connection.execute("UPDATE project_records SET expires_at=min(expires_at,created_at+?) WHERE project_id=?", (policy.retention_seconds, actor.project_id))
            connection.execute("UPDATE project_records SET expires_at=min(expires_at,coalesce((SELECT min(r.expires_at) FROM record_report_links l JOIN reports r "
                               "ON r.project_id=l.project_id AND r.id=l.report_id WHERE l.project_id=project_records.project_id AND l.record_id=project_records.id),expires_at)) WHERE project_id=?", (actor.project_id,))
            connection.execute("UPDATE project_records SET expires_at=min(expires_at,(SELECT a.expires_at FROM project_records a WHERE a.project_id=project_records.project_id AND a.id=project_records.approval_id)) "
                               "WHERE project_id=? AND approval_id IS NOT NULL", (actor.project_id,))
            self._purge(connection, actor.project_id, now, policy)
            if (self._usage(connection, actor.project_id) > policy.max_bytes
                    or connection.execute("SELECT count(*) FROM reports WHERE project_id=? AND state='active'", (actor.project_id,)).fetchone()[0] > policy.max_reports
                    or connection.execute("SELECT count(*) FROM backups WHERE project_id=?", (actor.project_id,)).fetchone()[0] > policy.max_backups):
                raise TeamError("quota", "Delete existing evidence before lowering its quota")
            connection.execute("UPDATE projects SET policy=? WHERE id=?", (_json(policy.to_dict()).decode("ascii"), actor.project_id))
            return {"team_schema": 1, "project_id": actor.project_id, "policy": policy.to_dict()}
