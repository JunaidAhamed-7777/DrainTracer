"""Explicit local privacy profiles for reports; unknown fields fail closed."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .report_contract import METRIC_UNITS, SEVERITIES, validate_report

PROFILES = ("share", "minimal")
_EPOCH = 946684800.0  # Fixed 2000-01-01 origin; source wall-clock dates are removed.
_CATEGORIES = {"file", "socket", "registry", "event", "thread", "process", "mutex", "semaphore", "section", "other"}
_RESOURCES = {"memory", "handles", "threads"}
_STATUS = {"running", "sleeping", "disk-sleep", "stopped", "tracing-stop", "zombie", "dead",
           "wake-kill", "waking", "idle", "locked", "waiting", "parked", "unknown"}
_EVIDENCE = {"warming_up", "collecting", "no_growth", "growth", "unavailable", "partial", "stale"}
_MISSING = object()


def _enum(*values: str) -> tuple:
    return ("enum", set(values))


def _mapping(keys, spec) -> tuple:
    return ("map", set(keys), spec)


# Each numeric field is permitted only at its documented structural location.
# Opaque dictionaries never inherit a global list of evidence-like key names.
_METRICS = _mapping(METRIC_UNITS, "num")
_TYPES = _mapping(_CATEGORIES, "count")
_COUNTS = {key: "count" for key in ("live", "started", "exited", "restarted")}
_PROCESS = {
    "pid": "pid", "ppid": "pid", "create_time": "identity", "name": "name",
    "status": ("enum", _STATUS), "access_denied": "bool", "unmeasured": [("enum", set(METRIC_UNITS))],
    "handle_types": _TYPES, **{metric: "num" for metric in METRIC_UNITS},
}
_PROCESS_SUMMARY = {
    "pid": "pid", "ppid": "pid", "create_time": "identity", "name": "name",
    "first_seen": "time", "last_seen": "time", "exited_at": "time", "samples": "count",
    **{key: _METRICS for key in ("first", "last", "peak", "growth")},
}
_CONTRIBUTOR = {"pid": "pid", "create_time": "identity", "name": "name", "delta": "num",
                "share": "num", "new_process": "bool", "exited": "bool"}
_ALERT = {
    "resource": ("enum", _RESOURCES), "metric": ("enum", set(METRIC_UNITS)),
    "severity": ("enum", set(SEVERITIES)), "message": "redact", "growth_by_type": _TYPES,
    "contributors": [_CONTRIBUTOR], "mostly_new_processes": "bool",
    **{key: "num" for key in ("slope_per_second", "window_seconds", "current_value", "baseline_value",
                              "growth_from_new_processes", "growth_within_existing")},
}
_COVERAGE_ITEM = {"measured": "count", "total": "count", "unmeasured_pids": ["pid"]}
_COVERAGE = _mapping(METRIC_UNITS, _COVERAGE_ITEM)
_EVIDENCE_ITEM = {"state": ("enum", _EVIDENCE), "severity": ("enum", set(SEVERITIES)),
                  "valid_samples": "count", "needed_samples": "count", "elapsed_seconds": "num",
                  "needed_seconds": "num", "evaluated_window_seconds": "num"}
_STATS = {"count": "count", "mean": "num", "max": "num"}
_DIAGNOSTICS = {key: "num" for key in ("requested_interval_ms", "actual_interval_ms", "collection_ms",
    "handle_types_age_seconds", "handle_types_cost_ms", "monitor_cpu_percent", "monitor_rss_bytes",
    "monitor_private_bytes", "failed_samples", "dropped_snapshots")}
_DIAGNOSTICS.update({"clock_adjustments": "count", "sampling_gaps": "count"})
_LIFECYCLE = {"kind": _enum("started", "exited", "restarted"), "timestamp": "time", "pid": "pid",
              "ppid": "pid", "create_time": "identity", "name": "name", "replaces_pid": "pid"}
_SNAPSHOT = {
    "timestamp": "time", "root_pid": "pid", "process_alive": "bool", "monitoring_paused": "bool",
    "processes": [_PROCESS], "totals": _METRICS, "alerts": [_ALERT], "severity": ("enum", set(SEVERITIES)),
    "sample_interval_ms": "num", "warming_up": "bool", "coverage": _COVERAGE,
    "evidence": _mapping(_RESOURCES, _EVIDENCE_ITEM), "handle_types": _TYPES,
    "metadata": {"sampling_error": "error", "timing_reset": _enum("clock_adjustment", "sampling_gap"),
                 "sample_gap_seconds": "num", "clock_adjustment_seconds": "num"},
    "lifecycle": [_LIFECYCLE], "process_counts": _COUNTS,
    "diagnostics": _DIAGNOSTICS,
}
_FACTS = {
    "schema": "count", "samples": {key: "count" for key in ("taken", "collected", "measured", "paused", "failed")},
    "first_sample_at": "time", "last_sample_at": "time",
    "peaks": _mapping(METRIC_UNITS, {"value": "num", "timestamp": "time"}),
    "signals": _mapping(_RESOURCES, {"worst": ("enum", set(SEVERITIES)), "worst_at": "time",
        "episodes": "count", "escalations": "count", "signal_seconds": "num", "active": "bool"}),
    "evaluation": {"after_warmup_seconds": "num", "evaluated_seconds": "num", "first_evaluated_at": "time"},
    "coverage": {"samples": "count", "partial_samples": _mapping(METRIC_UNITS, "count"),
                 "unavailable_samples": _mapping(METRIC_UNITS, "count")},
    "chart": {key: "count" for key in ("stride", "stored", "seen")},
    "monitor": {"requested_interval_ms": "num", "interval_ms": _STATS, "collection_ms": _STATS,
                "monitor_cpu_percent": _STATS, "handle_classification_ms": _STATS,
                **{key: "num" for key in ("monitor_rss_peak_bytes", "monitor_private_peak_bytes",
                   "handle_types_max_age_seconds", "failed_samples", "dropped_snapshots")},
                "clock_adjustments": "count", "sampling_gaps": "count"},
}
_SETTINGS = {"sample_interval_ms": "num", "windows_seconds": ["num"], "warmup_seconds": "num",
             "timeout_seconds": "num", "thresholds": _mapping(_RESOURCES,
                 {"critical_delta": "num", "critical_slope": "num"}),
             "fail_on": ("enum", set(SEVERITIES)), "fail_on_inconclusive": "bool",
             "fail_on_target_error": "bool", "require_full_coverage": "bool", "budgets": _METRICS}
_CLEANUP = {
    "state": _enum("not_attempted", "complete", "incomplete"), "ownership": _enum("attached", "launched"),
    "attempted_actions": {key: "count" for key in ("detach", "terminate", "kill")},
    "remaining_count": "count", "omitted_processes": "count", "resume_required": "bool", "errors": ["redact"],
    "remaining_processes": [{"pid": "pid", "create_time": "identity", "resume_required": "bool"}],
}
_ROOT = {
    "report_schema": "count", "root_pid": "pid", "mode": _enum("launch", "attach"),
    "target_exit_code": "num", "sample_count": "count", "snapshot_stride": "count",
    "session_started_at": "iso", "session_ended_at": "iso", "snapshots": [_SNAPSHOT],
    "settings": _SETTINGS, "session_facts": _FACTS, "detection_ran": "bool",
    "coverage": {"samples": "count", "partial": "bool", "full": "bool", "measured_samples": "count",
                 "fully_measured_samples": "count", "unreadable_pids": ["pid"], "latest": _COVERAGE,
                 "metrics": _mapping(METRIC_UNITS, {"partial_samples": "count", "unavailable_samples": "count"})},
    "outcome": {"target": {"state": _enum("running", "stopped", "exited", "unknown", "left_running"),
                            "exit_code": "num", "failed": "bool", "description": "redact"},
                "measurement": {"result": _enum("conclusive", "partial", "inconclusive"),
                                "detection_ran": "bool", "partial_coverage": "bool", "full_coverage": "bool",
                                "reason": "redact", "coverage": "redact"},
                "startup": {"state": _enum("not_started", "started", "failed"), "error": "redact"},
                "cleanup": _CLEANUP,
                "workload": {"state": _enum("not_started", "starting", "running", "complete", "failed",
                                             "interrupted", "monitor_error", "startup_failed")},
                "headless": {"require_full_coverage": "bool", "exit_code": "num", "decision_as_of_checkpoint": "num"},
                "monitor_errors": ["redact"]},
    "processes": {"counts": _COUNTS, "live": [_PROCESS_SUMMARY], "exited": [_PROCESS_SUMMARY]},
    "event_log": [{"timestamp": "time", "time": "iso", "level": _enum("ok", "info", "warn", "alert", "error"),
                   "message": "redact"}],
    "event_counts": {key: "count" for key in ("observed", "retained", "dropped")},
    "markers": [{"timestamp": "time", "kind": _enum("marker"), "index": "count", "label": "redact"}],
    "warmup": {"seconds": "num", "completed": "bool", "completed_at": "iso", "baseline": _METRICS},
    "experiment": {"schema": "count", "mode": _enum("launch", "attach"), "operations": "num",
        "duration_seconds": "num", "budgets": _METRICS,
        "sampling": {"interval_ms": "num", "windows_seconds": ["num"], "warmup_seconds": "num", "timeout_seconds": "num"},
        "environment": {"os": _enum("Windows", "Linux"), "private_memory_metric": _enum("private_bytes", "uss"),
                        "handle_metric": _enum("handles", "file_descriptors")}},
    "recovery": {"partial": "strict_bool", "final_outcome_confirmed": "strict_bool", "exact_through_cutoff": "strict_bool"},
}


def _iso_stamp(value: Any) -> float | None:
    try:
        parsed = datetime.fromisoformat(value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _children(value, spec):
    if isinstance(spec, dict) and isinstance(value, dict):
        yield from ((child, spec[key], value) for key, child in value.items() if key in spec)
    elif isinstance(spec, list) and isinstance(value, list):
        yield from ((child, spec[0], None) for child in value)
    elif isinstance(spec, tuple) and spec[0] == "map" and isinstance(value, dict):
        yield from ((child, spec[2], value) for key, child in value.items() if key in spec[1])


def redact_report(report: dict[str, Any], profile: str = "share") -> dict[str, Any]:
    """Copy explicitly typed evidence paths; opaque and unknown structures fail closed."""
    validate_report(report)
    if profile not in PROFILES:
        raise ValueError(f"unknown privacy profile; choose {', '.join(PROFILES)}")
    stamps = []
    pending = [(report, _ROOT)]
    while pending:
        value, spec = pending.pop()
        if spec == "time" and type(value) in (int, float):
            stamps.append(value)
        elif spec == "iso":
            stamp = _iso_stamp(value)
            if stamp is not None:
                stamps.append(stamp)
        pending.extend((child, child_spec) for child, child_spec, _parent in _children(value, spec))
    origin = min(stamps, default=0.0)
    pids: dict[int, int] = {}
    identities: dict[tuple[Any, Any], float] = {}

    def pid(value: Any) -> int | None:
        if type(value) is not int:
            return None
        if value not in pids:
            pids[value] = len(pids) + 1
        return pids[value]

    def shifted(value: Any) -> float | None:
        try:
            return _EPOCH + (value - origin) if type(value) in (int, float) else None
        except OverflowError:
            return None

    def transform(value, spec, parent=None):
        if value is None:
            return _MISSING if spec == "strict_bool" else None
        if isinstance(spec, dict) and isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key in spec:
                    copied = transform(child, spec[key], value)
                    if copied is not _MISSING:
                        result[key] = copied
            return result
        if isinstance(spec, list) and isinstance(value, list):
            children = [transform(child, spec[0]) for child in value]
            return [child for child in children if child is not _MISSING]
        if isinstance(spec, tuple):
            if spec[0] == "enum":
                return value if isinstance(value, str) and value in spec[1] else _MISSING
            if spec[0] == "map" and isinstance(value, dict):
                result = {}
                for key, child in value.items():
                    if key in spec[1]:
                        copied = transform(child, spec[2], value)
                        if copied is not _MISSING:
                            result[key] = copied
                return result
            return _MISSING
        if spec == "redact":
            return "[removed]"
        if spec == "error":
            return "[removed]" if value else None
        if spec == "name":
            return f"process-{pid(parent.get('pid'))}" if isinstance(parent, dict) else "process"
        if spec == "pid":
            return pid(value)
        if spec == "identity":
            if type(value) not in (int, float) or not isinstance(parent, dict):
                return _MISSING
            identity = (pid(parent.get("pid")), value)
            if identity not in identities:
                identities[identity] = float(len(identities) + 1)
            return identities[identity]
        if spec == "time":
            return shifted(value)
        if spec == "iso":
            try:
                return datetime.fromtimestamp(shifted(_iso_stamp(value)), timezone.utc).isoformat()
            except (TypeError, ValueError, OverflowError, OSError):
                return None
        if spec == "num" and type(value) in (int, float):
            return value
        if spec == "count" and type(value) is int and value >= 0:
            return value
        if spec in ("bool", "strict_bool") and type(value) is bool:
            return value
        return _MISSING

    redacted = transform(report, _ROOT)
    redacted.update({
        "report_id": str(uuid.uuid4()), "target_path": "shared-target", "app": "DrainTracer",
        "created_at": datetime.fromtimestamp(_EPOCH, timezone.utc).isoformat(),
        "version": report.get("version") if re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){1,2}",
                                                            str(report.get("version", ""))) else None,
        "privacy": {"privacy_schema": 1, "profile": profile,
                    "transformations": ["paths and free text removed", "process identities pseudonymized",
                                        "wall-clock dates replaced", "unknown fields and captured logs removed"],
                    "review_required": True},
    })
    for index, marker in enumerate(redacted.get("markers") or (), start=1):
        marker["index"] = index
    if profile == "minimal":
        redacted.pop("event_log", None)
        redacted.pop("markers", None)
        redacted.pop("processes", None)
        for sample in redacted["snapshots"]:
            for process in sample.get("processes", ()):
                for metric in METRIC_UNITS:
                    process.pop(metric, None)
                process["unmeasured"] = list(METRIC_UNITS)
            for alert in sample.get("alerts", ()):
                alert.pop("contributors", None)
    validate_report(redacted)
    return redacted


def export_private_report(report: dict[str, Any], directory: str | Path,
                          profile: str = "share") -> tuple[Path, Path]:
    """Write the same privacy-transformed dictionary to JSON and embedded HTML."""
    from .html_report import write_html_report
    from .report import write_json_report

    redacted = redact_report(report, profile)
    json_path = write_json_report(redacted, directory)
    html_path = write_html_report(redacted, directory)
    return json_path, html_path


def preview_json(report: dict[str, Any], profile: str = "share") -> str:
    """A local preview of exactly the data eligible for a shared export."""
    return json.dumps(redact_report(report, profile), indent=2, allow_nan=False)
