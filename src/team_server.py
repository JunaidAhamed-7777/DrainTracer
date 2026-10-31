"""Project-only RPC adapter; local bootstrap and process execution are never remote."""

from __future__ import annotations

import base64
import binascii
import re
import ssl

from .team_client import ERROR_CODES, MAX_UPLOAD
from .team_models import TeamError
from .team_security import verify_data_file
from .team_transport import ALPN, SharingError, SharingServer

_UUID = re.compile(r"[0-9a-f]{32}\Z")


def _params(params, allowed=(), required=()):
    if not isinstance(params, dict) or set(params) - set(allowed) or set(required) - set(params):
        raise SharingError("invalid")
    return params


class TeamDispatcher:
    def __init__(self, store):
        self.store = store

    def __call__(self, message):
        try:
            if (set(message) != {"team_schema", "project_id", "token", "operation", "params"}
                    or type(message["team_schema"]) is not int or message["team_schema"] != 1
                    or not isinstance(message["project_id"], str) or not _UUID.fullmatch(message["project_id"])
                    or not isinstance(message["token"], str) or not 1 <= len(message["token"]) <= 512
                    or any(ord(character) < 33 for character in message["token"])
                    or not isinstance(message["operation"], str)):
                raise SharingError("invalid")
            operation, params = message["operation"], message["params"]
            # This returns an opaque handle only. Every called operation below
            # rechecks revocation, expiry, project and role in its transaction.
            actor = self.store.authenticate(message["token"], message["project_id"])
            result = self._dispatch(actor, operation, params)
            return {"team_schema": 1, "ok": True, "result": result}
        except (SharingError, TeamError) as error:
            return {"team_schema": 1, "ok": False,
                    "error": {"code": error.code if error.code in ERROR_CODES else "storage"}}

    def _dispatch(self, actor, operation, params):
        store = self.store
        if operation == "whoami":
            _params(params)
            return store.whoami(actor)
        if operation == "ingest":
            _params(params, ("report", "request_id"), ("report", "request_id"))
            value = params["report"]
            if (not isinstance(value, str) or len(value) > (MAX_UPLOAD + 2) // 3 * 4
                    or not isinstance(params["request_id"], str) or not _UUID.fullmatch(params["request_id"])):
                raise SharingError("invalid")
            try:
                raw = base64.b64decode(value, validate=True)
            except (ValueError, binascii.Error) as error:
                raise SharingError("invalid") from error
            if len(raw) > MAX_UPLOAD:
                raise SharingError("quota")
            return store.ingest(actor, raw, request_id=params["request_id"])
        if operation == "list_reports":
            _params(params, ("limit", "cursor"))
            return store.list_reports(actor, **params)
        if operation == "export_report":
            _params(params, ("report_id",), ("report_id",))
            raw = store.export_report(actor, params["report_id"])
            return {"team_schema": 1, "report_id": params["report_id"], "report": base64.b64encode(raw).decode("ascii")}
        if operation == "delete_report":
            _params(params, ("report_id",), ("report_id",))
            return store.delete_report(actor, params["report_id"])
        if operation == "get_policy":
            _params(params)
            return store.get_policy(actor)
        if operation == "update_policy":
            _params(params, ("policy",), ("policy",))
            return store.update_policy(actor, params["policy"])
        if operation == "issue_credential":
            _params(params, ("role", "ttl_seconds"), ("role",))
            return store.issue_credential(actor, **params)
        if operation == "list_credentials":
            _params(params)
            return store.list_credentials(actor)
        if operation == "revoke_credential":
            _params(params, ("credential_id",), ("credential_id",))
            return store.revoke_credential(actor, params["credential_id"])
        if operation == "audit":
            _params(params, ("after", "limit"))
            return store.audit(actor, **params)
        if operation == "backup":
            _params(params)
            return store.backup(actor)
        if operation == "list_backups":
            _params(params)
            return store.list_backups(actor)
        if operation in {"delete_backup", "restore"}:
            _params(params, ("backup_id",), ("backup_id",))
            return (store.delete_backup(actor, params["backup_id"]) if operation == "delete_backup"
                    else store.restore(actor, params["backup_id"]))
        if operation == "purge":
            _params(params)
            return store.purge(actor)
        if operation == "create_record":
            _params(params, ("kind", "value", "key", "report_ids", "ttl_seconds"), ("kind", "value"))
            return store.create_record(actor, **params)
        if operation == "read_record":
            _params(params, ("record_id",), ("record_id",))
            return store.read_record(actor, params["record_id"])
        if operation == "list_records":
            _params(params, ("kind", "key", "limit", "cursor"))
            return store.list_records(actor, **params)
        if operation == "delete_record":
            _params(params, ("record_id",), ("record_id",))
            return store.delete_record(actor, params["record_id"])
        raise SharingError("invalid", "Unsupported project operation")


def make_server(store, certificate_file, key_file, *, host="127.0.0.1", port=8443, timeout=10):
    certificate = verify_data_file(certificate_file, writable=False)
    key = verify_data_file(key_file, writable=False)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols([ALPN])
    context.keylog_filename = None
    context.load_cert_chain(certificate, key)
    return SharingServer((host, port), context, TeamDispatcher(store), timeout=timeout)
