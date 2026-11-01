"""Sanitized logical snapshots that cannot restore authority or deleted evidence."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from .report_contract import parse_report, session_summary
from .team_evidence import _id, _iso, _json
from .team_models import TeamError


class _Backups:
    @staticmethod
    def _backup_meta(project_id, row):
        return {"team_schema": 1, "project_id": project_id, "backup_id": row["id"],
                "sha256": row["sha256"], "stored_bytes": row["stored_bytes"],
                "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"])}

    def backup(self, actor) -> dict:
        details = {}
        with self._authorized(actor, "admin", action="backup_create", details=details) as (connection, _row, policy, now):
            if connection.execute("SELECT count(*) FROM backups WHERE project_id=?", (actor.project_id,)).fetchone()[0] >= policy.max_backups:
                raise TeamError("quota", "Project backup limit reached")
            rows = connection.execute("SELECT * FROM reports WHERE project_id=? AND state='active' ORDER BY id", (actor.project_id,)).fetchall()
            if not rows:
                raise TeamError("not_found", "There are no live reports to snapshot")
            if sum(row["stored_bytes"] for row in rows) > self.limits.max_backup_bytes:
                raise TeamError("quota", "Reports exceed the managed snapshot byte limit")
            members = []
            for row in rows:
                if type(row["payload"]) is not bytes or hashlib.sha256(row["payload"]).hexdigest() != row["sha256"]:
                    raise TeamError("storage", "Damaged evidence cannot become a managed snapshot")
                try:
                    report = parse_report(row["payload"])
                except ValueError:
                    raise TeamError("storage", "Unreadable evidence cannot become a managed snapshot") from None
                members.append({"id": row["id"], "sha256": row["sha256"], "report": report})
            payload = _json({"backup_schema": 1, "store_id": self.store_id, "project_id": actor.project_id, "reports": members})
            if len(payload) > self.limits.max_backup_bytes:
                raise TeamError("quota", "Managed snapshot exceeds its byte limit")
            self._admit_bytes(connection, actor.project_id, policy, len(payload))
            backup_id, expires = uuid4().hex, min(now + policy.backup_ttl_seconds, *(row["expires_at"] for row in rows))
            connection.execute("INSERT INTO backups VALUES(?,?,?,?,?,?,?)",
                               (actor.project_id, backup_id, now, expires, payload, hashlib.sha256(payload).hexdigest(), len(payload)))
            connection.executemany("INSERT INTO backup_members VALUES(?,?,?)",
                                   ((actor.project_id, backup_id, row["id"]) for row in rows))
            details.update(backup_id=backup_id, reports=len(rows), stored_bytes=len(payload))
            result = self._backup_meta(actor.project_id, connection.execute(
                "SELECT * FROM backups WHERE project_id=? AND id=?", (actor.project_id, backup_id)).fetchone())
            return {**result, "report_ids": [row["id"] for row in rows]}

    def list_backups(self, actor) -> dict:
        with self._authorized(actor, "admin") as (connection, _row, _policy, _now):
            rows = connection.execute("SELECT * FROM backups WHERE project_id=? ORDER BY id", (actor.project_id,)).fetchall()
            return {"team_schema": 1, "project_id": actor.project_id,
                    "backups": [self._backup_meta(actor.project_id, row) for row in rows]}

    def delete_backup(self, actor, backup_id: str) -> dict:
        backup_id = _id(backup_id)
        with self._authorized(actor, "admin", action="backup_delete", details={"backup_id": backup_id}) as (connection, _row, _policy, _now):
            removed = connection.execute("DELETE FROM backups WHERE project_id=? AND id=?", (actor.project_id, backup_id)).rowcount
            return {"team_schema": 1, "project_id": actor.project_id, "backup_id": backup_id, "deleted": bool(removed)}

    def restore(self, actor, backup_id: str) -> dict:
        backup_id = _id(backup_id)
        details = {"backup_id": backup_id}
        with self._authorized(actor, "admin", action="backup_restore", details=details) as (connection, _row, policy, now):
            backup = connection.execute("SELECT * FROM backups WHERE project_id=? AND id=?", (actor.project_id, backup_id)).fetchone()
            if backup is None:
                raise TeamError("not_found", "Managed snapshot is no longer available")
            raw = backup["payload"]
            if (type(raw) is not bytes or len(raw) > self.limits.max_backup_bytes
                    or hashlib.sha256(raw).hexdigest() != backup["sha256"]):
                raise TeamError("storage", "Managed snapshot is damaged")
            try:
                value = json.loads(raw)
                if (set(value) != {"backup_schema", "store_id", "project_id", "reports"}
                        or value["backup_schema"] != 1 or value["store_id"] != self.store_id
                        or value["project_id"] != actor.project_id or type(value["reports"]) is not list
                        or not 1 <= len(value["reports"]) <= self.limits.max_reports):
                    raise ValueError
                expected = {row[0] for row in connection.execute("SELECT report_id FROM backup_members WHERE project_id=? AND backup_id=?",
                                                               (actor.project_id, backup_id))}
                seen, replacements = set(), []
                for item in value["reports"]:
                    if type(item) is not dict or set(item) != {"id", "sha256", "report"} or item["id"] in seen:
                        raise ValueError
                    report_id = _id(item["id"])
                    seen.add(report_id)
                    authority = self._report_row(connection, actor.project_id, report_id)
                    payload = _json(item["report"])
                    if (authority["expires_at"] <= now or item["sha256"] != authority["sha256"]
                            or hashlib.sha256(payload).hexdigest() != authority["sha256"]
                            or len(payload) > self.limits.max_upload_bytes * 2):
                        raise ValueError
                    report = parse_report(payload)
                    if report.get("privacy", {}).get("profile") != authority["privacy_profile"]:
                        raise ValueError
                    summary = _json(session_summary(report))
                    size = len(payload) + len(summary)
                    replacements.append((payload, summary, size, actor.project_id, report_id))
                if seen != expected:
                    raise ValueError
            except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
                raise TeamError("storage", "Snapshot no longer matches live report authority") from None
            connection.executemany("UPDATE reports SET payload=?,summary=?,stored_bytes=? WHERE project_id=? AND id=?", replacements)
            self._admit_bytes(connection, actor.project_id, policy, 0)
            details.update(reports=len(replacements))
            return {"team_schema": 1, "project_id": actor.project_id, "backup_id": backup_id,
                    "restored": len(replacements), "report_ids": sorted(seen)}
