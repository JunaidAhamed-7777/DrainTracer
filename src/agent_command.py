"""Explicit foreground Windows agent and its minimal authenticated JSON client."""

from __future__ import annotations

import argparse
import json
import math
import threading
import uuid
from pathlib import Path

from .agent_service import AgentError, AgentPolicy, AgentRegistry
from .agent_state import AgentJournal, AgentStateError, acquire_control_fence
from .agent_transport import (
    AgentTransportError,
    PipeListener,
    authenticate,
    connect,
    load_secret,
    pipe_name,
    private_directory,
)
from .api import _version


class AgentServer:
    def __init__(self, state_directory, *, profile="default", policy=None,
                 retention_seconds=7 * 86400, max_storage_bytes=512 * 1024 * 1024):
        self.directory = private_directory(state_directory)
        self._fence = acquire_control_fence(self.directory)
        try:
            if (self.directory / "agent-removal.json").exists():
                raise AgentStateError("an interrupted removal must be explicitly completed before startup")
            self.secret = load_secret(self.directory, create=True)
            self.reports = private_directory(self.directory / "reports")
            self.checkpoints = private_directory(self.directory / "checkpoints")
            from .agent_transport import current_user_sid
            self.journal = AgentJournal(self.directory, app_version=_version(), owner=current_user_sid(),
                                        retention_seconds=retention_seconds, max_storage_bytes=max_storage_bytes)
            try:
                self.registry = AgentRegistry(self.reports, policy or AgentPolicy(), journal=self.journal,
                                              checkpoints=self.checkpoints)
                self.listener = PipeListener(pipe_name(profile))
            except BaseException:
                self.journal.close()
                raise
        except BaseException:
            self._fence.close()
            raise
        self.rejected_connections = 0

    def serve(self, stop: threading.Event):
        clean = False
        try:
            while not stop.is_set():
                self.registry.refresh()
                if not self.listener.accept():
                    stop.wait(0.02)
                    continue
                try:
                    authenticate(self.listener, self.secret, server=True)
                    request = self.listener.receive()
                    self.listener.send(self.registry.handle(request))
                    # DisconnectNamedPipe discards unread bytes. Wait for a
                    # bounded receipt acknowledgement before reusing the pipe.
                    if self.listener.receive() != {"ack": request.get("id")}:
                        raise AgentTransportError("the local agent reply was not acknowledged")
                except (AgentTransportError, OSError, ValueError):
                    # No session data or credentials in authentication diagnostics.
                    self.rejected_connections += 1
                finally:
                    self.listener.disconnect()
                    if self.registry.stopping:
                        stop.set()
        finally:
            try:
                clean = self.registry.close(timeout=15)
            finally:
                self.listener.close()
                self.journal.close()
                self._fence.close()
        return clean


class AgentClient:
    def __init__(self, state_directory, *, profile="default", timeout=15.0):
        self.directory = Path(state_directory).absolute()
        self.secret = load_secret(self.directory)
        self.name = pipe_name(profile)
        self.timeout = timeout

    def request(self, operation, params=None, *, request_id=None):
        identity = request_id or str(uuid.uuid4())
        pipe = connect(self.name, timeout=self.timeout)
        try:
            authenticate(pipe, self.secret, server=False)
            pipe.send({"protocol": 1, "id": identity, "operation": operation, "params": params or {}})
            response = pipe.receive()
            pipe.send({"ack": identity})
        finally:
            pipe.close()
        if response.get("protocol") != 1 or response.get("id") != identity or type(response.get("ok")) is not bool:
            raise AgentTransportError("the agent returned an invalid response identity")
        if not response["ok"]:
            error = response.get("error", {})
            exception = AgentError(error.get("code", "operation_failed"), error.get("message", "agent request failed"))
            exception.may_have_run = bool(error.get("may_have_run"))
            raise exception
        return response["result"]


