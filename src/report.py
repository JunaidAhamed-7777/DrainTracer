"""Leak report identifiers, serialization, and storage."""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from .events import MetricCoverage, MetricsSnapshot

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
    filename = PureWindowsPath(target).name
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
    """Convert a snapshot to JSON-safe primitives."""
    data = asdict(snapshot)
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
    "new_report_id",
    "report_directory",
    "report_filename",
    "snapshot_to_dict",
    "write_json_report",
    "write_text_atomic",
]
