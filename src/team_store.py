"""Project-scoped local authority; every operation revalidates its bearer."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from .team_backups import _Backups
from .team_database import _Database
from .team_evidence import _Evidence
from .team_governance import _Governance
from .team_models import (
    Actor,
    ProjectPolicy,
    StoreLimits,
    TeamError,
    _make_actor,
    duration,
    integer,
    text,
)

_ID = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"dt1\.([0-9a-f]{32})\.([0-9a-f]{32})\.([0-9a-f]{64})")
_RANK = {"viewer": 0, "operator": 1, "admin": 2}
MAX_CREDENTIAL_TTL = 30 * 86400
_MAX_SEQUENCE = 2**63 - 1


def _id(value) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TeamError("invalid", "Identifier must be lowercase UUID hex")
    return value


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _denied() -> TeamError:
    return TeamError("unauthorized", "Credential is not valid for this project")


class _ExpiredCredential(Exception):
    def __init__(self, row):
        self.row = row


class TeamStore(_Governance, _Backups, _Evidence, _Database):
    """No server/listener activation and no trust in a caller's cached role."""

    def _now(self) -> float:
        try:
            value = self._clock()
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253399708799:
                raise TypeError
        except (ValueError, TypeError, OverflowError):
            raise TeamError("storage", "System clock is outside the supported authority range") from None
        return float(value)

    def _policy(self, row) -> ProjectPolicy:
        try:
            if len(row["policy"].encode("utf-8")) > 8192:
                raise ValueError
            value = json.loads(row["policy"])
            if not isinstance(value, dict):
                raise TypeError
            return ProjectPolicy(**value).resolved(self.limits)
        except (ValueError, TypeError, AttributeError):
            raise TeamError("storage", "Project policy is missing or unusable") from None

    @staticmethod
    def _parts(token: str, project_id: str):
        if (not isinstance(token, str) or len(token) != 134 or not isinstance(project_id, str)
                or not _ID.fullmatch(project_id)):
            raise _denied()
        match = _TOKEN.fullmatch(token)
        if not match or not hmac.compare_digest(match[1], project_id):
            raise _denied()
        return match[1], match[2]

    def _identity(self, connection, token: str, project_id: str, now: float):
        project_id, credential_id = self._parts(token, project_id)
        row = connection.execute(
            "SELECT c.*,p.policy,p.deleted_at FROM credentials c JOIN projects p ON p.id=c.project_id "
            "WHERE c.project_id=? AND c.id=?", (project_id, credential_id),
        ).fetchone()
        digest = hashlib.sha256(token.encode("ascii")).digest()
        if (row is None or row["deleted_at"] is not None or type(row["digest"]) is not bytes
                or len(row["digest"]) != 32 or not hmac.compare_digest(row["digest"], digest)):
            raise _denied()
        created, expires = row["created_at"], row["expires_at"]
        if (row["role"] not in _RANK or type(created) not in (int, float) or type(expires) not in (int, float)
                or not math.isfinite(created) or not math.isfinite(expires) or created > now
                or not 0 < expires - created <= MAX_CREDENTIAL_TTL):
            raise _denied()
        if expires <= now:
            raise _ExpiredCredential(row)
        return row

    def _login(self, connection, token, project_id, now):
        try:
            return self._identity(connection, token, project_id, now)
        except _ExpiredCredential as expired:
            row = expired.row
            policy = self._policy(row)
            connection.execute("DELETE FROM credentials WHERE project_id=? AND id=?", (project_id, row["id"]))
            self._audit(connection, project_id, row["id"], "credential_expired", "ok", {}, now, policy)
            connection.commit()
            raise _denied() from None

    def authenticate(self, token: str, project_id: str) -> Actor:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._login(connection, token, project_id, self._now())
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return _make_actor(project_id, row["id"], token)

    @contextmanager
    def _authorized(self, actor: Actor, role: str, *, action: str | None = None, details: dict | None = None):
        if type(actor) is not Actor:
            raise _denied()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                row = self._login(connection, actor._token, actor.project_id, now)
                if row["id"] != actor.credential_id:
                    raise _denied()
                policy = self._policy(row)
                if _RANK[row["role"]] < _RANK[role]:
                    self._audit(connection, actor.project_id, actor.credential_id, action or "access",
                                "denied", {}, now, policy)
                    connection.commit()
                    raise TeamError("forbidden", "Credential role does not permit this operation")
                self._purge(connection, actor.project_id, now, policy)
                connection.execute("SAVEPOINT operation")
                try:
                    yield connection, row, policy, now
                except TeamError:
                    connection.execute("ROLLBACK TO operation")
                    connection.execute("RELEASE operation")
                    connection.commit()
                    raise
                connection.execute("RELEASE operation")
                if action:
                    self._audit(connection, actor.project_id, actor.credential_id, action, "ok", details or {}, now, policy)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _audit(self, connection, project_id, credential_id, action, result, details, now, policy):
        encoded_details = _json(details)
        if len(encoded_details) > 1024:
            raise TeamError("storage", "Audit event exceeds its fixed metadata bound")
        row = connection.execute("SELECT audit_seen,audit_dropped FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None or type(row["audit_seen"]) is not int or not 0 <= row["audit_seen"] < _MAX_SEQUENCE:
            raise TeamError("storage", "Project audit counter is unusable")
        sequence = row["audit_seen"] + 1
        size = len(encoded_details) + len(action) + len(result) + 128
        connection.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?)",
                           (project_id, sequence, now, credential_id, action, result, encoded_details.decode("ascii"), size))
        connection.execute("UPDATE projects SET audit_seen=? WHERE id=?", (sequence, project_id))
        self._trim_audit(connection, project_id, now, policy)

    def _trim_audit(self, connection, project_id, now, policy):
        deleted = connection.execute("DELETE FROM audit_events WHERE project_id=? AND timestamp<=?",
                                     (project_id, now - policy.audit_retention_seconds)).rowcount
        retained = connection.execute("SELECT sequence,stored_bytes FROM audit_events WHERE project_id=? ORDER BY sequence",
                                      (project_id,)).fetchall()
        count, total = len(retained), sum(item["stored_bytes"] for item in retained)
        for item in retained:
            if count <= self.limits.max_audit_events and total <= self.limits.max_audit_bytes:
                break
            connection.execute("DELETE FROM audit_events WHERE project_id=? AND sequence=?", (project_id, item["sequence"]))
            count -= 1
            total -= item["stored_bytes"]
            deleted += 1
        if deleted:
            connection.execute("UPDATE projects SET audit_dropped=audit_dropped+? WHERE id=?", (deleted, project_id))

    @staticmethod
    def _issued(project_id, credential_id, role, token, created, expires):
        return {"team_schema": 1, "project_id": project_id,
                "credential": {"id": credential_id, "role": role, "created_at": _iso(created), "expires_at": _iso(expires)},
                "token": token}

    def _issue(self, connection, project_id, role, ttl_seconds, now):
        if not isinstance(role, str) or role not in _RANK:
            raise TeamError("invalid", "Credential role must be viewer, operator or admin")
        ttl = duration(ttl_seconds, "ttl_seconds", 1, MAX_CREDENTIAL_TTL)
        connection.execute("DELETE FROM credentials WHERE project_id=? AND expires_at<=?", (project_id, now))
        count = connection.execute("SELECT count(*) FROM credentials WHERE project_id=?", (project_id,)).fetchone()[0]
        if count >= self.limits.max_credentials:
            raise TeamError("quota", "Project credential limit reached")
        credential_id = uuid4().hex
        token = f"dt1.{project_id}.{credential_id}.{secrets.token_hex(32)}"
        connection.execute("INSERT INTO credentials VALUES(?,?,?,?,?,?)",
                           (project_id, credential_id, hashlib.sha256(token.encode("ascii")).digest(), role, now, now + ttl))
        return self._issued(project_id, credential_id, role, token, now, now + ttl)

    def bootstrap_project(self, name: str, *, policy: ProjectPolicy | None = None,
                          credential_ttl: float = MAX_CREDENTIAL_TTL) -> dict:
        """OS-local administration only; transports must not expose this method."""
        name = text(name, "name", 128)
        if policy is not None and type(policy) is not ProjectPolicy:
            raise TeamError("invalid", "policy must be ProjectPolicy")
        policy = (policy or ProjectPolicy()).resolved(self.limits)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active, total = connection.execute("SELECT sum(deleted_at IS NULL),count(*) FROM projects").fetchone()
                if (active or 0) >= self.limits.max_projects or total >= 1024:
                    raise TeamError("quota", "Project or authority-history limit reached")
                now, project_id = self._now(), uuid4().hex
                connection.execute("INSERT INTO projects(id,name,created_at,policy) VALUES(?,?,?,?)",
                                   (project_id, name, now, _json(policy.to_dict()).decode("ascii")))
                issued = self._issue(connection, project_id, "admin", credential_ttl, now)
                self._audit(connection, project_id, None, "project_bootstrap", "ok",
                            {"credential_id": issued["credential"]["id"]}, now, policy)
                connection.commit()
                return issued
            except BaseException:
                connection.rollback()
                raise

    def recover_admin(self, project_id: str, *, credential_ttl: float = MAX_CREDENTIAL_TTL) -> dict:
        """Explicit OS-local recovery revokes existing keys; it cannot recreate deleted projects."""
        project_id = _id(project_id)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM projects WHERE id=? AND deleted_at IS NULL", (project_id,)).fetchone()
                if row is None:
                    raise TeamError("not_found", "Project is not available")
                policy, now = self._policy(row), self._now()
                removed = connection.execute("DELETE FROM credentials WHERE project_id=?", (project_id,)).rowcount
                issued = self._issue(connection, project_id, "admin", credential_ttl, now)
                self._audit(connection, project_id, None, "local_admin_recovery", "ok",
                            {"revoked": removed, "credential_id": issued["credential"]["id"]}, now, policy)
                connection.commit()
                return issued
            except BaseException:
                connection.rollback()
                raise

    def list_projects_local(self) -> dict:
        """OS-local administration; never expose this method through a transport."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT id,name,created_at,deleted_at FROM projects ORDER BY id").fetchall()
            connection.commit()
            return {"team_schema": 1, "store_id": self.store_id, "projects": [
                {"project_id": row["id"], "name": row["name"], "created_at": _iso(row["created_at"]),
                 "deleted_at": _iso(row["deleted_at"]) if row["deleted_at"] is not None else None} for row in rows]}

    def delete_project(self, actor: Actor) -> dict:
        """Delete project evidence and revoke every credential in one transaction."""
        with self._authorized(actor, "admin", action="project_delete") as (connection, _row, _policy, now):
            reports = [row[0] for row in connection.execute("SELECT id FROM reports WHERE project_id=?", (actor.project_id,))]
            for report_id in reports:
                self._remove_report(connection, actor.project_id, report_id, "deleted")
            connection.execute("DELETE FROM backups WHERE project_id=?", (actor.project_id,))
            records = [row[0] for row in connection.execute("SELECT id FROM project_records WHERE project_id=?", (actor.project_id,))]
            self._remove_records(connection, actor.project_id, records)
            connection.execute("DELETE FROM credentials WHERE project_id=?", (actor.project_id,))
            connection.execute("UPDATE projects SET deleted_at=? WHERE id=?", (now, actor.project_id))
            return {"team_schema": 1, "project_id": actor.project_id, "deleted": True}

    def purge_local(self) -> dict:
        """OS-local scheduled maintenance can expire inactive projects' retained data."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now, counts = self._now(), {"reports": 0, "records": 0, "backups": 0}
                for row in connection.execute("SELECT * FROM projects").fetchall():
                    policy = self._policy(row)
                    removed = self._purge(connection, row["id"], now, policy)
                    for key in counts:
                        counts[key] += removed[key]
                connection.commit()
                return {"team_schema": 1, "store_id": self.store_id, "purged": counts}
            except BaseException:
                connection.rollback()
                raise

    def whoami(self, actor: Actor) -> dict:
        with self._authorized(actor, "viewer") as (_connection, row, _policy, _now):
            return {"team_schema": 1, "project_id": actor.project_id, "credential_id": row["id"],
                    "role": row["role"], "expires_at": _iso(row["expires_at"])}

    def get_policy(self, actor: Actor) -> dict:
        with self._authorized(actor, "viewer") as (_connection, _row, policy, _now):
            return {"team_schema": 1, "project_id": actor.project_id, "policy": policy.to_dict()}

    def issue_credential(self, actor: Actor, role: str, *, ttl_seconds: float = 86400) -> dict:
        details = {}
        with self._authorized(actor, "admin", action="credential_issue", details=details) as (connection, _row, _policy, now):
            issued = self._issue(connection, actor.project_id, role, ttl_seconds, now)
            details.update(credential_id=issued["credential"]["id"], role=role)
            return issued

    def list_credentials(self, actor: Actor) -> dict:
        with self._authorized(actor, "admin") as (connection, _row, _policy, now):
            rows = connection.execute("SELECT id,role,created_at,expires_at FROM credentials WHERE project_id=? AND expires_at>? ORDER BY id",
                                      (actor.project_id, now)).fetchall()
            return {"team_schema": 1, "project_id": actor.project_id, "credentials": [
                {"id": row["id"], "role": row["role"], "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"])}
                for row in rows]}

    def revoke_credential(self, actor: Actor, credential_id: str) -> dict:
        credential_id = _id(credential_id)
        with self._authorized(actor, "admin", action="credential_revoke", details={"credential_id": credential_id}) as (connection, _row, _policy, _now):
            removed = connection.execute("DELETE FROM credentials WHERE project_id=? AND id=?", (actor.project_id, credential_id)).rowcount
            return {"team_schema": 1, "project_id": actor.project_id, "credential_id": credential_id, "revoked": bool(removed)}

    def audit(self, actor: Actor, *, after: int = 0, limit: int = 100) -> dict:
        after, limit = integer(after, "after", 0, _MAX_SEQUENCE), integer(limit, "limit", 1, 100)
        with self._authorized(actor, "admin") as (connection, _row, policy, now):
            self._trim_audit(connection, actor.project_id, now, policy)
            rows = connection.execute("SELECT * FROM audit_events WHERE project_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                                      (actor.project_id, after, limit + 1)).fetchall()
            counts = connection.execute("SELECT audit_seen,audit_dropped FROM projects WHERE id=?", (actor.project_id,)).fetchone()
            events = [{"sequence": row["sequence"], "timestamp": _iso(row["timestamp"]),
                       "credential_id": row["credential_id"], "action": row["action"], "result": row["result"],
                       "details": json.loads(row["details"])} for row in rows[:limit]]
            return {"team_schema": 1, "project_id": actor.project_id, "events": events,
                    "observed": counts["audit_seen"], "dropped": counts["audit_dropped"],
                    "next_sequence": rows[limit - 1]["sequence"] if len(rows) > limit else None}


__all__ = ["Actor", "ProjectPolicy", "StoreLimits", "TeamError", "TeamStore"]
