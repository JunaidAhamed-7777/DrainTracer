"""Leak report identifiers, serialization, and storage."""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from . import report_contract
from .events import LifecycleEvent, MetricCoverage, MetricsSnapshot, ProcessSummary

DEFAULT_REPORT_DIR = "leak_reports/reports"
# 3: snapshots add per-metric ``coverage`` and per-resource ``evidence``,
# processes list their ``unmeasured`` counters, and reports add ``coverage``
# and ``outcome``. All are optional for readers; older reports lack them.
REPORT_SCHEMA = 3
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"
}


def new_report_id() -> str:
    """Return a random UUID4 string used for report identity and filenames."""
    return str(uuid.uuid4())


def iso_timestamp(epoch: float | None = None) -> str:
    """Format an epoch (or now) as ISO 8601 local time with its UTC offset."""
    moment = (
        datetime.now(timezone.utc)
        if epoch is None
        else datetime.fromtimestamp(epoch, timezone.utc)
    )
    return moment.astimezone().isoformat(timespec="milliseconds")


def report_filename(report_id: str, suffix: str = ".json") -> str:
    return f"leak_report_{report_id}{suffix}"


def target_filename(target: str) -> str:
    """Read a recorded absolute Windows or Linux path on either platform."""
    parser = PurePosixPath if target.startswith("/") else PureWindowsPath
    return parser(target).name


def report_directory(report: Mapping[str, Any], root: str | Path) -> Path:
    """Group exports by the actual target filename and recorded local date."""
    target = report.get("target_path")
    created = report.get("created_at")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("report has no target_path for its output directory")
    try:
        moment = datetime.fromisoformat(created) if isinstance(created, str) else None
    except ValueError as exc:
        raise ValueError("report created_at is not a valid ISO 8601 timestamp") from exc
    if moment is None or moment.tzinfo is None:
        raise ValueError("report created_at must include a UTC offset")
    filename = target_filename(target)
    safe = "".join(
        character if character.isprintable() and character not in '<>:"/\\|?*' else "_"
        for character in filename
    ).strip(" .")
    if safe.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICES:
        safe = "_" + safe
    safe = safe[:100].rstrip(" .")
    if not safe:
        raise ValueError("report target_path has no usable filename")
    return Path(root) / f"{safe}_{moment.date().isoformat()}"


def snapshot_to_dict(snapshot: MetricsSnapshot) -> dict[str, Any]:
    """Convert a snapshot to JSON-safe primitives.

    Per-process summaries are left out: they repeat on every sample, and
    reports carry them once at the top level instead.
    """
    data = asdict(replace(snapshot, process_summaries=(), exited_processes=()))
    del data["process_summaries"], data["exited_processes"]
    data["lifecycle"] = [lifecycle_to_dict(event) for event in snapshot.lifecycle]
    data["alerts"] = [
        {
            "resource": alert.resource.value,
            "metric": alert.metric,
            "severity": alert.severity.value,
            "message": alert.message,
            "slope_per_second": alert.slope_per_second,
            "window_seconds": alert.window_seconds,
            "current_value": alert.current_value,
            "baseline_value": alert.baseline_value,
            "growth_by_type": alert.growth_by_type,
            "contributors": [
                {
                    "pid": item.pid,
                    "create_time": item.create_time,
                    "name": item.name,
                    "delta": item.delta,
                    "share": round(item.share, 4),
                    "new_process": item.new_process,
                    "exited": item.exited,
                }
                for item in alert.contributors
            ],
            "growth_from_new_processes": alert.growth_from_new_processes,
            "growth_within_existing": alert.growth_within_existing,
            "mostly_new_processes": alert.mostly_new_processes,
        }
        for alert in snapshot.alerts
    ]
    data["severity"] = snapshot.severity.value
    data["coverage"] = coverage_to_dict(snapshot.coverage)
    # Keyed by resource and without the coverage it repeats, so the
    # evidence costs little in long reports.
    data["evidence"] = {
        item.resource.value: {
            "state": item.state.value,
            "severity": item.severity.value,
            "valid_samples": item.valid_samples,
            "elapsed_seconds": round(item.elapsed_seconds, 3),
            "evaluated_window_seconds": item.evaluated_window_seconds,
        }
        for item in snapshot.evidence
    }
    return data


def lifecycle_to_dict(event: LifecycleEvent) -> dict[str, Any]:
    """A process start, exit or restart as JSON primitives."""
    return {
        "kind": event.kind.value,
        "timestamp": event.timestamp,
        "pid": event.pid,
        "name": event.name,
        "create_time": event.create_time,
        "ppid": event.ppid,
        "replaces_pid": event.replaces_pid,
    }


