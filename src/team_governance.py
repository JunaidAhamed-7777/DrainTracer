"""Project-bound explicit baseline approvals and client-computed receipts."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from .team_evidence import _id, _iso, _json
from .team_models import TeamError, duration, integer, text
from .team_records import validate_record_value

_KINDS = {"baseline_approval": "admin", "comparison_receipt": "operator"}
_BINDINGS = ("repository", "workload_id", "baseline_revision", "audit_head")


def _kind(value):
    if type(value) is not str or value not in _KINDS:
        raise TeamError("invalid", "Record kind is not supported")
    return value


def _key(value):
    if value is None:
        return None
    value = text(value, "key", 128)
    if not value.isprintable():
        raise TeamError("invalid", "Record key contains unsupported characters")
    return value


class _Governance:
    @staticmethod
    def _record_row(connection, project_id, record_id):
        row = connection.execute("SELECT * FROM project_records WHERE project_id=? AND id=?", (project_id, record_id)).fetchone()
        if row is None:
            raise TeamError("not_found", "Record is not available")
        return row

    @staticmethod
    def _record_meta(connection, project_id, row, request_id=None, replayed=False):
        raw = row["payload"]
        if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise TeamError("storage", "Stored record is damaged")
        try:
            value = validate_record_value(row["kind"], json.loads(raw))
        except (ValueError, TypeError, UnicodeError):
            raise TeamError("storage", "Stored record has unsupported fields") from None
        links = [item[0] for item in connection.execute("SELECT report_id FROM record_report_links WHERE project_id=? AND record_id=? ORDER BY report_id",
                                                       (project_id, row["id"]))]
        return {"team_schema": 1, "project_id": project_id, "record_id": row["id"], "kind": row["kind"],
                "key": row["record_key"], "request_id": request_id, "replayed": replayed,
                "sha256": row["sha256"], "stored_bytes": row["stored_bytes"],
                "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"]),
                "value": value, "report_ids": links}

    def create_record(self, actor, kind: str, value: dict, *, key: str | None = None,
                      report_ids=(), ttl_seconds: float | None = None, request_id: str | None = None) -> dict:
        kind = _kind(kind)
        with self._authorized(actor, _KINDS[kind]):
            pass
        value, key = validate_record_value(kind, value), _key(key)
        if type(report_ids) not in (tuple, list) or len(report_ids) > 20:
            raise TeamError("invalid", "At most 20 report identifiers may be linked")
        links = sorted(_id(item) for item in report_ids)
        if len(set(links)) != len(links):
            raise TeamError("invalid", "Linked report identifiers must be distinct")
        if ttl_seconds is not None:
            ttl_seconds = duration(ttl_seconds, "ttl_seconds", 1, 30 * 86400)
        request_id = _id(request_id) if request_id is not None else None
        payload = _json(value)
        if len(payload) > self.limits.max_record_bytes:
            raise TeamError("quota", "Record exceeds the configured byte limit")
        digest = hashlib.sha256(_json({"kind": kind, "value": value, "key": key, "report_ids": links,
                                      "ttl_seconds": ttl_seconds})).hexdigest()
        details = {"kind": kind}
        with self._authorized(actor, _KINDS[kind], action="record_create", details=details) as (connection, _row, policy, now):
            existing = self._request(connection, actor.project_id, request_id, digest, "record")
            if existing is not None:
                details.update(record_id=existing, replayed=True)
                return self._record_meta(connection, actor.project_id, self._record_row(connection, actor.project_id, existing), request_id, True)
            expires = now + min(ttl_seconds if ttl_seconds is not None else policy.retention_seconds, policy.retention_seconds)
            for report_id in links:
                report = self._report_row(connection, actor.project_id, report_id)
                expires = min(expires, report["expires_at"])
            approval_id = None
            if kind == "comparison_receipt":
                approval_id = value["approval_id"]
                approval = self._record_row(connection, actor.project_id, approval_id)
                if approval["kind"] != "baseline_approval":
                    raise TeamError("invalid", "Receipt must reference a live baseline approval")
                approved = self._record_meta(connection, actor.project_id, approval)["value"]
                if any(approved[field] != value[field] for field in _BINDINGS):
                    raise TeamError("invalid", "Receipt does not match its project baseline approval")
                expires = min(expires, approval["expires_at"])
            if connection.execute("SELECT count(*) FROM project_records WHERE project_id=?", (actor.project_id,)).fetchone()[0] >= self.limits.max_records:
                raise TeamError("quota", "Project record limit reached")
            self._admit_bytes(connection, actor.project_id, policy, len(payload))
            record_id = uuid4().hex
            connection.execute("INSERT INTO project_records VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (actor.project_id, record_id, kind, key, now, expires, payload,
                                hashlib.sha256(payload).hexdigest(), len(payload), approval_id))
            connection.executemany("INSERT INTO record_report_links VALUES(?,?,?)", ((actor.project_id, record_id, report_id) for report_id in links))
            self._remember(connection, actor.project_id, request_id, digest, record_id, "record", now)
            details.update(record_id=record_id, replayed=False)
            return self._record_meta(connection, actor.project_id, self._record_row(connection, actor.project_id, record_id), request_id)

    def list_records(self, actor, *, kind: str | None = None, key: str | None = None,
                     limit: int = 100, cursor: str | None = None) -> dict:
        kind, key = _kind(kind) if kind is not None else None, _key(key)
        limit, cursor = integer(limit, "limit", 1, 100), _id(cursor) if cursor is not None else ""
        with self._authorized(actor, "viewer") as (connection, _row, _policy, _now):
            rows = connection.execute("SELECT * FROM project_records WHERE project_id=? AND id>? AND (? IS NULL OR kind=?) "
                                      "AND (? IS NULL OR record_key=?) ORDER BY id LIMIT ?",
                                      (actor.project_id, cursor, kind, kind, key, key, limit + 1)).fetchall()
            return {"team_schema": 1, "project_id": actor.project_id,
                    "records": [self._record_meta(connection, actor.project_id, row) for row in rows[:limit]],
                    "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}

    def read_record(self, actor, record_id: str) -> dict:
        record_id = _id(record_id)
        with self._authorized(actor, "viewer", action="record_read", details={"record_id": record_id}) as (connection, _row, _policy, _now):
            return self._record_meta(connection, actor.project_id, self._record_row(connection, actor.project_id, record_id))

    def delete_record(self, actor, record_id: str) -> dict:
        record_id = _id(record_id)
        with self._authorized(actor, "admin", action="record_delete", details={"record_id": record_id}) as (connection, _row, _policy, _now):
            row = connection.execute("SELECT id FROM project_records WHERE project_id=? AND id=?", (actor.project_id, record_id)).fetchone()
            self._remove_records(connection, actor.project_id, [record_id])
            return {"team_schema": 1, "project_id": actor.project_id, "record_id": record_id, "deleted": row is not None}
