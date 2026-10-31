"""Collect the pre-registered repeated calibration set without changing defaults."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from unittest import mock

import psutil
from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import MonitorSettings
from src.events import LeakSeverity, severity_rank
from src.headless import HeadlessRunner
from src.leak_detector import DEFAULT_THRESHOLDS


@dataclass(frozen=True)
class Case:
    name: str
    fixture: str
    arguments: tuple[str, ...]
    purpose: str
    intended_growth: str | None = None
    warmup: float = 3.0
    deny_root: bool = False


CASES = (
    Case("stable", "steady.py", ("--memory-mb", "8", "--handles", "4", "--threads", "2"), "non-growth"),
    Case("cache-plateau", "calibration_target.py", ("cache",), "benign retention", warmup=0),
    Case("startup", "startup_spike.py", ("--startup-seconds", "2.5", "--startup-mb", "96"),
         "benign startup", warmup=0),
    Case("startup-warmup", "startup_spike.py", ("--startup-seconds", "2.5", "--startup-mb", "96"),
         "non-growth after warm-up"),
    Case("recovery", "calibration_target.py", ("recovery",), "recovering allocation"),
    Case("worker-churn", "calibration_target.py", ("churn",), "bounded worker churn"),
    Case("fast-memory", "memory_leak.py", ("--chunk-mb", "2", "--interval", "0.25", "--max-mb", "128"),
         "known growth then cap", "memory"),
    Case("slow-memory", "memory_leak.py", ("--chunk-mb", "0.25", "--interval", "0.5", "--max-mb", "48"),
         "sustained known growth", "memory"),
    Case("handle-growth", "handle_leak.py", ("--handles-per-tick", "10", "--interval", "0.25", "--max-handles", "800"),
         "known growth then cap", "handles"),
    Case("thread-growth", "thread_leak.py", ("--threads-per-tick", "1", "--interval", "0.25", "--max-threads", "80"),
         "known growth then cap", "threads"),
    Case("partial-access", "process_tree.py", ("--depth", "1", "--fanout", "1", "--memory-mb", "1"),
         "injected root read denial", deny_root=True),
    Case("larger-tree", "process_tree.py", ("--depth", "3", "--fanout", "2", "--memory-mb", "1"),
         "stable fifteen-process tree"),
)


def stats(values: list[float]) -> dict:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return {"count": 0, "mean": None, "p95": None, "max": None}
    position = (len(ordered) - 1) * 0.95
    low, high = math.floor(position), math.ceil(position)
    percentile = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return {"count": len(ordered), "mean": round(statistics.mean(ordered), 4),
            "p95": round(percentile, 4), "max": round(ordered[-1], 4)}


class MeasuredRunner(HeadlessRunner):
    def __init__(self, *args, **kwargs):
        self.snapshot_age_ms: list[float] = []
        self.export_ms: float | None = None
        super().__init__(*args, **kwargs)

    def _accept_snapshot(self, snapshot, *, already_observed=False):
        self.snapshot_age_ms.append(max(0, time.time() - snapshot.timestamp) * 1000)
        super()._accept_snapshot(snapshot, already_observed=already_observed)

    def _export(self):
        started = time.perf_counter()
        try:
            super()._export()
        finally:
            self.export_ms = (time.perf_counter() - started) * 1000


class _DeniedMemory:
    """Only the sampler's root memory read fails; identity/cleanup stay real."""
    def __init__(self, process):
        self._process = process

    def __getattr__(self, name):
        return getattr(self._process, name)

    def memory_info(self):
        raise psutil.AccessDenied(self.pid)


