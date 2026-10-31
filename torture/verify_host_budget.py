"""Preregistered component-only host refresh/cache measurements; no targets."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.leak_detector import LeakDetector
from src.process_runner import ProcessManager

SAMPLES = 200
REPLICATES = 3
MARGINS = {"refresh.p99": (2.0, 1.0), "refresh.max": (2.0, 2.0),
           "cached.p99": (2.0, .1), "cached.max": (2.0, .5)}


def distribution(values):
    if (len(values) != SAMPLES
            or any(type(value) not in (int, float) or not 0 <= value < 60000 or not math.isfinite(value)
                   for value in values)):
        raise ValueError("Incomplete or invalid timing population")
    ordered = sorted(values)
    return {"samples": len(values), "minimum": ordered[0], "median": statistics.median(values),
            "p99": ordered[math.ceil(len(values) * .99) - 1], "max": ordered[-1]}


def ceilings(summaries):
    if len(summaries) != REPLICATES:
        raise ValueError("Three reference replicates are required")
    result = {}
    for key, (margin, floor) in MARGINS.items():
        path, statistic = key.split(".")
        values = [row[path][statistic] for row in summaries]
        if any(type(value) not in (int, float) or not 0 <= value < 60000 or not math.isfinite(value)
               for value in values):
            raise ValueError("Invalid reference statistic")
        result[key] = max(floor, margin * max(values))
    return result


def breaches(summary, limits):
    return [{"metric": key, "actual_ms": summary[key.split(".")[0]][key.split(".")[1]],
             "ceiling_ms": limit}
            for key, limit in limits.items()
            if summary[key.split(".")[0]][key.split(".")[1]] > limit]


def source_record():
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()

    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Commit tracked changes before measuring")
    required = json.loads((ROOT / "runtime-manifest.json").read_bytes())["required_files"]
    paths = sorted(set(required) | {"torture/benchmark_host.py", "torture/verify_host_budget.py",
                                    "docs/host-budget-protocol.md"})
    return {"commit": git("rev-parse", "HEAD"),
            "sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}}


def environment():
    from importlib.metadata import version

    memory = psutil.virtual_memory()
    return {"os": platform.system(), "release": platform.release(), "kernel_version": platform.version(),
            "python": platform.python_version(), "psutil": psutil.__version__, "rich": version("rich"),
            "logical_cpus": psutil.cpu_count(), "physical_memory_bytes": memory.total,
            "available_memory_bytes_at_start": memory.available,
            "host_cpu_percent_at_start": psutil.cpu_percent(interval=.2),
            "shared_developer_machine": True}


def signature(env):
    return {key: env[key] for key in ("os", "release", "kernel_version", "python", "psutil", "rich",
                                     "logical_cpus", "physical_memory_bytes")}


def measure(replicate):
    detector = LeakDetector(ProcessManager(None))
    values = {"refresh": [], "cached": []}
    quality = {"valid_memory_refreshes": 0, "known_cpu_after_priming": 0, "matching_cached_calls": 0}
    for index in range(SAMPLES):
        detector._host_sampled_at = None
        started = time.perf_counter_ns()
        fresh = detector._sample_host()
        values["refresh"].append((time.perf_counter_ns() - started) / 1_000_000)
        started = time.perf_counter_ns()
        cached = detector._sample_host()
        values["cached"].append((time.perf_counter_ns() - started) / 1_000_000)
        if fresh is None or cached is None:
            raise RuntimeError("Host observations unavailable")
        quality["valid_memory_refreshes"] += (fresh.available_memory_bytes is not None
                                               and fresh.total_memory_bytes is not None)
        quality["known_cpu_after_priming"] += index > 0 and fresh.cpu_percent is not None
        fields = ("sampled_at", "cpu_percent", "available_memory_bytes", "total_memory_bytes", "collection_ms")
        quality["matching_cached_calls"] += all(getattr(fresh, key) == getattr(cached, key) for key in fields)
    summary = {key: distribution(value) for key, value in values.items()}
    passed = (quality["valid_memory_refreshes"] == SAMPLES
              and quality["known_cpu_after_priming"] == SAMPLES - 1
              and quality["matching_cached_calls"] == SAMPLES)
    return {"replicate": replicate, "summary": summary, "timings_ms": values,
            "quality": quality, "quality_pass": passed}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".host-budget-", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, allow_nan=False, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_budget(path, record):
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("Budget exceeds its input bound")
    budget = json.loads(raw)
    if (budget.get("host_budget_schema") != 1 or budget.get("source") != record["source"]
            or budget.get("environment_signature") != signature(record["environment"])
            or budget.get("samples_per_replicate") != SAMPLES or budget.get("replicates") != REPLICATES
            or budget.get("interval_seconds") != LeakDetector.HOST_INTERVAL_SECONDS
            or budget.get("ceilings_ms") != ceilings(budget["reference_summaries"])):
        raise ValueError("Frozen budget does not match source, environment or derivation")
    return budget


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("reference", "verify"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budgets", type=Path)
    args = parser.parse_args(argv)
    budget_path = args.output.with_suffix(".budgets.json")
    if args.output.exists() or args.mode == "reference" and budget_path.exists():
        parser.error("Refusing to overwrite prior evidence")
    if (args.mode == "verify") != (args.budgets is not None):
        parser.error("Only verification requires --budgets")
    record = {"host_measurement_schema": 1, "mode": args.mode, "state": "preparing", "runs": [],
              "started_at": datetime.now(timezone.utc).isoformat(),
              "samples_per_replicate": SAMPLES, "replicates": REPLICATES,
              "interval_seconds": LeakDetector.HOST_INTERVAL_SECONDS}
    try:
        record.update(source=source_record(), environment=environment())
        budget = load_budget(args.budgets, record) if args.budgets else None
        if budget is not None:
            record["budget_sha256"] = hashlib.sha256(args.budgets.read_bytes()).hexdigest()
            record["ceilings_ms"] = budget["ceilings_ms"]
        save(args.output, record)
        for replicate in range(1, REPLICATES + 1):
            record.update(state="running", active_replicate=replicate)
            save(args.output, record)
            row = measure(replicate)
            row["breaches"] = breaches(row["summary"], budget["ceilings_ms"]) if budget else []
            record["runs"].append(row)
            save(args.output, record)
            if not row["quality_pass"]:
                raise RuntimeError("Host observation quality failed")
        record.pop("active_replicate", None)
        record["state"] = "budget_failures" if any(row["breaches"] for row in record["runs"]) else "complete"
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(args.output, record)
        if args.mode == "reference":
            budget = {"host_budget_schema": 1, "source": record["source"],
                      "environment_signature": signature(record["environment"]),
                      "samples_per_replicate": SAMPLES, "replicates": REPLICATES,
                      "interval_seconds": LeakDetector.HOST_INTERVAL_SECONDS,
                      "reference_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                      "reference_summaries": [row["summary"] for row in record["runs"]]}
            budget["ceilings_ms"] = ceilings(budget["reference_summaries"])
            save(budget_path, budget)
        print(json.dumps({"state": record["state"], "replicates": len(record["runs"])}))
        return 0 if record["state"] == "complete" else 1
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - retain failure without private exception text
        record.update(state="failed", error_type=type(error).__name__, finished_at=datetime.now(timezone.utc).isoformat())
        save(args.output, record)
        print(json.dumps({"state": "failed", "error_type": type(error).__name__}))
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