def process_summary_to_dict(summary: ProcessSummary) -> dict[str, Any]:
    """One process identity's lifetime and counter summary as JSON primitives."""
    return {
        "pid": summary.pid,
        "create_time": summary.create_time,
        "name": summary.name,
        "ppid": summary.ppid,
        "first_seen": summary.first_seen,
        "last_seen": summary.last_seen,
        "exited_at": summary.exited_at,
        "samples": summary.samples,
        "first": dict(summary.first),
        "last": dict(summary.last),
        "peak": dict(summary.peak),
        "growth": {metric: summary.growth(metric) for metric in summary.last},
    }


def coverage_to_dict(coverage: Mapping[str, MetricCoverage] | None) -> dict[str, Any] | None:
    """Per-metric coverage as JSON primitives, or ``None`` when not recorded."""
    if coverage is None:
        return None
    return {
        metric: {
            "measured": item.measured,
            "total": item.total,
            "unmeasured_pids": list(item.unmeasured_pids),
        }
        for metric, item in coverage.items()
    }


def write_text_atomic(
    path: Path, content: str, newline: str | None = None, errors: str = "strict"
) -> Path:
    """Write via a temporary sibling so readers never see a partial file.

    ``newline`` is passed to ``open``; ``"\\n"`` writes the text exactly
    as given instead of translating line endings for the platform.
    ``errors`` decides what happens to text UTF-8 cannot hold, such as the
    unpaired surrogates Windows allows in file and process names.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline=newline, errors=errors) as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        # Do not leave a half-written temporary behind on failure.
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path


def _within_contract_tokens(value: Any) -> bool:
    pending = [(value, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if (count > report_contract.MAX_REPORT_VALUES or depth > report_contract.MAX_REPORT_DEPTH
                or isinstance(value, (dict, list, tuple)) and depth >= report_contract.MAX_REPORT_DEPTH):
            return False
        if isinstance(value, dict):
            count += len(value)  # JSON object keys are tokens too.
            children = value.values()
        elif isinstance(value, (list, tuple)):
            children = value
        else:
            continue
        if count + len(pending) + len(value) > report_contract.MAX_REPORT_VALUES:
            return False
        pending.extend((child, depth + 1) for child in children)
    return True


def _within_contract_bytes(value: dict[str, Any]) -> bool:
    encoder = json.JSONEncoder(indent=2, allow_nan=False, default=str)
    size = 0
    for part in encoder.iterencode(value):
        # ensure_ascii is true, and the writer translates LF on Windows.
        size += len(part) + (part.count("\n") if os.name == "nt" else 0)
        if size > report_contract.MAX_REPORT_BYTES:
            return False
    return True


def prepare_report_export(report: dict[str, Any]) -> dict[str, Any]:
    """Bound chart detail to the public reader while retaining exact session facts.

    The fresh export dictionary is copied; the live history is not changed.
    Both JSON and HTML must consume the returned dictionary.
    """
    result = dict(report)
    samples = list(result.get("snapshots") or ())
    result["snapshots"] = samples
    stride = int(result.get("snapshot_stride") or 1)
    while True:
        try:
            fits = _within_contract_tokens(result) and _within_contract_bytes(result)
        except ValueError:
            # Match the JSON writer's established non-finite normalization.
            result = _finite_only(result)
            fits = _within_contract_tokens(result) and _within_contract_bytes(result)
        if fits:
            return result
        facts = result.get("session_facts")
        if (len(samples) <= 2 or not isinstance(facts, dict)
                or type(facts.get("schema")) is not int or facts["schema"] != 1):
            raise ValueError("Report exceeds machine-contract export limits; non-chart evidence cannot be omitted")
        selected = samples[::2]
        if len(samples) % 2 == 0:
            selected.append(samples[-1])
        samples = selected
        stride *= 2
        result["snapshots"] = samples
        result["snapshot_stride"] = stride
        facts = dict(facts)
        facts["chart"] = dict(facts.get("chart") or {}, stride=stride, stored=len(samples))
        result["session_facts"] = facts


def write_json_report(report: dict[str, Any], directory: str | Path) -> Path:
    """Store ``report`` as ``<directory>/leak_report_<report_id>.json``.

    The file is always strict JSON: non-finite numbers become ``null`` and
    any unexpected object is written as its string form.
    """
    report_id = report.get("report_id")
    if not report_id:
        raise ValueError("report has no report_id")
    path = Path(directory) / report_filename(str(report_id))
    try:
        text = json.dumps(report, indent=2, allow_nan=False, default=str)
    except ValueError:
        text = json.dumps(_finite_only(report), indent=2, allow_nan=False, default=str)
    return write_text_atomic(path, text)


def _finite_only(value: Any) -> Any:
    """Copy ``value`` with NaN and infinities replaced by ``None``."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite_only(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_only(item) for item in value]
    return value


__all__ = [
    "DEFAULT_REPORT_DIR",
    "REPORT_SCHEMA",
    "coverage_to_dict",
    "iso_timestamp",
    "lifecycle_to_dict",
    "new_report_id",
    "prepare_report_export",
    "process_summary_to_dict",
    "report_directory",
    "report_filename",
    "snapshot_to_dict",
    "write_json_report",
    "write_text_atomic",
]
