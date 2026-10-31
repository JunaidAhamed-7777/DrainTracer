"""Bounded disposable operating exercises, with measurements instead of SLA claims."""

from __future__ import annotations

import math
import platform
import statistics
import threading
import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class OperatingBudgets:
    readiness_seconds: float = 2.0
    freshness_seconds: float = 2.0
    upload_seconds: float = 2.0
    read_seconds: float = 2.0
    cleanup_seconds: float = 20.0
    sampled_rss_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        for name in ("readiness_seconds", "freshness_seconds", "upload_seconds", "read_seconds", "cleanup_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= 60 or not math.isfinite(value):
                raise ValueError("Operating duration budgets must be finite and between zero and 60 seconds")
        if type(self.sampled_rss_bytes) is not int or not 1 <= self.sampled_rss_bytes <= 4 * 1024**3:
            raise ValueError("Sampled memory budget must be between one byte and 4 GiB")


def _rate(successful: int, total: int) -> dict:
    return {"successful": successful, "total": total, "ratio": successful / total if total else None}


def _distribution(values: list[float]) -> dict:
    return {"samples": len(values), "minimum": min(values) if values else None,
            "median": statistics.median(values) if values else None, "maximum": max(values) if values else None}


class ExerciseEvidence:
    """Fixed safe observations: never copy exception text, return values or credentials."""

    def __init__(self, budgets: OperatingBudgets):
        self.budgets = budgets
        self.probes = []
        self.timings = {}
        self.rates = {}
        self.resources = {}

    def probe(self, name, action, *, expected_code=None, timing=None):
        if len(self.probes) >= 128:
            raise RuntimeError("Operating exercise exceeded its fixed probe bound")
        started = time.monotonic()
        result, code, passed = None, None, False
        try:
            result = action()
            passed = expected_code is None
        except Exception as error:  # noqa: BLE001 - preserve every failed fixture probe without private exception text
            # Only an explicitly expected machine code can count as a safe
            # refusal. Unexpected errors remain failures with no source text.
            code = getattr(error, "code", None)
            passed = expected_code is not None and code == expected_code
        elapsed = max(0.0, time.monotonic() - started)
        self.probes.append({"name": name, "kind": "expected_refusal" if expected_code else "baseline",
                            "passed": passed, "elapsed_seconds": elapsed})
        if timing is not None:
            self.timings.setdefault(timing, []).append(elapsed)
        return result if passed and expected_code is None else None

    def check(self, name, passed):
        self.probe(name, lambda: _require(passed))

    def rate(self, name, successful, total):
        if type(successful) is not int or type(total) is not int or not 0 <= successful <= total:
            raise ValueError("Operating counts must be ordered nonnegative integers")
        self.rates[name] = _rate(successful, total)

    def document(self):
        timings = {name: _distribution(values) for name, values in self.timings.items()}
        limits = {"readiness": self.budgets.readiness_seconds, "freshness": self.budgets.freshness_seconds,
                  "ingest": self.budgets.upload_seconds,
                  "upload": self.budgets.upload_seconds, "read": self.budgets.read_seconds,
                  "cleanup": self.budgets.cleanup_seconds}
        checks = {name: bool(timings.get(name, {}).get("samples"))
                  and timings[name]["maximum"] <= value for name, value in limits.items() if name in timings}
        for name, resource in self.resources.items():
            checks[f"{name}_sampled_rss"] = (resource["samples"] > 0
                                            and resource["sampled_peak_rss_bytes"] <= self.budgets.sampled_rss_bytes)
        return {"probes": list(self.probes), "timings_seconds": timings, "rates": dict(self.rates),
                "resources": dict(self.resources), "budget_checks": checks,
                "checks_passed": bool(self.probes) and all(probe["passed"] for probe in self.probes)
                and all(checks.values())}


def _require(condition):
    if not condition:
        raise AssertionError("Operating invariant failed")


class ResourceSampler:
    """At most two known processes, 20 ms samples and a finite two-minute life."""

    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._processes = {}
        self._measurements = {}
        self._thread = threading.Thread(target=self._run, name="operating-resource-samples", daemon=True)

    def add(self, name, pid):
        if name not in {"driver", "agent"}:
            raise ValueError("Unknown reference process")
        process = psutil.Process(pid)
        created = process.create_time()
        cpu = process.cpu_times()
        with self._lock:
            self._processes[name] = (process, created, cpu.user + cpu.system)
            self._measurements[name] = {"samples": 0, "sampled_peak_rss_bytes": 0, "cpu_seconds": 0.0}

    def _sample(self):
        with self._lock:
            for name, (process, created, initial_cpu) in self._processes.items():
                try:
                    if psutil.Process(process.pid).create_time() != created:
                        continue
                    cpu, rss = process.cpu_times(), process.memory_info().rss
                except (psutil.Error, OSError):
                    continue
                row = self._measurements[name]
                row["samples"] += 1
                row["sampled_peak_rss_bytes"] = max(row["sampled_peak_rss_bytes"], rss)
                row["cpu_seconds"] = max(row["cpu_seconds"], cpu.user + cpu.system - initial_cpu, 0.0)

    def _run(self):
        deadline = time.monotonic() + 120
        while not self._stop.is_set() and time.monotonic() < deadline:
            self._sample()
            self._stop.wait(0.02)

    def __enter__(self):
        try:
            self._thread.start()
        except BaseException:
            # Context-manager __exit__ is not called when __enter__ fails,
            # including an interrupted/partially successful Thread.start.
            self._stop.set()
            if self._thread.ident is not None:
                self._thread.join(2)
            raise
        return self

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self._sample()
        with self._lock:
            return {name: dict(value) for name, value in self._measurements.items()}

    def __exit__(self, *_exc):
        self.close()


def reference_environment():
    return {"platform": platform.system(), "python": platform.python_version(), "psutil": psutil.__version__,
            "budgets_are": "local acceptance checks", "memory_measurement": "sampled lower bound on peak RSS",
            "availability_measurement": "bounded synthetic probes, not long-term uptime",
            "participants": "technical credentials only; no human pilot or consent claim"}


__all__ = ["ExerciseEvidence", "OperatingBudgets", "ResourceSampler"]
