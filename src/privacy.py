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
_PID_KEYS = {"pid", "ppid", "root_pid", "replaces_pid"}
_PID_LISTS = {"unmeasured_pids", "unreadable_pids"}
_TIMES = {"timestamp", "worst_at", "first_seen", "last_seen", "exited_at", "first_sample_at",
          "last_sample_at", "first_evaluated_at"}
_ISO_TIMES = {"created_at", "session_started_at", "session_ended_at", "time"}
_FREE_TEXT = {"message", "label", "description", "reason", "sampling_error", "monitor_errors"}
_CATEGORIES = {"file", "socket", "registry", "event", "thread", "process", "section", "other"}
_SAFE_TEXT = {*METRIC_UNITS, *SEVERITIES, *_CATEGORIES, "DrainTracer", "memory", "launch", "attach",
              "running", "sleeping", "disk-sleep", "stopped", "tracing-stop", "zombie", "dead",
              "wake-kill", "waking", "idle", "locked", "waiting", "parked", "unknown",
              "collecting", "no_growth", "growth", "unavailable", "partial", "conclusive", "inconclusive",
              "started", "exited", "restarted", "marker", "none", "info", "ok", "warn", "alert", "error",
              "left_running", "Windows", "Linux", "uss", "file_descriptors", "private_bytes"}
_KEYS = {
    "schema", "report_schema", "sample_count", "snapshot_stride", "snapshots", "processes", "totals",
    "alerts", "severity", "resource", "memory", "metric", "message", "slope_per_second", "window_seconds",
    "current_value", "baseline_value", "growth_by_type", "contributors", "delta", "share", "new_process",
    "growth_from_new_processes", "growth_within_existing", "mostly_new_processes", "name", "status",
    "create_time", "access_denied", "unmeasured", "process_alive", "monitoring_paused", "warming_up",
    "sample_interval_ms", "metadata", "sampling_error", "coverage", "measured", "total", "evidence",
    "valid_samples", "needed_samples", "elapsed_seconds", "needed_seconds", "evaluated_window_seconds",
    "state", "lifecycle", "process_counts", "live", "started", "exited", "restarted", "counts",
    "first", "last", "peak", "growth", "diagnostics", "handle_types", "handle_types_at",
    "handle_types_by_process", "session_facts", "samples", "taken", "collected", "paused", "failed",
    "peaks", "value", "signals", "worst", "episodes", "escalations", "signal_seconds", "active",
    "evaluation", "after_warmup_seconds", "evaluated_seconds", "monitor", "requested_interval_ms",
    "actual_interval_ms", "interval_ms", "collection_ms", "monitor_cpu_percent", "monitor_rss_bytes",
    "monitor_private_bytes", "monitor_rss_peak_bytes", "monitor_private_peak_bytes", "handle_types_age_seconds",
    "handle_types_cost_ms", "handle_classification_ms", "handle_types_max_age_seconds", "failed_samples",
    "dropped_snapshots", "mean", "max", "min", "count", "chart", "stride", "stored", "seen", "metrics",
    "partial_samples", "unavailable_samples", "partial", "latest", "outcome", "target", "measurement",
    "exit_code", "target_exit_code", "description", "result", "detection_ran", "partial_coverage",
    "reason", "monitor_errors", "settings", "windows_seconds", "warmup_seconds", "timeout_seconds",
    "thresholds", "critical_delta", "critical_slope", "fail_on", "fail_on_inconclusive", "fail_on_target_error",
    "budgets", "experiment", "environment", "os", "private_memory_metric", "handle_metric", "operations",
    "warmup", "seconds", "completed", "baseline", "markers", "index", "label", "kind", "event_log", "level",
    *_PID_KEYS, *_PID_LISTS, *_TIMES, *_ISO_TIMES, *METRIC_UNITS, *_CATEGORIES,
}


def redact_report(report: dict[str, Any], profile: str = "share") -> dict[str, Any]:
    """Return an allowlisted copy with pseudonymous identities and relative timing.

    ``minimal`` additionally removes event/marker detail and process attribution.
    Neither profile copies unknown keys, free text, paths, argument fingerprints,
    config origins, machine identifiers, or captured logs.
    """
    validate_report(report)
    if profile not in PROFILES:
        raise ValueError(f"unknown privacy profile; choose {', '.join(PROFILES)}")
    stamps = [sample["timestamp"] for sample in report["snapshots"]]
    events = report.get("event_log")
    stamps += [event["timestamp"] for event in (events if isinstance(events, list) else ())
               if isinstance(event, dict) and type(event.get("timestamp")) in (int, float)]
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
        return _EPOCH + value - origin if type(value) in (int, float) else None

    def copy(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            result = {}
            source_pid = value.get("pid")
            for child_key, child in value.items():
                if child_key not in _KEYS:
                    continue
                if child_key in _PID_KEYS:
                    result[child_key] = pid(child)
                elif child_key in _PID_LISTS:
                    result[child_key] = [pid(item) for item in child] if isinstance(child, list) else []
                elif child_key in _TIMES or child_key == "handle_types_at":
                    result[child_key] = shifted(child)
                elif child_key == "create_time":
                    identity = (pid(source_pid), child) if type(child) in (int, float) else None
                    if identity is None:
                        result[child_key] = None
                    else:
                        if identity not in identities:
                            identities[identity] = float(len(identities) + 1)
                        result[child_key] = identities[identity]
                elif child_key in _ISO_TIMES:
                    try:
                        parsed = datetime.fromisoformat(child)
                        stamp = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
                        result[child_key] = datetime.fromtimestamp(shifted(stamp), timezone.utc).isoformat()
                    except (TypeError, ValueError, OverflowError, OSError):
                        result[child_key] = None
                elif child_key == "name":
                    result[child_key] = f"process-{pid(source_pid)}" if source_pid is not None else "process"
                elif child_key == "handle_types_by_process":
                    result[child_key] = ({str(pid(int(raw_pid))): copy(counts)
                                          for raw_pid, counts in child.items() if raw_pid.isdecimal()}
                                         if isinstance(child, dict) else {})
                else:
                    result[child_key] = copy(child, child_key)
            return result
        if isinstance(value, list):
            return [copy(item, key) for item in value]
        if isinstance(value, str):
            return value if key not in _FREE_TEXT and value in _SAFE_TEXT else "[removed]"
        return value

    redacted = copy(report)
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
