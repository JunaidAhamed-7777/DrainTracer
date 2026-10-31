"""Explicit sharing calls and privacy transformation, independent of collection."""

from __future__ import annotations

import base64
import json

from .privacy import redact_report
from .report_contract import parse_report
from .team_models import TeamError
from .team_security import read_credential_file
from .team_transport import Endpoint, SharingError, request

MAX_UPLOAD = 8 * 1024 * 1024
ERROR_CODES = {"unauthorized", "forbidden", "not_found", "invalid", "quota", "busy", "storage", "conflict",
               "unavailable", "certificate"}


def prepare_upload(raw: bytes, *, profile: str = "share") -> bytes:
    if not isinstance(raw, bytes) or len(raw) > MAX_UPLOAD or profile not in {"share", "minimal"}:
        raise SharingError("invalid", "Choose a share/minimal report of at most 8 MiB")
    safe = redact_report(parse_report(raw), profile)
    encoded = json.dumps(safe, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_UPLOAD:
        raise SharingError("quota")
    return encoded


class TeamClient:
    def __init__(self, endpoint: Endpoint, credential_file, *, cafile=None, timeout=10):
        self.endpoint, self.cafile, self.timeout = endpoint, cafile, timeout
        self.credential_file = credential_file

    @property
    def project_id(self):
        return read_credential_file(self.credential_file)["project_id"]

    def call(self, operation: str, params: dict | None = None, *, timeout=None):
        credentials = read_credential_file(self.credential_file)
        reply = request(self.endpoint, {"team_schema": 1, "project_id": credentials["project_id"],
                                       "token": credentials["token"], "operation": operation,
                                       "params": {} if params is None else params},
                        cafile=self.cafile, timeout=self.timeout if timeout is None else timeout)
        if (set(reply) - {"team_schema", "ok", "result", "error"} or type(reply.get("team_schema")) is not int
                or reply["team_schema"] != 1 or type(reply.get("ok")) is not bool):
            raise SharingError("invalid", "Server returned an unsupported response")
        if not reply["ok"]:
            error = reply.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise TeamError(code if isinstance(code, str) and code in ERROR_CODES else "storage",
                            "Sharing request was refused")
        if set(reply) != {"team_schema", "ok", "result"} or not isinstance(reply["result"], dict):
            raise SharingError("invalid", "Server returned an unsupported result")
        return reply["result"]

    def ingest_prepared(self, raw: bytes, request_id: str, *, timeout=None):
        if not isinstance(raw, bytes) or len(raw) > MAX_UPLOAD:
            raise SharingError("quota")
        return self.call("ingest", {"report": base64.b64encode(raw).decode("ascii"), "request_id": request_id},
                         timeout=timeout)