def summarize_run(runner: MeasuredRunner, case: Case, repeat: int, code: int) -> dict:
    report = runner.build_report()
    snapshots = list(runner._snapshot_history)
    start = runner._started_at
    resources = {}
    for resource in ("memory", "handles", "threads"):
        first, first_high = None, None
        windows = {str(int(window)): {"judged_samples": 0, "first_judged_s": None}
                   for window in runner.settings.windows}
        for sample in snapshots:
            elapsed = sample.timestamp - start if start is not None else None
            for alert in sample.alerts:
                if alert.resource.value == resource:
                    first = elapsed if first is None else first
                    if severity_rank(alert.severity) >= severity_rank(LeakSeverity.HIGH):
                        first_high = elapsed if first_high is None else first_high
            for evidence in sample.evidence:
                if evidence.resource.value == resource:
                    for window in evidence.windows:
                        if window.judged:
                            item = windows[str(int(window.window_seconds))]
                            item["judged_samples"] += 1
                            if item["first_judged_s"] is None:
                                item["first_judged_s"] = round(elapsed, 4)
        facts = report["session_facts"]["signals"][resource]
        resources[resource] = {"worst": facts["worst"], "episodes": facts["episodes"],
                               "first_signal_s": round(first, 4) if first is not None else None,
                               "first_high_s": round(first_high, 4) if first_high is not None else None,
                               "windows": windows}
    diagnostics = [sample.diagnostics for sample in snapshots if sample.diagnostics is not None]
    return {
        "scenario": case.name, "repeat": repeat, "purpose": case.purpose,
        "intended_growth": case.intended_growth, "injected_root_read_denial": case.deny_root,
        "fixture": case.fixture, "target_arguments": [*case.arguments, "--duration", "90"],
        "warmup_seconds": runner.settings.warmup, "exit_code": code,
        "interrupted": runner._interrupted,
        "duration_seconds": round(runner._uptime(), 4),
        "detection_ran": report["detection_ran"], "partial_coverage": report["coverage"]["partial"],
        "coverage": {key: value for key, value in report["coverage"].items()
                     if key in {"samples", "metrics", "full", "fully_measured_samples", "measured_samples"}},
        "samples": report["session_facts"]["samples"], "signals": resources,
        "target_peaks": {metric: value["value"] for metric, value in report["session_facts"]["peaks"].items()},
        "maximum_processes": max((len(sample.processes) for sample in snapshots), default=0),
        "monitor": report["session_facts"]["monitor"],
        "collection_ms": stats([item.collection_ms for item in diagnostics if item.collection_ms is not None]),
        "snapshot_age_at_consumption_ms": stats(runner.snapshot_age_ms),
        "export_ms": round(runner.export_ms, 4) if runner.export_ms is not None else None,
        "monitor_error_count": len(report["outcome"]["monitor_errors"]),
        "operational_error": code in {2, 130} or runner._run_error is not None or runner._export_error is not None,
        "export_failed": runner._export_error is not None,
        "cleanup_complete": not runner.manager.has_tracked_processes,
        "report_pair_written": runner._last_report is not None and runner._last_html_report is not None,
    }


def run_case(case: Case, repeat: int, directory: Path, *, smoke: bool = False) -> dict:
    interval, windows, duration = (100, (1.0,), 2.0) if smoke else (500, (10.0, 30.0, 60.0), 75.0)
    warmup = min(case.warmup, 0.1) if smoke else case.warmup
    executable = getattr(sys, "_base_executable", None) or sys.executable
    with open(os.devnull, "w", encoding="utf-8") as output:
        runner = MeasuredRunner(executable, args=[str(ROOT / "torture" / case.fixture),
                                                 *case.arguments, "--duration", "90"],
                                console=Console(file=output), report_dir=directory,
                                settings=MonitorSettings(sample_interval_ms=interval, windows=windows,
                                                         warmup=warmup, timeout=duration), timeout=duration)
        original = runner.manager.iter_psutil_processes

        def readable():
            return [_DeniedMemory(process) if process.pid == runner.manager.root_pid else process
                    for process in original()]

        context = mock.patch.object(runner.manager, "iter_psutil_processes", side_effect=readable)
        try:
            if case.deny_root:
                with context:
                    code = runner.run()
            else:
                code = runner.run()
            return summarize_run(runner, case, repeat, code)
        finally:
            if runner.manager.has_tracked_processes:
                runner.manager.kill()