def build_parser():
    parser = argparse.ArgumentParser(description="Opt-in foreground Windows monitoring agent. Output is JSON.")
    parser.add_argument("--state-dir", default=str(Path.home() / ".draintracer-agent"))
    parser.add_argument("--profile", default="default", help="Local endpoint profile, not a monitor configuration profile.")
    parser.add_argument("--request-timeout", type=float, default=15.0,
                        help="Client request timeout in seconds (0 < value <= 60; default: 15).")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Start explicitly in the foreground; stop with Ctrl+C.")
    server.add_argument("--allow-launch", action="store_true")
    server.add_argument("--launch-root", action="append", default=[])
    server.add_argument("--allow-pause", action="store_true", help="Authorize pause/resume for launched targets only.")
    server.add_argument("--allow-kill", action="store_true", help="Authorize explicit target kill separately from cancellation.")
    server.add_argument("--max-sessions", type=int, default=4)
    server.add_argument("--max-records", type=int, default=64)
    server.add_argument("--max-timeout", type=float, default=3600)
    server.add_argument("--retention-days", type=float, default=7)
    server.add_argument("--storage-limit-mib", type=int, default=512)
    start = commands.add_parser("start")
    target = start.add_mutually_exclusive_group(required=True)
    target.add_argument("--pid", type=int)
    target.add_argument("--target")
    start.add_argument("--create-time", type=float, help="Required with --pid; read psutil.Process(pid).create_time().")
    start.add_argument("--timeout", type=float, default=60)
    start.add_argument("--sample-interval", type=int, default=500, metavar="MILLISECONDS")
    start.add_argument("--windows", type=float, nargs="+", default=[10, 30, 60], metavar="SECONDS")
    start.add_argument("--warmup", type=float, default=0, metavar="SECONDS")
    start.add_argument("target_args", nargs=argparse.REMAINDER, help="Literal target arguments after --.")
    status = commands.add_parser("status")
    status.add_argument("session_id", nargs="?")
    status.add_argument("--offset", type=int)
    status.add_argument("--limit", type=int)
    events = commands.add_parser("events")
    events.add_argument("session_id")
    events.add_argument("--cursor", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    commands.add_parser("diagnose", help="Read live readiness/storage or inspect offline state without controlling targets.")
    commands.add_parser("stop", help="Request ordinary authenticated shutdown; inspect diagnostics for completed cleanup.")
    recover = commands.add_parser("recover", help="Preview a matching checkpoint without starting the agent.")
    recover.add_argument("session_id")
    recover.add_argument("--out-dir")
    remove = commands.add_parser("remove", help="Preview known control-file removal; preserve evidence and unrelated files.")
    remove.add_argument("--apply", action="store_true")
    support = commands.add_parser("support", help="Preview redacted support data; no upload.")
    support.add_argument("--privacy-profile", choices=("minimal", "share"), default="minimal")
    support.add_argument("--include-report", action="append", default=[], metavar="SESSION_UUID")
    support.add_argument("--out", help="Create a new local ZIP archive exclusively.")
    for operation in ("cancel", "export", "pause", "resume", "kill"):
        commands.add_parser(operation).add_argument("session_id")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if not math.isfinite(args.request_timeout) or not 0 < args.request_timeout <= 60:
            raise AgentError("invalid_input", "request timeout must be finite, greater than zero, and at most 60 seconds")
        if args.command == "serve":
            policy = AgentPolicy(allow_launch=args.allow_launch, allow_pause=args.allow_pause,
                                 allow_kill=args.allow_kill, launch_roots=tuple(map(Path, args.launch_root)),
                                 max_sessions=args.max_sessions, max_records=args.max_records,
                                 max_timeout=args.max_timeout)
            server = AgentServer(args.state_dir, profile=args.profile, policy=policy,
                                 retention_seconds=args.retention_days * 86400,
                                 max_storage_bytes=args.storage_limit_mib * 1024 * 1024)
            stop = threading.Event()
            print(json.dumps({"agent": "ready", "protocol": 1, "profile": args.profile}), flush=True)
            try:
                clean = server.serve(stop)
            except KeyboardInterrupt:
                # serve's finally already performs ownership-specific cleanup.
                clean = all(record.result is not None and record.result["cleanup"] in {"complete", "not_attempted"}
                            for record in server.registry.records.values())
            print(json.dumps({"agent": "stopped", "cleanup_complete": clean}), flush=True)
            return 0 if clean else 2
        if args.command in {"diagnose", "recover", "remove", "support"}:
            from .agent_diagnostics import (
                inspect_agent,
                recover_session,
                remove_control_files,
                support_document,
                support_reports,
                write_support_bundle,
            )
            if args.command in {"diagnose", "support"}:
                try:
                    client = AgentClient(args.state_dir, profile=args.profile, timeout=1.0)
                except (AgentTransportError, OSError, ValueError):
                    client = None
                diagnostics = inspect_agent(args.state_dir, client)
                if args.command == "diagnose":
                    print(json.dumps(diagnostics, indent=2, ensure_ascii=True, allow_nan=False))
                    return 0 if diagnostics["ready"] else 2
                document = support_document(diagnostics, support_reports(args.state_dir, args.include_report),
                                            profile=args.privacy_profile)
                result = write_support_bundle(document, args.out) if args.out else document
            elif args.command == "recover":
                result = recover_session(args.state_dir, args.session_id, args.out_dir)
            else:
                result = remove_control_files(args.state_dir, apply=args.apply)
            print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
            return 0
        client = AgentClient(args.state_dir, profile=args.profile, timeout=args.request_timeout)
        if args.command == "stop":
            print(json.dumps(client.request("shutdown"), indent=2, ensure_ascii=True))
            return 0
        params = {}
        if args.command == "start":
            params = {"mode": "attach" if args.pid is not None else "launch", "timeout_seconds": args.timeout,
                      "sample_interval_ms": args.sample_interval, "windows_seconds": args.windows,
                      "warmup_seconds": args.warmup}
            target_args = args.target_args[1:] if args.target_args[:1] == ["--"] else args.target_args
            if args.pid is not None:
                if target_args:
                    raise AgentError("invalid_input", "attached targets do not accept arguments")
                params.update(pid=args.pid, create_time=args.create_time)
            else:
                if args.create_time is not None:
                    raise AgentError("invalid_input", "launch does not accept create_time")
                params.update(target=args.target, args=target_args)
        elif args.session_id is not None:
            params["session_id"] = args.session_id
        if args.command == "status" and (args.offset is not None or args.limit is not None):
            if args.session_id is not None:
                raise AgentError("invalid_input", "status pagination cannot select one session")
            if args.offset is not None:
                params["offset"] = args.offset
            if args.limit is not None:
                params["limit"] = args.limit
        if args.command == "events":
            params.update(cursor=args.cursor, limit=args.limit)
        print(json.dumps(client.request(args.command, params), indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    except (AgentTransportError, AgentError, OSError, ValueError) as error:
        print(json.dumps({"error": {"code": getattr(error, "code", "unavailable"), "message": str(error),
                                    "may_have_run": bool(getattr(error, "may_have_run", False))}}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
