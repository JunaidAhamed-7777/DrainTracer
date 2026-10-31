"""Bounded options and opaque credential handles for the local team core."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace

MIB = 1024 * 1024
TEAM_SCHEMA = 1
ROLES = ("viewer", "operator", "admin")
_ACTOR_PROOF = object()


class TeamError(ValueError):
    """A safe error for project callers; messages never include report or token bytes."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def integer(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise TeamError("invalid", f"{name} must be an integer from {minimum} to {maximum}")
    return value


def duration(value, name: str, minimum: float, maximum: float) -> float:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not minimum <= value <= maximum):
        raise TeamError("invalid", f"{name} is outside its allowed duration range")
    return float(value)


def text(value, name: str, maximum: int, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum or value != value.strip()
            or (not empty and not value) or any(ord(character) < 32 for character in value)):
        raise TeamError("invalid", f"{name} must be bounded text without controls or surrounding whitespace")
    return value


@dataclass(frozen=True)
class StoreLimits:
    max_upload_bytes: int = 8 * MIB
    max_project_bytes: int = 64 * MIB
    max_global_bytes: int = 256 * MIB
    max_projects: int = 32
    max_credentials: int = 32
    max_reports: int = 100
    max_backups: int = 3
    max_backup_bytes: int = 32 * MIB
    max_records: int = 256
    max_record_bytes: int = 16 * 1024
    max_requests: int = 4096
    max_audit_events: int = 10_000
    max_audit_bytes: int = 2 * MIB
    max_sqlite_bytes: int = 512 * MIB
    busy_timeout_seconds: float = 2.0

    def __post_init__(self):
        caps = {"max_upload_bytes": 16 * MIB, "max_project_bytes": 128 * MIB,
                "max_global_bytes": 384 * MIB, "max_projects": 128, "max_credentials": 128,
                "max_reports": 1000, "max_backups": 8, "max_backup_bytes": 64 * MIB,
                "max_records": 1024, "max_record_bytes": 16 * 1024, "max_requests": 16384,
                "max_audit_events": 10_000, "max_audit_bytes": 2 * MIB, "max_sqlite_bytes": 512 * MIB}
        for name, maximum in caps.items():
            integer(getattr(self, name), name, 1, maximum)
        if (self.max_upload_bytes > self.max_project_bytes or self.max_project_bytes > self.max_global_bytes
                or self.max_global_bytes > self.max_sqlite_bytes or self.max_backup_bytes > self.max_project_bytes
                or self.max_sqlite_bytes < 128 * 1024 or self.max_sqlite_bytes % 4096):
            raise TeamError("invalid", "storage limits must fit within project/global/database caps and whole 4096-byte pages")
        if self.max_audit_bytes < 2048:
            raise TeamError("invalid", "audit byte limit must retain at least one complete event")
        duration(self.busy_timeout_seconds, "busy_timeout_seconds", 0.01, 5.0)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProjectPolicy:
    privacy_profile: str = "share"
    retention_seconds: float = 7 * 86400
    backup_ttl_seconds: float = 86400
    audit_retention_seconds: float = 90 * 86400
    max_bytes: int | None = None
    max_reports: int | None = None
    max_backups: int | None = None

    def __post_init__(self):
        if self.privacy_profile not in ("share", "minimal"):
            raise TeamError("invalid", "privacy_profile must be share or minimal")
        duration(self.retention_seconds, "retention_seconds", 60, 30 * 86400)
        duration(self.backup_ttl_seconds, "backup_ttl_seconds", 1, 86400)
        duration(self.audit_retention_seconds, "audit_retention_seconds", 86400, 90 * 86400)
        for name, maximum in (("max_bytes", 128 * MIB), ("max_reports", 1000), ("max_backups", 8)):
            if getattr(self, name) is not None:
                integer(getattr(self, name), name, 1, maximum)

    def resolved(self, limits: StoreLimits) -> ProjectPolicy:
        result = replace(self, max_bytes=self.max_bytes if self.max_bytes is not None else limits.max_project_bytes,
                         max_reports=self.max_reports if self.max_reports is not None else limits.max_reports,
                         max_backups=self.max_backups if self.max_backups is not None else limits.max_backups)
        if (result.max_bytes > limits.max_project_bytes or result.max_reports > limits.max_reports
                or result.max_backups > limits.max_backups):
            raise TeamError("invalid", "project policy cannot exceed the store administrator's limits")
        return result

    def to_dict(self) -> dict:
        return asdict(self)


class Actor:
    """Opaque bearer handle; all authority must be revalidated by each store operation."""

    __slots__ = ("_credential_id", "_project_id", "_token")

    def __init__(self, project_id: str, credential_id: str, token: str, *, _proof=None):
        if _proof is not _ACTOR_PROOF:
            raise TypeError("Actors are returned only by TeamStore.authenticate")
        object.__setattr__(self, "_project_id", project_id)
        object.__setattr__(self, "_credential_id", credential_id)
        object.__setattr__(self, "_token", token)

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def credential_id(self) -> str:
        return self._credential_id

    def __setattr__(self, _name, _value):
        raise AttributeError("Actor handles are immutable")

    def __repr__(self) -> str:
        return f"Actor(project_id={self.project_id!r}, credential_id={self.credential_id!r})"

    def __reduce_ex__(self, _protocol):
        raise TypeError("Actor handles cannot be serialized")


def _make_actor(project_id: str, credential_id: str, token: str) -> Actor:
    return Actor(project_id, credential_id, token, _proof=_ACTOR_PROOF)


__all__ = ["Actor", "ProjectPolicy", "StoreLimits", "TeamError"]
