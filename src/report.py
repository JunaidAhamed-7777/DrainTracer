"""Leak report identifiers, serialization, and storage."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import MetricsSnapshot

DEFAULT_REPORT_DIR = "leak_reports"
REPORT_SCHEMA = 2


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


def snapshot_to_dict(snapshot: MetricsSnapshot) -> dict[str, Any]:
    """Convert a snapshot to JSON-safe primitives."""
    data = asdict(snapshot)
    data["alerts"] = [
        {
            "resource": alert.resource.value,
            "severity": alert.severity.value,
            "message": alert.message,
            "slope_per_second": alert.slope_per_second,
            "window_seconds": alert.window_seconds,
            "current_value": alert.current_value,
            "baseline_value": alert.baseline_value,
        }
        for alert in snapshot.alerts
    ]
    data["severity"] = snapshot.severity.value
    return data


def write_text_atomic(path: Path, content: str, newline: str | None = None) -> Path:
    """Write via a temporary sibling so readers never see a partial file.

    ``newline`` is passed to ``open``; ``"\\n"`` writes the text exactly
    as given instead of translating line endings for the platform.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline=newline) as handle:
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
    """Store ``report`` as ``<directory>/leak_report_<report_id>.json``."""
    report_id = report.get("report_id")
    if not report_id:
        raise ValueError("report has no report_id")
    path = Path(directory) / report_filename(str(report_id))
    return write_text_atomic(path, json.dumps(report, indent=2))


__all__ = [
    "DEFAULT_REPORT_DIR",
    "REPORT_SCHEMA",
    "iso_timestamp",
    "new_report_id",
    "report_filename",
    "snapshot_to_dict",
    "write_json_report",
    "write_text_atomic",
]
