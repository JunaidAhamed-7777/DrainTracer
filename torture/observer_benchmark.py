"""Reference-derived observer budgets and bounded native soak verification."""

from __future__ import annotations

import argparse
import dataclasses
import gc
import hashlib
import json
import math
import os
import platform
import sys
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from importlib.metadata import version
from pathlib import Path

import psutil
from calibrate import (
    ROOT,
    Case,
    MeasuredRunner,
    save,
    source_record,
    stats,
    summarize_run,
)
from rich.console import Console

from src.config import MonitorSettings
from src.process_runner import _windows_process_exited
from src.report_contract import read_report


class Footprint:
    """Fixed-space 50 ms RSS watcher, including export and reader validation."""
    def __init__(self):
        self.process = psutil.Process()
        self.peak = 0
        self.error = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)

    def __enter__(self):
        self.started = time.perf_counter()
        times = self.process.cpu_times()
        self.cpu_started = times.user + times.system
        self.thread.start()
        return self

    def _watch(self):
        try:
            while True:
                self.peak = max(self.peak, self.process.memory_info().rss)
                if self.stop.wait(0.05):
                    return
        except (OSError, psutil.Error):
            self.error = True

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(2)
        self.error = self.error or self.thread.is_alive()
        times = self.process.cpu_times()
        elapsed = time.perf_counter() - self.started
        self.cpu_percent = (times.user + times.system - self.cpu_started) / elapsed * 100

    def record(self):
        return {"harness_rss_peak_bytes": self.peak, "harness_cpu_percent_one_core": round(self.cpu_percent, 4),
                "watcher_error": self.error, "rss_watcher_interval_ms": 50}


class ObserverRunner(MeasuredRunner):
    def __init__(self, *args, **kwargs):
        self.post_collection_age_ms = []
        super().__init__(*args, **kwargs)

    def _accept_snapshot(self, snapshot, *, already_observed=False):
        diagnostics = snapshot.diagnostics
        if diagnostics is not None and diagnostics.collection_ms is not None:
            self.post_collection_age_ms.append(max(0, (time.time() - snapshot.timestamp) * 1000 - diagnostics.collection_ms))
        super()._accept_snapshot(snapshot, already_observed=already_observed)


def native(depth: int, repeat: int, duration: float, directory: Path):
    size = 2 ** (depth + 1) - 1
    arguments = ("--depth", str(depth), "--fanout", "2", "--memory-mb", "1")
    target_duration = 90 if duration <= 65 else duration + 30
    warmup = min(3, duration * 0.1)
    with Footprint() as footprint, open(os.devnull, "w", encoding="utf-8") as output:
        runner = ObserverRunner(getattr(sys, "_base_executable", None) or sys.executable,
            args=[str(ROOT / "torture/process_tree.py"), *arguments, "--duration", str(target_duration)],
            console=Console(file=output), report_dir=directory, timeout=duration,
            settings=MonitorSettings(sample_interval_ms=500, windows=(10, 30, 60), warmup=warmup, timeout=duration))
        try:
            code = runner.run()
            row = summarize_run(runner, Case(f"tree-{size}", "process_tree.py", arguments, "native observer cost"), repeat, code)
            row["target_arguments"] = [*arguments, "--duration", str(target_duration)]
            snapshots = list(runner._snapshot_history)
            diagnostics = [sample.diagnostics for sample in snapshots if sample.diagnostics is not None]
            row["monitor_cpu_percent"] = stats([item.monitor_cpu_percent for item in diagnostics if item.monitor_cpu_percent is not None])
            row["handle_classification_ms"] = stats([sample.diagnostics.handle_types_cost_ms for sample in snapshots
                if sample.handle_types is not None and sample.diagnostics is not None and sample.diagnostics.handle_types_cost_ms is not None])
            row["post_collection_age_estimate_ms"] = stats(runner.post_collection_age_ms)
            row["tree_size"] = size
            identities = {(process.pid, process.create_time) for sample in snapshots for process in sample.processes}
            unconfirmed = 0
            for pid, created in identities:
                try:
                    process = psutil.Process(pid)
                    if created is None:
                        unconfirmed += 1
                    elif process.create_time() == created:
                        exited = _windows_process_exited(pid) if sys.platform == "win32" else process.status() in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}
                        unconfirmed += not exited
                except psutil.NoSuchProcess:
                    pass
                except (psutil.Error, OSError):
                    unconfirmed += 1
            row["observed_identity_count"] = len(identities)
            row["unconfirmed_owned_cleanup_identities"] = unconfirmed
            row["cleanup_complete"] = row["cleanup_complete"] and unconfirmed == 0
            row["quality_pass"] = (not row["operational_error"] and row["cleanup_complete"]
                and not row["partial_coverage"] and row["detection_ran"] and row["monitor_error_count"] == 0
                and row["maximum_processes"] >= size and row["monitor"]["failed_samples"] == 0
                and row["monitor"]["dropped_snapshots"] == 0 and row["monitor"]["clock_adjustments"] == 0
                and row["monitor"]["sampling_gaps"] == 0)
            saved = read_report(runner._last_report)
            row["public_reader_roundtrip"] = True
            row["quality_pass"] = row["quality_pass"] and runner._last_html_report is not None and runner._last_html_report.is_file()
            row["exported_snapshot_stride"] = saved["snapshot_stride"]
            row["exported_snapshots"] = len(saved["snapshots"])
            row["report_bytes"] = runner._last_report.stat().st_size
            shape = max(snapshots, key=lambda sample: (len(sample.processes), sum(len(item.windows) for item in sample.evidence),
                                                      sample.handle_types is not None))
        finally:
            if runner.manager.has_tracked_processes:
                runner.manager.kill()
    row.update(footprint.record())
    return row, shape


