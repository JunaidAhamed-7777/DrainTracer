"""Reproducible, session-owned workload recipes for local and CI monitoring."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from .config import MonitorSettings, resolve_settings
from .experiment import arguments_record
from .headless import EXIT_ERROR, EXIT_INTERRUPTED, HeadlessRunner
from .process_runner import ProcessManager
from .workload_logs import BoundedLog

EXIT_WORKLOAD = 6
MAX_RECIPE_BYTES = 65536
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


class RecipeError(ValueError):
    """A bounded recipe is invalid; errors never interpolate its secret values."""


class WorkloadFailure(RuntimeError):
    """Readiness or an exercise step failed, independently of leak evidence."""


def _object(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) - keys:
        raise RecipeError(f"Invalid {label} object or unknown option")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
        raise RecipeError(f"Invalid {label}; expected a nonempty bounded string")
    return value


def _seconds(value: Any, maximum: float, label: str) -> float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum
    except OverflowError:
        valid = False
    if not valid:
        raise RecipeError(f"Invalid {label}; expected seconds greater than zero and at most {maximum:g}")
    return float(value)


def _path(value: Any, base: Path, label: str, *, directory: bool = False) -> Path:
    path = (base / _text(value, label)).resolve()
    if not (path.is_dir() if directory else path.is_file()):
        raise RecipeError(f"The {label} must name an existing {'directory' if directory else 'file'}")
    return path


def _command(value: Any, base: Path, *, step: bool = False) -> dict:
    data = _object(value, {"target", "interpreter", "args", "timeout", "expect_exit"} if step else
                   {"target", "interpreter", "args"}, "command")
    target = _path(data.get("target"), base, "target")
    interpreter = _path(data["interpreter"], base, "interpreter") if "interpreter" in data else None
    args = data.get("args", [])
    if not isinstance(args, list) or len(args) > 128 or any(
        not isinstance(arg, str) or len(arg) > 4096 or "\0" in arg for arg in args
    ):
        raise RecipeError("Command args must be at most 128 bounded literal strings")
    command = {"target": str(target), "interpreter": str(interpreter) if interpreter else None,
               "args": args}
    if step:
        command["timeout"] = _seconds(data.get("timeout", 30), 3600, "exercise timeout")
        expected = data.get("expect_exit", 0)
        if type(expected) is not int or not 0 <= expected <= 255:
            raise RecipeError("Exercise expect_exit must be an integer from zero to 255")
        command["expect_exit"] = expected
    return command


def _pairs(items: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise RecipeError("Duplicate recipe key")
        result[key] = value
    return result


@dataclass(frozen=True)
class Recipe:
    path: Path
    command: dict
    cwd: Path
    env: dict[str, str]
    environment: dict
    secrets: tuple[str, ...]
    timeout: float
    readiness: dict | None
    exercise: tuple[dict, ...]
    log_limit: int | None
    settings: MonitorSettings

    def provenance(self) -> dict:
        def command_record(command: dict) -> dict:
            return {key: value for key, value in command.items() if key != "args"} | {
                "arguments": arguments_record(command["args"]),
                "effective_interpreter": command["interpreter"] or (
                    sys.executable if Path(command["target"]).suffix.lower() == ".py" else
                    (self.env.get("COMSPEC") or "cmd.exe")
                    if sys.platform == "win32" and Path(command["target"]).suffix.lower() in {".bat", ".cmd"}
                    else None)}

        record = {"schema": 1, "recipe_path": str(self.path), "cwd": str(self.cwd),
                  "command": command_record(self.command), "environment": self.environment,
                  "timeout_seconds": self.timeout,
                  "configuration": {"path": str(self.settings.source) if self.settings.source else None,
                                    "profile": self.settings.profile, "discovery": False},
                  "readiness": {"kind": "file", "timeout_seconds": self.readiness["timeout"]}
                  if self.readiness else {"kind": "none"},
                  "exercise": [command_record(command) for command in self.exercise],
                  "log_capture": {"enabled": self.log_limit is not None,
                                  "limit_bytes_per_stream": self.log_limit}}
        # Env values never participate, even in a guessable fingerprint.
        record["recipe_fingerprint"] = hashlib.sha256(
            json.dumps(record, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return record


def read_recipe(path: str | Path) -> Recipe:
    """Validate the whole recipe without echoing filesystem or config payloads."""
    try:
        return _read_recipe(path)
    except RecipeError:
        raise
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        raise RecipeError("Could not resolve a valid recipe, configuration, or filesystem path") from exc


def _read_recipe(path: str | Path) -> Recipe:
    path = Path(path).resolve()
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECIPE_BYTES + 1)
        if len(raw) > MAX_RECIPE_BYTES:
            raise RecipeError("Recipe exceeds the 64 KiB input limit")
        data = json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(RecipeError("Invalid number")))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, RecipeError):
            raise
        raise RecipeError("Could not read a valid UTF-8 JSON recipe") from exc
    data = _object(data, {"schema", "command", "cwd", "environment", "inherit_environment",
                          "timeout", "readiness", "exercise", "capture_logs", "config", "profile"}, "recipe")
    if type(data.get("schema")) is not int or data["schema"] != 1:
        raise RecipeError("Unsupported recipe schema; expected 1")
    base = path.parent
    command = _command(data.get("command"), base)
    cwd = _path(data.get("cwd", "."), base, "cwd", directory=True)
    inherit = data.get("inherit_environment", False)
    if type(inherit) is not bool:
        raise RecipeError("inherit_environment must be true or false")
    # Windows needs system locations even with an otherwise clean environment.
    essentials = ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    env = dict(os.environ) if inherit else {key: os.environ[key] for key in essentials if key in os.environ}
    selected = data.get("environment", {})
    if not isinstance(selected, dict) or len(selected) > 64:
        raise RecipeError("environment must contain at most 64 selected variables")
    sources = []
    secrets = []
    for name, value in selected.items():
        if not _ENV_NAME.fullmatch(name):
            raise RecipeError("Invalid environment variable name")
        source = "literal"
        if isinstance(value, dict):
            value = _object(value, {"from_env"}, "environment source")
            origin = _text(value.get("from_env"), "environment source name")
            if not _ENV_NAME.fullmatch(origin) or origin not in os.environ:
                raise RecipeError("A selected environment source is missing or invalid")
            value = os.environ[origin]
            source = "parent_environment"
        if not isinstance(value, str) or len(value) > 4096 or "\0" in value:
            raise RecipeError("Environment values must be strings of at most 4096 characters")
        env[name] = value
        secrets.append(value)
        sources.append({"name": name, "source": source})
    environment = {"mode": "inherit" if inherit else "selected", "selected": sorted(sources, key=lambda item: item["name"]),
                   "platform_basics": [key for key in essentials if key in env and key not in selected],
                   "values_recorded": False, "values_compared": False}
    readiness = None
    if "readiness" in data:
        ready = _object(data["readiness"], {"file", "timeout"}, "readiness")
        marker = cwd / _text(ready.get("file"), "readiness file")
        # lexists also rejects a dangling link; never remove a pre-existing marker.
        if os.path.lexists(marker):
            raise RecipeError("Readiness file already exists; choose a fresh session-owned marker")
        if not marker.parent.is_dir():
            raise RecipeError("Readiness file parent directory must exist")
        readiness = {"file": marker, "timeout": _seconds(ready.get("timeout", 30), 300, "readiness timeout")}
    exercise = data.get("exercise", [])
    if not isinstance(exercise, list) or len(exercise) > 8:
        raise RecipeError("exercise must contain at most eight commands")
    exercise = tuple(_command(item, base, step=True) for item in exercise)
    log_limit = None
    if "capture_logs" in data:
        capture = _object(data["capture_logs"], {"max_bytes"}, "capture_logs")
        log_limit = capture.get("max_bytes", 65536)
        if type(log_limit) is not int or not 1024 <= log_limit <= 1048576:
            raise RecipeError("Log max_bytes must be an integer from 1024 to 1048576")
    timeout = _seconds(data.get("timeout", 60), 86400, "session timeout")
    config = _path(data["config"], base, "configuration") if "config" in data else None
    profile = _text(data["profile"], "profile") if "profile" in data else None
    settings = resolve_settings(config_path=config, profile=profile, discover=False,
                                overrides={"timeout": timeout})
    return Recipe(path, command, cwd, env, environment, tuple(secrets),
                  timeout, readiness, exercise, log_limit, settings)


def _configure(manager: ProcessManager, command: dict, recipe: Recipe) -> None:
    manager.target_path = command["interpreter"] or command["target"]
    manager.args = ([command["target"]] if command["interpreter"] else []) + command["args"]
    manager.cwd = str(recipe.cwd)
    manager.env = dict(recipe.env)
    manager.capture_output = recipe.log_limit is not None


class WorkloadRunner(HeadlessRunner):
    """Monitor throughout readiness and exercises; all process control is owned."""

    def __init__(self, recipe: Recipe, *, report_dir: str | Path | None = None,
                 console: Console | None = None) -> None:
        super().__init__(recipe.command["target"], args=recipe.command["args"], console=console,
                         report_dir=report_dir if report_dir is not None else recipe.settings.report_dir,
                         timeout=recipe.timeout, settings=recipe.settings,
                         fail_on=recipe.settings.fail_on,
                         fail_on_inconclusive=recipe.settings.fail_on_inconclusive,
                         fail_on_target_error=recipe.settings.fail_on_target_error)
        self.recipe = recipe
        _configure(self.manager, recipe.command, recipe)
        self.workflow = {"state": "not_started", "readiness": "not_started", "exercise": []}
        self._logs: list[BoundedLog] = []
        self._log_dir: Path | None = None
        self._exercise_manager: ProcessManager | None = None
        self._streams = []

    def run(self) -> int:
        """Export workload failures too, and clean both kinds of owned tree."""
        self._print_banner()
        started = False
        try:
            self.start()
            started = True
            self._monitor()
        except KeyboardInterrupt:
            self._interrupted = True
            self.workflow["state"] = "interrupted"
            self._append_event("Workload interrupted; cleaning owned processes", "warn")
        except (RecipeError, WorkloadFailure):
            self.workflow["state"] = "failed"
            self._append_event("Workload execution could not complete", "error")
        except Exception:  # noqa: BLE001 - CLI boundary; keep private diagnostics out of artifacts.
            # OS exceptions can contain private paths; never copy arbitrary
            # command diagnostics or selected env values into the report.
            self.workflow["state"] = "monitor_error" if started else "startup_failed"
            self._run_error = "Workload monitoring failed" if started else "Workload target could not start"
            self._append_event(self._run_error, "error")
        finally:
            try:
                self.shutdown()
            except KeyboardInterrupt:
                self._interrupted = True
                self._force_cleanup()
            except Exception:  # noqa: BLE001 - cleanup must still export its outcome.
                self.workflow["state"] = "failed"
                self.workflow["failure"] = "Owned process cleanup could not complete"
                self._force_cleanup()
        try:
            self.poll()
        except Exception:  # noqa: BLE001 - export the evidence retained before this failure.
            self.workflow["state"] = "failed"
            self.workflow["failure"] = "Final workload evidence or captured output is incomplete"
        self._export()
        self._print_summary()
        return self.exit_status()

    def _print_summary(self) -> None:
        if self.exit_status() != EXIT_WORKLOAD:
            super()._print_summary()
            return
        self._print_line("error", "RESULT WORKLOAD FAILED: " + self.workflow.get(
            "failure", "readiness or an exercise step could not complete"))
        self._print_line("info", f"Leak evidence: {self.signals_text()}; {self._measurement_text()}")
        if self._export_error:
            self._print_line("error", "Report export failed")
        for label, path in (("Report", self._last_report), ("HTML", self._last_html_report)):
            if path is not None:
                self._print_line("info", f"{label}: {path}")

    def _capture(self, manager: ProcessManager, label: str) -> None:
        if self.recipe.log_limit is None:
            return
        streams = manager.output_streams
        self._streams.extend(streams.values())
        if self._log_dir is None:
            self._log_dir = Path(self.report_dir) / f"workload-logs-{uuid.uuid4().hex}"
            self._log_dir.mkdir(parents=True, mode=0o700)
        for name, stream in streams.items():
            log = BoundedLog(stream, self._log_dir / f"{label}-{name}.log",
                             self.recipe.log_limit, self.recipe.secrets)
            self._logs.append(log)
            log.start()

    def start(self) -> None:
        self.workflow["state"] = "starting"
        # Recheck just before launch: parsing may have happened much earlier.
        if self.recipe.readiness and os.path.lexists(self.recipe.readiness["file"]):
            self.workflow.update(state="failed", readiness="stale_file")
            raise RecipeError("Readiness file already exists; refusing to launch")
        try:
            super().start()
            self._capture(self.manager, "target")
        except BaseException:
            self.workflow["state"] = "failed"
            self.shutdown()
            raise

    def poll(self) -> int:
        count = super().poll()
        if any(log.error for log in self._logs):
            raise WorkloadFailure("Captured output could not be stored safely")
        return count

    def _tick(self, deadline: float, stage: str) -> None:
        self.poll()
        if self.timed_out or time.monotonic() >= deadline:
            raise WorkloadFailure(f"{stage} timed out")
        if self.tree_finished:
            raise WorkloadFailure(f"Target exited during {stage.lower()}")
        time.sleep(0.02)

    def _monitor(self) -> None:
        try:
            self.workflow["state"] = "running"
            if self.recipe.readiness:
                self.workflow["readiness"] = "waiting"
                self._append_event("Waiting for workload readiness file", "info")
                deadline = time.monotonic() + self.recipe.readiness["timeout"]
                while not self.recipe.readiness["file"].is_file():
                    self._tick(deadline, "Readiness")
                if self.recipe.readiness["file"].is_symlink():
                    raise WorkloadFailure("Readiness marker must be a regular file")
                self.workflow["readiness"] = "ready"
            else:
                self.workflow["readiness"] = "not_requested"
            for index, command in enumerate(self.recipe.exercise, 1):
                state = {"index": index, "state": "running", "exit_code": None}
                self.workflow["exercise"].append(state)
                manager = ProcessManager(command["target"])
                self._exercise_manager = manager
                _configure(manager, command, self.recipe)
                try:
                    manager.start()
                    self._capture(manager, f"exercise-{index}")
                    deadline = time.monotonic() + command["timeout"]
                    while manager.is_running:
                        self._tick(deadline, f"Exercise {index}")
                    state["exit_code"] = manager.exit_code
                    if manager.exit_code != command["expect_exit"]:
                        state["state"] = "failed"
                        raise WorkloadFailure(f"Exercise {index} returned an unexpected exit code")
                    state["state"] = "complete"
                except Exception as exc:
                    if isinstance(exc, WorkloadFailure):
                        raise
                    raise WorkloadFailure(f"Exercise {index} could not run") from exc
                finally:
                    if not self._cleanup_exercise():
                        raise WorkloadFailure(f"Exercise {index} cleanup is incomplete")
            self.workflow["state"] = "complete"
            self._append_event("Workload readiness and exercise steps complete", "info")
            super()._monitor()
        except WorkloadFailure as exc:
            self.workflow.update(state="failed", failure=str(exc))
            if self.workflow["readiness"] == "waiting":
                self.workflow["readiness"] = "failed"
            for state in self.workflow["exercise"]:
                if state["state"] == "running":
                    state["state"] = "failed"
            self._append_event(str(exc), "error")
        except KeyboardInterrupt:
            self.workflow["state"] = "interrupted"
            raise

    def _cleanup_exercise(self) -> bool:
        manager = self._exercise_manager
        if manager is None:
            return True
        try:
            manager.terminate()
        finally:
            if manager.has_tracked_processes:
                manager.kill()
            remaining = len(manager.tracked_pids)
            self.workflow["exercise_cleanup"] = {"ownership": "launched", "remaining_count": remaining,
                                                  "state": "incomplete" if remaining else "complete"}
            if not remaining:
                self._exercise_manager = None
        return not remaining

    def shutdown(self) -> None:
        try:
            if not self._cleanup_exercise():
                self.workflow.update(state="failed", failure="Exercise cleanup is incomplete")
        finally:
            try:
                super().shutdown()
            finally:
                for log in self._logs:
                    log.stop()
                for stream in [*self._streams, *self.manager.output_streams.values()]:
                    if not stream.closed:
                        stream.close()

    def build_report(self) -> dict:
        report = super().build_report()
        report["outcome"]["workload"] = {"state": self.workflow["state"]}
        report["workload"] = self.recipe.provenance() | {
            "outcome": self.workflow, "logs": [log.record() for log in self._logs]}
        return report

    def exit_status(self) -> int:
        if self._interrupted:
            return EXIT_INTERRUPTED
        if self.workflow["state"] == "failed":
            return EXIT_WORKLOAD
        return super().exit_status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor a bounded local/CI workload recipe (headless).")
    parser.add_argument("recipe", type=Path, help="UTF-8 JSON recipe, schema 1")
    parser.add_argument("--report-dir", type=Path, help="override the selected configuration's report directory")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    options = parser.parse_args(argv)
    try:
        recipe = read_recipe(options.recipe)
    except RecipeError as exc:
        print(f"Invalid workload recipe: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return WorkloadRunner(recipe, report_dir=options.report_dir,
                          console=Console(no_color=True) if options.no_color else None).run()


if __name__ == "__main__":
    raise SystemExit(main())
