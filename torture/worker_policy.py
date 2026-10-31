"""Collect pre-registered repeated worker-growth attribution evidence."""

from __future__ import annotations

import argparse
import hashlib
import io
import platform
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import psutil
from calibrate import ROOT, Case, MeasuredRunner, save, source_record, summarize_run
from observer_stress import cleanup_verified
from rich.console import Console

from src.config import MonitorSettings
from src.events import LeakResource
from src.report_contract import read_report

CASES = (
    Case("accumulate-workers", "worker_capacity.py", ("accumulate",), "new worker capacity", "memory"),
    Case("within-worker", "memory_leak.py", ("--chunk-mb", "12", "--interval", "2", "--max-mb", "96"),
         "growth inside one process", "memory"),
    Case("churn-workers", "worker_capacity.py", ("churn",), "bounded worker churn"),
)


def run_case(case, repeat, directory, *, smoke=False):
    duration = 2 if smoke else 75
    session = MeasuredRunner(getattr(sys, "_base_executable", None) or sys.executable,
        args=[str(ROOT / "torture" / case.fixture), *case.arguments, "--duration", "90"],
        console=Console(file=io.StringIO()), report_dir=directory, timeout=duration,
        settings=MonitorSettings(sample_interval_ms=500, windows=(1,) if smoke else (10, 30, 60),
                                 warmup=0 if smoke else 3, timeout=duration))
    try:
        code = session.run()
    finally:
        if session.manager.has_tracked_processes:
            session.manager.kill()
    identity_count = cleanup_verified(session)
    row = summarize_run(session, case, repeat, code)
    row["interrupted"] = session._interrupted
    row["observed_identity_count"] = identity_count
    saved = read_report(session._last_report)
    row["public_reader_roundtrip"] = True
    row["quality_pass"] = (row["cleanup_complete"] and row["report_pair_written"] and not row["operational_error"]
        and not row["partial_coverage"] and not row["monitor_error_count"] and not row["samples"]["failed"]
        and not row["monitor"]["dropped_snapshots"] and not row["monitor"]["clock_adjustments"]
        and not row["monitor"]["sampling_gaps"] and saved["outcome"]["cleanup"]["state"] == "complete")
    alerts = []
    for snapshot in session._snapshot_history:
        for alert in snapshot.alerts:
            if alert.resource != LeakResource.MEMORY:
                continue
            total = alert.growth_from_new_processes + alert.growth_within_existing
            alerts.append({"elapsed_seconds": round(snapshot.timestamp - session._started_at, 4),
                "severity": alert.severity.value, "window_seconds": alert.window_seconds,
                "growth_from_new_processes_bytes": alert.growth_from_new_processes,
                "growth_within_existing_bytes": alert.growth_within_existing,
                "fraction_of_positive_growth_from_new_processes": alert.growth_from_new_processes / total if total else None,
                "mostly_new_processes": alert.mostly_new_processes,
                "retained_contributors": len(alert.contributors),
                "new_contributors": sum(item.new_process for item in alert.contributors),
                "existing_contributors": sum(not item.new_process for item in alert.contributors)})
    row["memory_attribution_alerts"] = alerts
    if case.name == "accumulate-workers":
        row["expected_attribution_observed"] = any(alert["mostly_new_processes"] for alert in alerts)
    elif case.name == "within-worker":
        row["expected_attribution_observed"] = any(not alert["mostly_new_processes"]
            and alert["growth_within_existing_bytes"] > 0 for alert in alerts)
    else:
        row["expected_attribution_observed"] = None
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true", help="orchestration only, not policy evidence")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("refusing to overwrite an existing worker-growth record")
    source = source_record()
    source["sha256"]["docs/worker-growth-protocol.md"] = hashlib.sha256((ROOT / "docs/worker-growth-protocol.md").read_bytes()).hexdigest()
    if source["tracked_changes"] and not args.smoke:
        parser.error("commit tracked changes before collecting worker policy evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    record = {"worker_policy_schema": 1, "kind": "harness_smoke" if args.smoke else "repeated_worker_growth_reference",
        "state": "running", "source": source, "started_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"os": platform.system(), "release": platform.release(), "version": platform.version(),
            "python": platform.python_version(), "psutil": version("psutil"), "rich": version("rich"),
            "logical_cpus": psutil.cpu_count(), "physical_memory_bytes": psutil.virtual_memory().total,
            "available_memory_bytes_at_start": psutil.virtual_memory().available,
            "host_cpu_percent_at_start": psutil.cpu_percent(interval=0.2),
            "shared_host_with_observer_validation_and_development": True},
        "settings": {"repetitions": 1 if args.smoke else 3, "duration_seconds": 2 if args.smoke else 75,
            "warmup_seconds": 0 if args.smoke else 3, "interval_ms": 500,
            "windows_seconds": [1] if args.smoke else [10, 30, 60], "failure_policy": "high"}, "runs": []}
    save(args.output, record)
    try:
        for repeat in range(1, record["settings"]["repetitions"] + 1):
            for case in CASES:
                record["active_case"] = {"scenario": case.name, "repeat": repeat}
                save(args.output, record)
                print(f"START {repeat} {case.name}", flush=True)
                row = run_case(case, repeat, args.output.parent / (args.output.stem + "-reports") / f"{repeat}-{case.name}", smoke=args.smoke)
                record["runs"].append(row)
                save(args.output, record)
                print(f"DONE {repeat} {case.name}: {row['signals']['memory']['worst']}", flush=True)
                if row["interrupted"]:
                    raise KeyboardInterrupt
                if not row["cleanup_complete"]:
                    raise RuntimeError("Sequence stopped after incomplete cleanup")
        record["state"] = "complete"
        record["active_case"] = None
        if not args.smoke and any(not row["quality_pass"] or row["expected_attribution_observed"] is False for row in record["runs"]):
            record["state"] = "evidence_mismatch"
    except KeyboardInterrupt:
        record["state"] = "interrupted"
    except Exception as error:  # noqa: BLE001 - preserve operational failures and stop the sequence.
        record["state"] = "operational_error"
        record["error_type"] = type(error).__name__
        print(f"FAILED {record['active_case']}: {type(error).__name__}", flush=True)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(args.output, record)
    return 0 if record["state"] == "complete" else 130 if record["state"] == "interrupted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
