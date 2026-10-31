"""Configuration and access diagnostics that never control a target."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import psutil
from rich.text import Text

from .config import resolve_settings
from .events import LeakResource
from .process_runner import ProcessManager, ProcessManagerError
from .theme import plain_console

INIT_CONFIG = '''# DrainTracer project settings. Command-line flags override these values.
sample_interval = "500ms"
windows = ["10s", "30s", "60s"]
warmup = "0s"
report_dir = "leak_reports/reports"

# Select explicitly with --profile ci for bounded headless checks.
[profiles.ci]
timeout = "2m"
fail_on = "medium"
fail_on_inconclusive = true
fail_on_target_error = true
'''


def _validate_generated_config():
    """Use the normal file loader for both generated base and CI profile."""
    with tempfile.TemporaryDirectory(prefix="draintracer-init-check-") as directory:
        source = Path(directory) / ".drainrc.toml"
        source.write_text(INIT_CONFIG, encoding="utf-8")
        resolve_settings(config_path=source, discover=False)
        resolve_settings(config_path=source, profile="ci", discover=False)


def run_init(arguments, console):
    """Preview by default; exclusive file creation never replaces project data."""
    parser = argparse.ArgumentParser(prog="draintracer init", description="Preview a validated project configuration.")
    parser.add_argument("--path", default=".drainrc.toml", help="New configuration destination (parent must exist).")
    parser.add_argument("--write", action="store_true", help="Create the file; existing files are never overwritten.")
    parser.add_argument("--json", action="store_true", help="Emit initialization schema 1, including the preview.")
    parser.add_argument("--plain", action="store_true", help="Disable terminal styling.")
    parser.add_argument("--no-color", action="store_true", help="Disable terminal colors.")
    parser.add_argument("--quiet", action="store_true", help="Show only the result, without the template.")
    args = parser.parse_args(arguments)
    if args.no_color:
        console.no_color = True
    if args.plain:
        console = plain_console(console)
    result = {"initialization_schema": 1, "path": args.path, "content": INIT_CONFIG,
              "written": False, "partial_file": False, "error": None}
    created = False
    try:
        if "\0" in args.path:
            raise ValueError("the destination path contains a null character")
        destination = Path(args.path).absolute()
        result["path"] = str(destination)
        _validate_generated_config()
        if not destination.parent.is_dir():
            raise OSError("the destination's parent directory must exist")
        if args.write:
            with destination.open("x", encoding="utf-8", newline="\n") as handle:
                created = True
                handle.write(INIT_CONFIG)
                handle.flush()
                os.fsync(handle.fileno())
            result["written"] = True
    except FileExistsError:
        result["error"] = "Refusing to overwrite an existing file; choose a new --path."
    except (ValueError, OSError, RuntimeError) as error:
        result["error"] = str(error)
        result["partial_file"] = created
        if created:
            result["error"] += " The new file may be partial; inspect it before retrying."
    if args.json:
        print(json.dumps(result, allow_nan=False), file=console.file)
    elif result["error"]:
        console.print(Text(f"ERROR: {result['error']}"))
    else:
        label = "CREATED" if result["written"] else "PREVIEW"
        console.print(Text(f"{label}: {result['path']}"))
        if not args.quiet:
            console.print(Text(INIT_CONFIG))
        if not result["written"]:
            console.print(Text("Pass --write to create this file; existing files are never overwritten."))
    return 2 if result["error"] else 0


def _check(checks, name, state, message):
    checks.append({"check": name, "state": state, "message": message})


def _resolved_path(value):
    if "\0" in str(value):
        raise ValueError("the path contains a null character")
    return Path(value).resolve()


def settings_record(settings):
    """Expose monitoring values and origins, excluding experiment free text."""
    names = (
        "sample_interval_ms", "windows", "warmup", "timeout", "fail_on",
        "fail_on_inconclusive", "fail_on_target_error", "require_full_coverage", "report_dir",
    )
    values = {name: getattr(settings, name) for name in names if hasattr(settings, name)}
    values["fail_on"] = settings.fail_on.value
    values["windows"] = list(settings.windows)
    values["report_dir"] = str(_resolved_path(settings.report_dir))
    values["budgets"] = dict(settings.budgets)
    values["thresholds"] = {
        resource.value: {
            "critical_delta": settings.thresholds[resource].critical_delta,
            "critical_slope": settings.thresholds[resource].critical_slope,
        } for resource in LeakResource
    }
    keys = list(values)
    keys += [f"budgets.{metric}" for metric in settings.budgets]
    keys += [f"thresholds.{resource.value}.{name}" for resource in LeakResource
             for name in ("critical_delta", "critical_slope")]
    return {
        "source": str(settings.source.resolve()) if settings.source is not None else None,
        "profile": settings.profile,
        "values": values,
        "origins": {key: settings.origins.get(key, "built-in default") for key in keys},
    }


def _report_access(path: str, checks):
    try:
        directory = _resolved_path(path)
        nearest = directory
        while not nearest.exists() and nearest.parent != nearest:
            nearest = nearest.parent
        if not nearest.is_dir():
            raise OSError("the report path or an existing parent is not a directory")
        with tempfile.TemporaryFile(prefix=".draintracer-doctor-", dir=nearest) as probe:
            probe.write(b"draintracer access probe\n")
            probe.flush()
            os.fsync(probe.fileno())
        suffix = "" if directory.exists() else " The report directory does not exist yet."
        _check(checks, "report_directory", "ok",
               f"Temporary create/write/remove succeeded in {nearest}.{suffix}")
    except (ValueError, OSError, RuntimeError) as error:
        _check(checks, "report_directory", "error",
               f"Choose a writable report directory: {error}")


def _target_access(target: str, arguments: list[str], checks):
    try:
        path = _resolved_path(target)
        if not path.is_file():
            _check(checks, "target", "error", f"Target is not an existing file: {path}")
            return
        # Reuse command construction without invoking start, attach, or Popen.
        ProcessManager(str(path), args=arguments)._build_command()
        if path.suffix.lower() == ".py":
            with path.open("rb") as source:
                source.read(1)
            interpreter = Path(sys.executable)
            if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
                _check(checks, "interpreter", "error", "The current Python interpreter is not executable.")
            else:
                _check(checks, "interpreter", "ok", f"Python interpreter: {interpreter}")
        elif sys.platform.startswith("linux") and not os.access(path, os.X_OK):
            _check(checks, "target", "error", f"Target is not executable; check its execute permissions: {path}")
            return
        _check(checks, "target", "ok", f"Target file exists: {path}")
    except (ValueError, OSError, RuntimeError, ProcessManagerError) as error:
        _check(checks, "target", "error", f"Target cannot be used: {error}")


def _pid_access(pid: int, checks):
    if pid == os.getpid():
        _check(checks, "pid", "error", "DrainTracer cannot attach to itself.")
        return
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            raise psutil.NoSuchProcess(pid)
        _check(checks, "pid", "ok", f"PID {pid} is running; no control action was attempted.")
        readers = (("memory", process.memory_info), ("threads", process.num_threads),
                   ("handles", process.num_handles if sys.platform == "win32" else process.num_fds))
        for metric, read in readers:
            try:
                read()
                _check(checks, f"pid_{metric}", "ok", f"PID {pid}: {metric} readable now.")
            except (psutil.AccessDenied, OSError):
                _check(checks, f"pid_{metric}", "warning",
                       f"PID {pid}: {metric} unavailable; monitoring may have partial coverage. "
                       "Check the intended account's permissions.")
        if not process.is_running():
            _check(checks, "pid_identity", "error", "Process exited or changed during the check; repeat doctor.")
    except psutil.AccessDenied:
        _check(checks, "pid", "warning", f"PID {pid} cannot be inspected; check process permissions.")
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        _check(checks, "pid", "error", f"No running process with PID {pid}; select it again.")
    except OSError as error:
        _check(checks, "pid", "warning", f"Cannot inspect PID {pid}: {error}")


def run_doctor(arguments, console, *, parser_factory, settings_resolver):
    """Reuse normal flag/config validation; return 2 for blocking diagnostics."""
    parser = parser_factory()
    parser.prog = "draintracer doctor"
    parser.description = "Check settings and paths without launching or controlling a target."
    parser.epilog = "Example: draintracer doctor --target worker.py --timeout 30s --json"
    parser.add_argument("--json", action="store_true", help="Emit diagnostic schema 1 as JSON.")
    args = parser.parse_args(arguments)
    if args.no_color:
        console.no_color = True
    if args.plain:
        console = plain_console(console)
    checks = []
    record = None
    try:
        settings = settings_resolver(args)
        record = settings_record(settings)
        for warning in settings.warnings():
            _check(checks, "duration", "warning", warning)
        _report_access(settings.report_dir, checks)
        if args.target is not None:
            forwarded = list(args.target_args or [])
            _target_access(args.target, forwarded[1:] if forwarded[:1] == ["--"] else forwarded, checks)
        elif args.pid is not None:
            _pid_access(args.pid, checks)
        else:
            _check(checks, "target", "warning", "No target or PID supplied; only settings and output access were checked.")
    except (ValueError, OSError, RuntimeError) as error:
        _check(checks, "settings", "error", str(error))
    result = {"diagnostic_schema": 1, "settings": record, "checks": checks,
              "target_controlled": False}
    blocked = any(check["state"] == "error" for check in checks)
    if args.json:
        print(json.dumps(result, allow_nan=False), file=console.file)
    else:
        if record is not None and not args.quiet:
            console.print(Text(f"Configuration: {record['source'] or 'built-in defaults'}"))
            for key, value in record["values"].items():
                if key == "thresholds":
                    for resource, limits in value.items():
                        for name, amount in limits.items():
                            leaf = f"thresholds.{resource}.{name}"
                            console.print(Text(f"{leaf}: {amount} ({record['origins'][leaf]})"))
                elif key == "budgets" and value:
                    for metric, amount in value.items():
                        leaf = f"budgets.{metric}"
                        console.print(Text(f"{leaf}: {amount} ({record['origins'][leaf]})"))
                else:
                    console.print(Text(f"{key}: {value} ({record['origins'][key]})"))
        for check in checks:
            if not args.quiet or check["state"] != "ok":
                console.print(Text(f"{check['state'].upper()}: {check['message']}"))
        console.print(Text(f"RESULT {'BLOCKED' if blocked else 'READY'}: no target control was attempted."))
    return 2 if blocked else 0
