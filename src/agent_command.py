"""Explicit foreground Windows agent and its minimal authenticated JSON client."""

from __future__ import annotations

import argparse
import json
import os
import threading
import uuid
from pathlib import Path

from .agent_service import AgentError, AgentPolicy, AgentRegistry
from .agent_transport import (
    AgentTransportError,
    PipeListener,
    authenticate,
    connect,
    load_secret,
    pipe_name,
    private_directory,
)


class AgentServer:
    def __init__(self, state_directory, *, profile="default", policy=None):
        self.directory = private_directory(state_directory)
        self.secret = load_secret(self.directory, create=True)
        self.reports = private_directory(self.directory / "reports")
        self.registry = AgentRegistry(self.reports, policy or AgentPolicy())
        self.listener = PipeListener(pipe_name(profile))
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
        finally:
            try:
                clean = self.registry.close(timeout=15)
            finally:
                self.listener.close()
        return clean


class AgentClient:
    def __init__(self, state_directory, *, profile="default", timeout=15.0):
        self.secret = load_secret(Path(state_directory))
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
    parser.add_argument("--state-dir", default=str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DrainTracerAgent"))
    parser.add_argument("--profile", default="default", help="Local endpoint profile, not a monitor configuration profile.")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Start explicitly in the foreground; stop with Ctrl+C.")
    server.add_argument("--allow-launch", action="store_true")
    server.add_argument("--launch-root", action="append", default=[])
    server.add_argument("--allow-pause", action="store_true", help="Authorize pause/resume for launched targets only.")
    server.add_argument("--allow-kill", action="store_true", help="Authorize explicit target kill separately from cancellation.")
    server.add_argument("--max-sessions", type=int, default=4)
    server.add_argument("--max-records", type=int, default=64)
    server.add_argument("--max-timeout", type=float, default=3600)
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
    events = commands.add_parser("events")
    events.add_argument("session_id")
    events.add_argument("--cursor", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    for operation in ("cancel", "export", "pause", "resume", "kill"):
        commands.add_parser(operation).add_argument("session_id")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            policy = AgentPolicy(allow_launch=args.allow_launch, allow_pause=args.allow_pause,
                                 allow_kill=args.allow_kill, launch_roots=tuple(map(Path, args.launch_root)),
                                 max_sessions=args.max_sessions, max_records=args.max_records,
                                 max_timeout=args.max_timeout)
            server = AgentServer(args.state_dir, profile=args.profile, policy=policy)
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
        client = AgentClient(args.state_dir, profile=args.profile)
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
