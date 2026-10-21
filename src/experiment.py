"""Experiment provenance for reports: what ran, with which workload, where.

Comparing two reports only supports "my fix helped" when the runs were
equivalent experiments. This module builds the ``experiment`` section of a
report: optional labels the user supplies (workload ID, application build,
operation count, a note), the sampling settings and duration, and the
environment the run measured.

Reports are often attached to tickets or CI artifacts, so the record is
redacted by default:

* target arguments are never stored; only their count and a short
  SHA-256 fingerprint, which lets a comparison notice different arguments
  without revealing them (a fingerprint of a short, guessable argument can
  still be confirmed by hashing guesses, so it is not encryption);
* environment variables are never read or stored;
* the host name and user name are left out;
* free-text labels have ``password=...``-style values and bearer tokens
  replaced with ``<redacted>``.
"""

from __future__ import annotations

import functools
import hashlib
import math
import os
import platform
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import psutil

# Bumped when the shape of ``experiment`` changes incompatibly.
EXPERIMENT_SCHEMA = 1
REDACTED = "<redacted>"
# Free-text labels are short identifiers, not documents.
LABEL_LIMIT = 200
FINGERPRINT_LENGTH = 16

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b((?:[\w.-]*[_.-])?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"auth|credential)s?)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}")
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")


@dataclass(frozen=True)
class ExperimentInfo:
    """User-supplied labels that identify an experiment; all optional."""

    workload_id: str | None = None
    build: str | None = None
    operations: float | None = None
    note: str | None = None

    @property
    def empty(self) -> bool:
        return (
            self.workload_id is None
            and self.build is None
            and self.operations is None
            and self.note is None
        )


def redact_text(value: str) -> str:
    """Replace secret-looking values in a free-text label with ``<redacted>``."""
    text = _URL_CREDENTIALS.sub(lambda match: f"{match.group(1)}{REDACTED}@", value)
    text = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text
    )
    text = _BEARER.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    return text


def clean_label(value: Any) -> str | None:
    """A trimmed, redacted, length-limited label, or ``None`` when blank."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return redact_text(text)[:LABEL_LIMIT]


def arguments_record(args: Sequence[str] | None) -> dict[str, Any]:
    """Count and fingerprint target arguments without storing them.

    The fingerprint hashes the arguments joined by NUL, so ``["a b"]`` and
    ``["a", "b"]`` differ. No arguments give no fingerprint.
    """
    values = [str(item) for item in (args or ())]
    fingerprint = None
    if values:
        digest = hashlib.sha256("\0".join(values).encode("utf-8", "surrogatepass"))
        fingerprint = digest.hexdigest()[:FINGERPRINT_LENGTH]
    return {"count": len(values), "fingerprint": fingerprint, "redacted": True}


def environment_record(version: str | None = None) -> dict[str, Any]:
    """The machine and runtime a run measured, without identifying the host or user.

    Each probe is best effort: a value that cannot be read is ``None``. The
    probes run once per process, since every export would otherwise repeat
    them and none of the values change while DrainTracer runs.
    """
    return dict(_environment(version))


@functools.lru_cache(maxsize=4)
def _environment(version: str | None) -> Mapping[str, Any]:

    def probe(read) -> Any:
        try:
            return read()
        except Exception:  # an unusual platform; leave the detail out
            return None

    return MappingProxyType({
        "os": probe(platform.system) or None,
        "os_release": probe(platform.release) or None,
        "os_version": probe(platform.version) or None,
        "machine": probe(platform.machine) or None,
        "cpu_count": probe(os.cpu_count),
        "total_memory_bytes": probe(lambda: int(psutil.virtual_memory().total)),
        "python_version": probe(platform.python_version) or None,
        "python_implementation": probe(platform.python_implementation) or None,
        "draintracer_version": version,
    })


def experiment_record(
    info: ExperimentInfo,
    *,
    args: Sequence[str] | None,
    mode: str,
    duration_seconds: float | None,
    sample_interval_ms: int,
    windows_seconds: Sequence[float],
    warmup_seconds: float,
    timeout_seconds: float | None,
    budgets: Mapping[str, float] | None = None,
    version: str | None = None,
    environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The report's ``experiment`` section."""
    return {
        "schema": EXPERIMENT_SCHEMA,
        "workload_id": clean_label(info.workload_id),
        "build": clean_label(info.build),
        "operations": info.operations,
        "note": clean_label(info.note),
        "mode": mode,
        "arguments": arguments_record(args) if mode == "launch" else None,
        "duration_seconds": None if duration_seconds is None else round(duration_seconds, 3),
        "sampling": {
            "interval_ms": sample_interval_ms,
            "windows_seconds": list(windows_seconds),
            "warmup_seconds": warmup_seconds,
            "timeout_seconds": timeout_seconds,
        },
        "budgets": dict(budgets) if budgets else {},
        "environment": dict(environment) if environment is not None else environment_record(version),
        "redaction": {
            "arguments": "count and fingerprint only",
            "environment_variables": "not recorded",
            "labels": "secret-looking values replaced",
        },
    }


@dataclass(frozen=True)
class ExperimentData:
    """The ``experiment`` section read back leniently; older reports have none."""

    workload_id: str | None = None
    build: str | None = None
    operations: float | None = None
    note: str | None = None
    argument_count: int | None = None
    argument_fingerprint: str | None = None
    environment: Mapping[str, Any] | None = None


def load_experiment(report: Mapping[str, Any]) -> ExperimentData | None:
    """Parse a report's ``experiment`` section, ignoring malformed fields."""
    section = report.get("experiment")
    if not isinstance(section, Mapping):
        return None

    def text(key: str) -> str | None:
        value = section.get(key)
        return value if isinstance(value, str) and value else None

    operations = section.get("operations")
    if isinstance(operations, bool) or not isinstance(operations, (int, float)):
        operations = None
    elif not math.isfinite(float(operations)) or operations <= 0:
        operations = None
    arguments = section.get("arguments")
    count = fingerprint = None
    if isinstance(arguments, Mapping):
        raw = arguments.get("count")
        count = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None
        raw = arguments.get("fingerprint")
        fingerprint = raw if isinstance(raw, str) and raw else None
    environment = section.get("environment")
    return ExperimentData(
        workload_id=text("workload_id"),
        build=text("build"),
        operations=None if operations is None else float(operations),
        note=text("note"),
        argument_count=count,
        argument_fingerprint=fingerprint,
        environment=environment if isinstance(environment, Mapping) else None,
    )


__all__ = [
    "EXPERIMENT_SCHEMA",
    "ExperimentData",
    "ExperimentInfo",
    "REDACTED",
    "arguments_record",
    "clean_label",
    "environment_record",
    "experiment_record",
    "load_experiment",
    "redact_text",
]