def _fresh(value):
    if isinstance(value, Enum) or value is None or type(value) is bool:
        return value
    if dataclasses.is_dataclass(value):
        return type(value)(**{field.name: _fresh(getattr(value, field.name)) for field in dataclasses.fields(value)})
    if isinstance(value, dict):
        return {key: _fresh(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_fresh(item) for item in value)
    if isinstance(value, list):
        return [_fresh(item) for item in value]
    if type(value) in (int, float):
        return type(value)(str(value))
    if isinstance(value, str):
        return value.encode("utf-8").decode("utf-8")
    return value


def retention(shape, directory: Path, *, smoke=False):
    if psutil.virtual_memory().available < 2 * 1024**3:
        raise RuntimeError("At least 2 GiB available memory is required for retention stress")
    with Footprint() as footprint, open(os.devnull, "w", encoding="utf-8") as output:
        runner = ObserverRunner("retention-shape.exe", console=Console(file=output), report_dir=directory,
                                settings=MonitorSettings(note="Synthetic retention/export replay; not a native run"))
        limit = 8 if smoke else runner.SNAPSHOT_LIMIT - 1
        latest_summaries = shape.process_summaries
        shape = dataclasses.replace(shape, process_summaries=(), exited_processes=())
        started = time.perf_counter()
        for index in range(limit):
            sample = dataclasses.replace(_fresh(shape), timestamp=1000 + index * 0.5,
                                         metadata={"retention_replay": True})
            if index == limit - 1:
                sample = dataclasses.replace(sample, process_summaries=_fresh(latest_summaries))
            runner._accept_snapshot(sample)
        fill_ms = (time.perf_counter() - started) * 1000
        facts = runner.build_report()["session_facts"]
        runner._export()
        if runner._export_error is not None:
            raise RuntimeError("Retention export failed")
        saved = read_report(runner._last_report)
        if saved["session_facts"]["peaks"] != facts["peaks"] or saved["session_facts"]["signals"] != facts["signals"]:
            raise RuntimeError("Retention export changed exact resource evidence")
        if (saved["snapshots"][0]["timestamp"] != 1000
                or saved["snapshots"][-1]["timestamp"] != 1000 + (limit - 1) * 0.5):
            raise RuntimeError("Retention export lost its first or last point")
        row = {"kind": "synthetic_retention_export", "native_shape_processes": len(shape.processes),
               "filled_snapshots": limit, "fill_ms": round(fill_ms, 4), "export_ms": round(runner.export_ms, 4),
               "exported_snapshots": len(saved["snapshots"]), "exported_snapshot_stride": saved["snapshot_stride"],
               "report_bytes": runner._last_report.stat().st_size, "public_reader_roundtrip": True,
               "exact_facts_preserved": True}
    row.update(footprint.record())
    return row


def budgets(record):
    limits = {}
    for size in (1, 7, 31):
        rows = [row for row in record["runs"] if row["tree_size"] == size]
        values = {}
        for group, statistic, margin in (("collection_ms", "p95", 1.5), ("collection_ms", "max", 2),
            ("monitor_cpu_percent", "mean", 1.5), ("monitor_cpu_percent", "p95", 1.5),
            ("handle_classification_ms", "p95", 1.5), ("handle_classification_ms", "max", 2),
            ("post_collection_age_estimate_ms", "p95", 1.5), ("post_collection_age_estimate_ms", "max", 2)):
            known = [row[group][statistic] for row in rows if row[group][statistic] is not None]
            values[group + "." + statistic] = round(max(known) * margin, 4) if known else None
        limits[str(size)] = values
    all_rows = [*record["runs"], record["retention"]]
    return {"observer_budget_schema": 1, "os": record["environment"]["os"], "source": record["source"],
            "reference_started_at": record["started_at"], "per_tree": limits,
            "rss_peak_bytes": math.ceil(max(row["harness_rss_peak_bytes"] for row in all_rows) * 1.25),
            "export_ms": round(max(row["export_ms"] for row in all_rows) * 2, 4)}


def breaches(row, budget):
    result = []
    for key, limit in budget["per_tree"][str(row["tree_size"])].items():
        group, statistic = key.split(".")
        value = row[group][statistic]
        if limit is not None and (value is None or value > limit):
            result.append(key)
    if row["harness_rss_peak_bytes"] > budget["rss_peak_bytes"]:
        result.append("harness_rss_peak_bytes")
    if row["export_ms"] > budget["export_ms"]:
        result.append("export_ms")
    if not row["quality_pass"] or row["watcher_error"]:
        result.append("measurement_quality")
    return result


def load_budget(path: Path):
    with path.open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Frozen budget exceeds the 1 MiB input limit")
    budget = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(budget, dict) or budget.get("observer_budget_schema") != 1:
        raise ValueError("Unsupported observer budget")
    expected = {"collection_ms.p95", "collection_ms.max", "monitor_cpu_percent.mean", "monitor_cpu_percent.p95",
                "handle_classification_ms.p95", "handle_classification_ms.max",
                "post_collection_age_estimate_ms.p95", "post_collection_age_estimate_ms.max"}
    if not isinstance(budget.get("per_tree"), dict) or set(budget["per_tree"]) != {"1", "7", "31"}:
        raise ValueError("Observer budgets must cover all three tree sizes")
    for values in budget["per_tree"].values():
        if not isinstance(values, dict) or set(values) != expected:
            raise ValueError("Incomplete observer budget metrics")
        for value in values.values():
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError("Invalid observer budget limit")
    for key in ("rss_peak_bytes", "export_ms"):
        value = budget.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Invalid observer budget ceiling")
    if not isinstance(budget.get("source"), dict) or not isinstance(budget["source"].get("sha256"), dict):
        raise TypeError("Observer budget source identity is required")
    return budget


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("reference", "verify"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budgets", type=Path)
    parser.add_argument("--smoke", action="store_true", help="orchestration only, never performance evidence")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("refusing to overwrite an existing result")
    if args.mode == "verify" and args.budgets is None:
        parser.error("verification needs a frozen --budgets file")
    source = source_record()
    source["sha256"]["docs/observer-protocol.md"] = hashlib.sha256((ROOT / "docs/observer-protocol.md").read_bytes()).hexdigest()
    if source["tracked_changes"] and not args.smoke:
        parser.error("commit tracked changes before measuring observer cost")
    budget = load_budget(args.budgets) if args.budgets else None
    if budget is not None and (budget.get("observer_budget_schema") != 1 or budget.get("os") != platform.system()):
        parser.error("the frozen budget must match this platform and schema")
    if budget is not None:
        for path, digest in budget["source"]["sha256"].items():
            if (path.startswith("src/") or path in {"torture/observer_benchmark.py", "torture/process_tree.py"}) and source["sha256"].get(path) != digest:
                parser.error("runtime or observer harness differs from the frozen reference budget")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    record = {"observer_measurement_schema": 1, "mode": args.mode, "smoke": args.smoke, "state": "running",
              "source": source, "started_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"os": platform.system(), "release": platform.release(), "version": platform.version(),
                              "python": platform.python_version(), "psutil": version("psutil"), "rich": version("rich"),
                              "logical_cpus": psutil.cpu_count(),
                              "physical_memory_bytes": psutil.virtual_memory().total,
                              "available_memory_bytes_at_start": psutil.virtual_memory().available,
                              "host_cpu_percent_at_start": psutil.cpu_percent(interval=0.2),
                              "shared_host_with_calibration_and_development": True},
              "settings": {"interval_ms": 500, "windows_seconds": [10, 30, 60], "warmup_seconds": 0.2 if args.smoke else 3,
                           "native_seconds": 2 if args.smoke else 65, "repetitions": 1 if args.smoke else 3,
                           "soak_seconds": 2 if args.smoke else 1800}, "runs": []}
    save(args.output, record)
    try:
        shape = None
        for repeat in range(1, record["settings"]["repetitions"] + 1):
            for depth in (0, 2, 4):
                size = 2 ** (depth + 1) - 1
                print(f"START {args.mode} {repeat} tree-{size}", flush=True)
                record["active_case"] = {"phase": "native", "repeat": repeat, "tree_size": size}
                save(args.output, record)
                row, shape = native(depth, repeat, record["settings"]["native_seconds"],
                    args.output.parent / (args.output.stem + "-reports") / f"{repeat}-{size}")
                row["budget_breaches"] = breaches(row, budget) if budget else []
                record["runs"].append(row)
                save(args.output, record)
                if not row["cleanup_complete"]:
                    raise RuntimeError("Owned cleanup is incomplete; refusing another target")
                if row.get("interrupted") or row["exit_code"] == 130:
                    raise KeyboardInterrupt
                print(f"DONE {args.mode} {repeat} tree-{size}: {row['budget_breaches']}", flush=True)
                gc.collect()
        if args.mode == "reference":
            print("START synthetic retention/export stress", flush=True)
            record["active_case"] = {"phase": "synthetic_retention_export"}
            save(args.output, record)
            record["retention"] = retention(shape, args.output.parent / (args.output.stem + "-retention"), smoke=args.smoke)
            if not args.smoke:
                if record["retention"]["watcher_error"] or any(not row["quality_pass"] or row["watcher_error"] for row in record["runs"]):
                    raise RuntimeError("Reference quality failed; no budget can be derived")
                budget_path = args.output.with_suffix(".budgets.json")
                if budget_path.exists():
                    raise RuntimeError("Refusing to overwrite frozen budgets")
                save(budget_path, budgets(record))
        else:
            print("START native 31-process soak", flush=True)
            record["active_case"] = {"phase": "native_soak", "tree_size": 31}
            save(args.output, record)
            row, _shape = native(4, 1, record["settings"]["soak_seconds"],
                args.output.parent / (args.output.stem + "-reports") / "soak-31")
            row["budget_breaches"] = breaches(row, budget)
            record["soak"] = row
            if row.get("interrupted") or row["exit_code"] == 130:
                raise KeyboardInterrupt
        record["state"] = "complete"
        record["active_case"] = None
        if budget and any(row["budget_breaches"] for row in [*record["runs"], *([record["soak"]] if "soak" in record else [])]):
            record["state"] = "budget_failures"
    except KeyboardInterrupt:
        record["state"] = "interrupted"
    except Exception as error:  # noqa: BLE001 - preserve failed evidence, never selectively retry a measurement.
        record["state"] = "operational_error"
        record["error_type"] = type(error).__name__
        print(f"Observer measurement failed: {type(error).__name__}", flush=True)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(args.output, record)
    return 0 if record["state"] == "complete" else 130 if record["state"] == "interrupted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
