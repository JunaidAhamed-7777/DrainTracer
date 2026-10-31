"""Explicit sharing commands; offline preparation never opens a connection."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import _version
from .team_client import ERROR_CODES, MAX_UPLOAD, TeamClient, prepare_upload
from .team_models import TeamError
from .team_queue import UploadQueue, history_key
from .team_receipts import comparison_text, prepare_approval, prepare_receipt
from .team_security import (
    _new_credential_file,
    secure_directory,
    verify_data_file,
    write_credential_file,
)
from .team_transport import Endpoint, SharingError, decode_message


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise SharingError("invalid")


def _read(path, maximum, *, private=False):
    path = Path(path).absolute()
    if private:
        verify_data_file(path, writable=False)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise SharingError("invalid")
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise SharingError("quota")
    return raw


def _object(path, maximum=16384, *, private=False):
    return decode_message(_read(path, maximum, private=private))


def _label(value):
    return (type(value) is str and 0 < len(value) <= 256 and value == value.strip()
            and not any(unicodedata.category(character).startswith("C") for character in value))


def operating_evidence(path, *, now=None):
    """Require current private local evidence, without claiming human consent or attestation."""
    value = _object(path, 1024 * 1024, private=True)
    gates = value.get("readiness_gates")
    required = {"named_operator", "support_contact", "runbook_identified", "successful_restore_exercise",
                "team_exercise_completed", "required_observations_complete", "all_checks_and_budgets_passed"}
    try:
        finished = datetime.fromisoformat(value["finished_at"].replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if (type(value.get("operations_schema")) is not int or value["operations_schema"] != 1
                or value.get("version") != _version() or value.get("operating_prerequisites_met") is not True
                or not isinstance(gates, dict) or not required <= gates.keys() or not all(item is True for item in gates.values())
                or not _label(value.get("operator")) or not _label(value.get("support_contact"))
                or not isinstance(value.get("components"), list) or "team" not in value["components"]
                or finished.utcoffset() != timedelta(0) or not current - timedelta(days=7) <= finished <= current):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SharingError("invalid", "Current private passing operating evidence is required") from None
    return value


def _destination(path):
    destination = Path(path).absolute()
    secure_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise SharingError("conflict")
    return destination


def _write(path, raw):
    destination = _destination(path)
    descriptor = _new_credential_file(destination)
    with os.fdopen(descriptor, "wb") as handle:
        verify_data_file(destination)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return {"share_schema": 1, "exported_bytes": len(raw)}


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


_TOKEN = re.compile(r"dt1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{64}")


def _public(value):
    """Never display credential-bearing fields or bearer-shaped strings from a peer."""
    if isinstance(value, dict):
        return {_TOKEN.sub("[credential]", key): _public(item) for key, item in value.items()
                if key.casefold() not in {"token", "secret", "password", "authorization"}}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return _TOKEN.sub("[credential]", value) if isinstance(value, str) else value


def _client(args):
    if not args.endpoint or not args.credentials:
        raise SharingError("invalid")
    endpoint = Endpoint.parse(args.endpoint, args.connect_ip)
    return TeamClient(endpoint, args.credentials, cafile=args.ca, timeout=args.timeout)


def build_parser():
    parser = _Parser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"DrainTracer {_version()}")
    parser.add_argument("--endpoint", help="Explicit tls://host:port; no endpoint is discovered automatically")
    parser.add_argument("--connect-ip", help="Explicit numeric destination for a DNS hostname")
    parser.add_argument("--credentials", type=Path, help="Protected local credential file; never pass a token argument")
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--timeout", type=float, default=10)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    serve = commands.add_parser("serve")
    serve.add_argument("--store", type=Path, required=True)
    serve.add_argument("--certificate", type=Path, required=True)
    serve.add_argument("--key", type=Path, required=True)
    serve.add_argument("--operating-evidence", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8443)
    serve.add_argument("--allow-network", action="store_true")
    preview = commands.add_parser("preview")
    preview.add_argument("path", type=Path)
    preview.add_argument("--profile", choices=("share", "minimal"), default="share")
    preview.add_argument("--output", type=Path)
    for command in ("enqueue-report", "enqueue-comparison", "queue", "flush"):
        item = commands.add_parser(command)
        item.add_argument("--queue", required=True, type=Path)
        if command in ("enqueue-report", "enqueue-comparison"):
            item.add_argument("path", type=Path)
            item.add_argument("--ttl-seconds", type=float, default=86400)
        if command == "enqueue-report":
            item.add_argument("--profile", choices=("share", "minimal"), default="share")
        if command == "enqueue-comparison":
            item.add_argument("--report-id", action="append", default=[])
        if command == "flush":
            item.add_argument("--limit", type=int, default=8)
            item.add_argument("--deadline-seconds", type=float, default=60)
            item.add_argument("--retry-blocked", action="store_true")
    history = commands.add_parser("history")
    history.add_argument("--repository")
    history.add_argument("--source-commit")
    history.add_argument("--workload-id")
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--cursor")
    comparison = commands.add_parser("comparison")
    comparison.add_argument("record_id")
    comparison.add_argument("--text", action="store_true", help="Literal plain text; no ANSI or HTML rendering")
    export = commands.add_parser("export")
    export.add_argument("report_id")
    export.add_argument("--output", required=True, type=Path)
    approval = commands.add_parser("approve-baseline")
    approval.add_argument("store", type=Path)
    approval.add_argument("--repository", required=True)
    approval.add_argument("--note", default="")
    approval.add_argument("--request-id", required=True)
    approval.add_argument("--ttl-seconds", type=float, default=86400)
    prepared = commands.add_parser("prepare-comparison")
    prepared.add_argument("store", type=Path)
    prepared.add_argument("--run", type=Path, action="append", required=True)
    prepared.add_argument("--source-commit", required=True)
    prepared.add_argument("--repository", required=True)
    prepared.add_argument("--approval-id", required=True)
    prepared.add_argument("--output", type=Path, required=True)
    for command in ("whoami", "policy", "audit", "backups", "backup", "credentials", "purge"):
        commands.add_parser(command)
    for command, identifier in (("revoke", "credential_id"), ("delete-report", "report_id"),
                                ("delete-record", "record_id"), ("restore", "backup_id"), ("delete-backup", "backup_id")):
        commands.add_parser(command).add_argument(identifier)
    issue = commands.add_parser("issue-credential")
    issue.add_argument("role", choices=("viewer", "operator", "admin"))
    issue.add_argument("--ttl-seconds", type=float, default=86400)
    issue.add_argument("--output", required=True, type=Path)
    return parser


def _serve(args):
    from .team_server import make_server
    from .team_store import TeamStore

    operating_evidence(args.operating_evidence)
    address = ipaddress.ip_address(args.host)
    if not address.is_loopback and not args.allow_network:
        raise SharingError("forbidden")
    if not 1 <= args.port <= 65535:
        raise SharingError("invalid")
    with TeamStore.open(args.store) as store:
        server = make_server(store, args.certificate, args.key, host=str(address), port=args.port, timeout=args.timeout)
        try:
            print(_json({"share_schema": 1, "listening": True, "scope": "operator-controlled service"}).decode("ascii"), flush=True)
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()
    return {"share_schema": 1, "stopped": True}, 0


def _offline(args):
    if args.command == "preview":
        raw = prepare_upload(_read(args.path, MAX_UPLOAD), profile=args.profile)
        return (_write(args.output, raw) if args.output else decode_message(raw)), 0
    if args.command == "prepare-comparison":
        from .baseline_control import BaselineStore, load_runs
        destination = _destination(args.output)
        with BaselineStore(args.store) as store:
            code, result = store.check(load_runs(args.run), source_commit=args.source_commit)
            receipt = prepare_receipt(result, repository=args.repository, workload_id=store.state["workload_id"],
                                      approval_id=args.approval_id)
        return {**_write(destination, _json(receipt)), "check_exit_code": code}, code
    return None


def _remote(args):
    client = _client(args)
    if args.command in {"enqueue-report", "enqueue-comparison", "queue", "flush"}:
        queue = UploadQueue(args.queue, client.project_id, client.endpoint,
                            create=args.command in {"enqueue-report", "enqueue-comparison"})
        if args.command == "enqueue-report":
            return queue.enqueue(_read(args.path, MAX_UPLOAD), profile=args.profile, ttl_seconds=args.ttl_seconds), 0
        if args.command == "enqueue-comparison":
            return queue.enqueue_receipt(_object(args.path), report_ids=args.report_id, ttl_seconds=args.ttl_seconds), 0
        if args.command == "queue":
            result = queue.inspect()
            return result, 2 if result["entries"] else 0
        result = queue.flush(client, limit=args.limit, deadline_seconds=args.deadline_seconds, retry_blocked=args.retry_blocked)
        return result, 2 if result["pending"] else 0
    if args.command == "history":
        bindings = (args.repository, args.source_commit, args.workload_id)
        if any(bindings) and not all(bindings):
            raise SharingError("invalid")
        return (client.call("list_records", {"kind": "comparison_receipt", "key": history_key(*bindings),
                                             "limit": args.limit, "cursor": args.cursor}) if all(bindings)
                else client.call("list_reports", {"limit": args.limit, "cursor": args.cursor})), 0
    if args.command == "comparison":
        record = client.call("read_record", {"record_id": args.record_id})
        text = comparison_text(record)
        return text if args.text else record, 0
    if args.command == "export":
        import base64

        from .report_contract import parse_report
        result = client.call("export_report", {"report_id": args.report_id})
        raw = base64.b64decode(result["report"], validate=True)
        parse_report(raw)
        return _write(args.output, raw), 0
    if args.command == "approve-baseline":
        from .baseline_control import BaselineStore
        with BaselineStore(args.store) as store:
            value = prepare_approval(store, args.repository, note=args.note)
        return client.call("create_record", {"kind": "baseline_approval", "value": value,
                                              "ttl_seconds": args.ttl_seconds, "request_id": args.request_id}), 0
    if args.command == "issue-credential":
        destination = _destination(args.output)
        issued = client.call("issue_credential", {"role": args.role, "ttl_seconds": args.ttl_seconds})
        try:
            write_credential_file(destination, {"team_credentials_schema": 1, "project_id": issued["project_id"],
                                                "token": issued["token"]})
        except Exception:  # noqa: BLE001 - attempt revocation after any uncertain private delivery
            try:
                client.call("revoke_credential", {"credential_id": issued["credential"]["id"]})
            except Exception:  # noqa: BLE001 - outer fixed code requires local audit/administration, never echoes token
                raise SharingError("storage") from None
            raise SharingError("storage") from None
        return {"share_schema": 1, "project_id": issued["project_id"], "credential": issued["credential"],
                "credential_saved": True}, 0
    simple = {"whoami": "whoami", "policy": "get_policy", "audit": "audit", "backups": "list_backups",
              "backup": "backup", "credentials": "list_credentials", "purge": "purge"}
    if args.command in simple:
        return client.call(simple[args.command]), 0
    operations = {"revoke": ("revoke_credential", "credential_id"), "delete-report": ("delete_report", "report_id"),
                  "delete-record": ("delete_record", "record_id"), "restore": ("restore", "backup_id"),
                  "delete-backup": ("delete_backup", "backup_id")}
    operation, identifier = operations[args.command]
    return client.call(operation, {identifier: getattr(args, identifier)}), 0


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if args.command == "serve":
            result, code = _serve(args)
        else:
            offline = _offline(args)
            result, code = offline if offline is not None else _remote(args)
        result = _public(result)
        if type(result) is str:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(result.encode(encoding, errors="backslashreplace").decode(encoding))
        else:
            print(_json(result).decode("ascii"))
        return code
    except KeyboardInterrupt:
        return 130
    except (SharingError, TeamError) as error:
        print(_json({"share_schema": 1, "error": {"code": error.code if type(error.code) is str and error.code in ERROR_CODES else "invalid"}}).decode("ascii"),
              file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - CLI boundary must never expose source values or private exception text
        print('{"share_schema":1,"error":{"code":"operation_failed"}}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