def source_record() -> dict:
    command, directory = "git", str(ROOT)
    pointer = ROOT / ".git"
    if sys.platform.startswith("linux") and pointer.is_file() and ":/" in pointer.read_text().replace("\\", "/"):
        # Windows-created worktree pointers are not POSIX paths. Use its owner
        # Git so CRLF normalization and worktree metadata match the checkout.
        command = shutil.which("git.exe")
        if command is None:
            raise RuntimeError("A Windows-owned worktree under WSL requires git.exe for source identity")
        directory = subprocess.check_output(["wslpath", "-w", str(ROOT)], text=True).strip()

    def git(*arguments):
        return subprocess.check_output([command, "-C", directory, *arguments], text=True).strip()
    files = [*ROOT.glob("src/*.py"), *ROOT.glob("torture/*.py"), ROOT / "runtime-manifest.json",
             ROOT / "docs/calibration-protocol.md"]
    return {"commit": git("rev-parse", "HEAD"), "tracked_changes": bool(git("status", "--porcelain", "--untracked-files=no")),
            "runtime_version": HeadlessRunner.VERSION,
            "sha256": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted(files)}}


def save(path: Path, record: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(record, output, indent=2, allow_nan=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new aggregate JSON destination; raw evidence stays beside it")
    parser.add_argument("--smoke", action="store_true", help="quick harness validation; never a calibration measurement")
    parser.add_argument("--concurrent-windows-wsl", action="store_true", help="record shared-host concurrent measurement conditions")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("refusing to overwrite an existing aggregate dataset")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    memory = psutil.virtual_memory()
    source = source_record()
    if source["tracked_changes"] and not args.smoke:
        parser.error("commit tracked changes before collecting a calibration dataset")
    record = {"calibration_schema": 1, "kind": "harness_smoke" if args.smoke else "preregistered_calibration",
              "state": "running", "started_at": datetime.now(timezone.utc).isoformat(), "source": source,
              "environment": {"os": platform.system(), "release": platform.release(), "version": platform.version(),
                              "machine": platform.machine(), "python": platform.python_version(),
                              "psutil": version("psutil"), "rich": version("rich"), "logical_cpus": psutil.cpu_count(),
                              "physical_memory_bytes": memory.total, "available_memory_bytes_at_start": memory.available,
                              "host_cpu_percent_at_start": psutil.cpu_percent(interval=0.2),
                              "concurrent_windows_wsl": args.concurrent_windows_wsl},
              "settings": {"interval_ms": 100 if args.smoke else 500, "windows_seconds": [1] if args.smoke else [10, 30, 60],
                           "duration_seconds": 2 if args.smoke else 75, "repetitions": 1 if args.smoke else 3,
                           "thresholds": {resource.value: {"critical_delta": item.critical_delta,
                                                          "critical_slope": item.critical_slope}
                                          for resource, item in DEFAULT_THRESHOLDS.items()}}, "runs": []}
    save(args.output, record)
    failed = False
    for repeat in range(1, record["settings"]["repetitions"] + 1):
        for case in CASES:
            print(f"START {repeat} {case.name}", flush=True)
            baseline = {"host_cpu_percent": psutil.cpu_percent(interval=0.1),
                        "available_memory_bytes": psutil.virtual_memory().available}
            try:
                result = run_case(case, repeat, args.output.parent / (args.output.stem + "-reports") / f"{repeat}-{case.name}",
                                  smoke=args.smoke)
            except Exception as error:  # noqa: BLE001 - retain a failed run; never erase inconvenient measurements.
                result = {"scenario": case.name, "repeat": repeat, "operational_error_type": type(error).__name__}
                failed = True
            record["runs"].append(result)
            result["host_conditions_before_run"] = baseline
            failed = failed or result.get("operational_error", False) or not result.get("report_pair_written", False)
            save(args.output, record)
            print(f"DONE {repeat} {case.name}: {result.get('exit_code', 'error')}", flush=True)
            if result.get("cleanup_complete") is not True:
                record["state"] = "aborted_cleanup_unconfirmed"
                save(args.output, record)
                return 2
            if result.get("interrupted") or result.get("exit_code") == 130:
                record["state"] = "interrupted"
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                save(args.output, record)
                return 130
    record["state"] = "complete_with_errors" if failed else "complete"
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(args.output, record)
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
