"""Explicit local team administration. No listener, HTML rendering or token arguments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .team_models import ProjectPolicy, StoreLimits, TeamError
from .team_security import read_credential_file, secure_directory, write_credential_file
from .team_store import TeamStore


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise TeamError("invalid", "Command arguments are invalid; use --help")


def _parser():
    parser = _Parser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path, help="Existing private store, or new leaf for init")
    parser.add_argument("--credentials", type=Path, help="Protected credential JSON; no token command-line option")
    commands = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    commands.add_parser("init").add_argument("--limits", type=Path, help="Optional bounded StoreLimits JSON")
    commands.add_parser("projects")
    commands.add_parser("purge-local")
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("name")
    bootstrap.add_argument("--policy", type=Path)
    bootstrap.add_argument("--credential-output", type=Path, required=True)
    recover = commands.add_parser("admin-recover")
    recover.add_argument("project_id")
    recover.add_argument("--credential-output", type=Path, required=True)
    for name in ("whoami", "policy", "credentials", "reports", "backups", "purge", "backup", "project-delete"):
        command = commands.add_parser(name)
        if name == "reports":
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--cursor")
    policy = commands.add_parser("policy-set")
    policy.add_argument("path", type=Path)
    issue = commands.add_parser("credential-issue")
    issue.add_argument("role", choices=("viewer", "operator", "admin"))
    issue.add_argument("--ttl-seconds", type=float, default=86400)
    issue.add_argument("--credential-output", type=Path, required=True)
    commands.add_parser("credential-revoke").add_argument("credential_id")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--request-id", required=True, help="Stable lowercase UUID hex reused for an uncertain retry")
    for name in ("report", "report-delete"):
        commands.add_parser(name).add_argument("report_id")
    export = commands.add_parser("export")
    export.add_argument("report_id")
    export.add_argument("--output", type=Path, required=True, help="New JSON file; never overwritten")
    for name in ("backup-delete", "restore"):
        commands.add_parser(name).add_argument("backup_id")
    audit = commands.add_parser("audit")
    audit.add_argument("--after", type=int, default=0)
    audit.add_argument("--limit", type=int, default=100)
    create = commands.add_parser("record-create")
    create.add_argument("kind", choices=("baseline_approval", "comparison_receipt"))
    create.add_argument("path", type=Path)
    create.add_argument("--key")
    create.add_argument("--report-id", action="append", default=[])
    create.add_argument("--ttl-seconds", type=float)
    create.add_argument("--request-id", required=True)
    records = commands.add_parser("records")
    records.add_argument("--kind", choices=("baseline_approval", "comparison_receipt"))
    records.add_argument("--key")
    records.add_argument("--limit", type=int, default=100)
    records.add_argument("--cursor")
    for name in ("record", "record-delete"):
        commands.add_parser(name).add_argument("record_id")
    return parser


def _read(path, maximum):
    if not path.is_file():
        raise TeamError("invalid", "Input must be an ordinary file")
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise TeamError("invalid", "Input file exceeds its configured byte limit")
    return raw


def _object(path, maximum=16384):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result
    try:
        value = json.loads(_read(path, maximum), object_pairs_hook=unique,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise TeamError("invalid", "Input file is not bounded strict JSON") from None
    if type(value) is not dict:
        raise TeamError("invalid", "Input file must contain an object")
    return value


def _credential_destination(path):
    path = path.absolute()
    secure_directory(path.parent)
    if path.exists() or path.is_symlink():
        raise TeamError("conflict", "Credential destination already exists and cannot be replaced")
    return path


def _save_issued(store, issued, path, *, revoker=None):
    try:
        write_credential_file(path, {"team_credentials_schema": 1, "project_id": issued["project_id"], "token": issued["token"]})
    except (OSError, ValueError):
        # A token not delivered durably must not remain usable on a partial file.
        try:
            actor = revoker or store.authenticate(issued["token"], issued["project_id"])
            store.revoke_credential(actor, issued["credential"]["id"])
        except (OSError, ValueError):
            raise TeamError("storage", "Credential delivery failed; inspect the destination and audit, then revoke the new key locally") from None
        raise TeamError("storage", "Credential delivery failed; the new key was revoked; inspect the destination") from None
    return {"team_schema": 1, "project_id": issued["project_id"], "credential": issued["credential"], "credential_saved": True}


def _dispatch(store, actor, args):
    action = args.action
    simple = {"whoami": store.whoami, "policy": store.get_policy, "credentials": store.list_credentials,
              "backups": store.list_backups, "purge": store.purge, "backup": store.backup,
              "project-delete": store.delete_project}
    if action in simple:
        return simple[action](actor)
    if action == "policy-set":
        return store.update_policy(actor, _object(args.path))
    if action == "credential-issue":
        destination = _credential_destination(args.credential_output)
        return _save_issued(store, store.issue_credential(actor, args.role, ttl_seconds=args.ttl_seconds), destination, revoker=actor)
    if action == "credential-revoke":
        return store.revoke_credential(actor, args.credential_id)
    if action == "ingest":
        return store.ingest(actor, _read(args.path, store.limits.max_upload_bytes), request_id=args.request_id)
    if action == "reports":
        return store.list_reports(actor, limit=args.limit, cursor=args.cursor)
    if action == "report":
        return store.read_report(actor, args.report_id)
    if action == "export":
        raw = store.export_report(actor, args.report_id)
        destination = args.output.absolute()
        secure_directory(destination.parent)
        # Export is sanitized but still diagnostic evidence: use the same private,
        # exclusive file provisioning without printing content or filesystem paths.
        import os

        from .team_security import _new_credential_file
        descriptor = _new_credential_file(destination)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return {"team_schema": 1, "project_id": actor.project_id, "report_id": args.report_id, "exported_bytes": len(raw)}
    if action == "report-delete":
        return store.delete_report(actor, args.report_id)
    if action == "backup-delete":
        return store.delete_backup(actor, args.backup_id)
    if action == "restore":
        return store.restore(actor, args.backup_id)
    if action == "audit":
        return store.audit(actor, after=args.after, limit=args.limit)
    if action == "record-create":
        return store.create_record(actor, args.kind, _object(args.path), key=args.key, report_ids=args.report_id,
                                   ttl_seconds=args.ttl_seconds, request_id=args.request_id)
    if action == "records":
        return store.list_records(actor, kind=args.kind, key=args.key, limit=args.limit, cursor=args.cursor)
    if action == "record":
        return store.read_record(actor, args.record_id)
    if action == "record-delete":
        return store.delete_record(actor, args.record_id)
    raise TeamError("invalid", "Command is not supported")


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        if args.action == "init":
            try:
                limits = StoreLimits(**_object(args.limits)) if args.limits else StoreLimits()
            except TypeError:
                raise TeamError("invalid", "Unknown store limit field") from None
            with TeamStore.create(args.store, limits=limits) as store:
                result = {"team_schema": 1, "store_id": store.store_id, "created": True}
        else:
            with TeamStore.open(args.store) as store:
                if args.action == "projects":
                    result = store.list_projects_local()
                elif args.action == "purge-local":
                    result = store.purge_local()
                elif args.action in ("bootstrap", "admin-recover"):
                    destination = _credential_destination(args.credential_output)
                    if args.action == "bootstrap":
                        try:
                            policy = ProjectPolicy(**_object(args.policy)) if args.policy else ProjectPolicy()
                        except TypeError:
                            raise TeamError("invalid", "Unknown policy field") from None
                        issued = store.bootstrap_project(args.name, policy=policy)
                    else:
                        issued = store.recover_admin(args.project_id)
                    result = _save_issued(store, issued, destination)
                else:
                    if not args.credentials:
                        raise TeamError("unauthorized", "A protected credential file is required")
                    credential = read_credential_file(args.credentials)
                    actor = store.authenticate(credential["token"], credential["project_id"])
                    result = _dispatch(store, actor, args)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
        return 0
    except TeamError as error:
        print(json.dumps({"team_schema": 1, "error": {"code": error.code, "message": str(error)}}, ensure_ascii=True), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        print('{"team_schema":1,"error":{"code":"storage","message":"Local operation failed; no input content or credentials were printed"}}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
