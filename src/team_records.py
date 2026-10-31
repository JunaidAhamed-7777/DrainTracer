"""Strict bounded project governance records; authentication and relationships live in the store."""

from __future__ import annotations

import json
import math
import re

from .team_models import TeamError

MAX_RECORD_BYTES = 16 * 1024
MAX_RECORD_DEPTH = 8
_COMMON = frozenset({"record_schema", "repository", "workload_id", "source_commit", "baseline_revision", "audit_head"})
_UNITS = {
    "private_bytes": frozenset({"bytes", "bytes/second"}),
    "rss_bytes": frozenset({"bytes", "bytes/second"}),
    "vms_bytes": frozenset({"bytes", "bytes/second"}),
    "handles": frozenset({"count", "count/second"}),
    "threads": frozenset({"count", "count/second"}),
    "cpu_percent": frozenset({"percent"}),
    "io_read_bytes_per_sec": frozenset({"bytes/second"}),
    "io_write_bytes_per_sec": frozenset({"bytes/second"}),
}


def _invalid():
    raise TeamError("invalid", "invalid team record") from None


def _keys(value, expected):
    if type(value) is not dict or len(value) != len(expected):
        _invalid()
    if any(type(key) is not str or key not in expected for key in value):
        _invalid()


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum or not value.isprintable()
            or (not empty and not value.strip())):
        _invalid()


def _hex(value, lengths):
    if type(value) is not str or len(value) not in lengths or not re.fullmatch("[0-9a-f]+", value):
        _invalid()


def _number(value):
    if value is None:
        return
    if type(value) not in (int, float) or (type(value) is int and value.bit_length() > 1024):
        _invalid()
    try:
        finite = math.isfinite(value)
    except OverflowError:
        _invalid()
    if not finite:
        _invalid()


def _bounded_copy(value):
    # Fixed fieldsets and scalar-only metric/qualification values have already
    # bounded traversal before encoding; arbitrary opaque trees never expand.
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_RECORD_DEPTH:
            _invalid()
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_RECORD_BYTES:
            _invalid()
        return json.loads(raw)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        _invalid()


def validate_record_value(kind, value: dict) -> dict:
    """Return detached strict JSON primitives without normalizing binding values.

    This validates record shape and bounds only. Same-project approval lookup,
    current roles, expiry, cascading deletion and audit authority require a
    transaction in the authoritative TeamStore.
    """
    if type(kind) is not str:
        _invalid()
    if kind == "baseline_approval":
        _keys(value, _COMMON | {"note"})
        _text(value["note"], 500, empty=True)
    elif kind == "comparison_receipt":
        _keys(value, _COMMON | {"approval_id", "outcome", "comparison_sha256", "basis", "metrics", "qualifications"})
        _hex(value["approval_id"], {32})
        _hex(value["comparison_sha256"], {64})
        if type(value["outcome"]) is not str or value["outcome"] not in {"pass", "regression", "inconclusive", "failure"}:
            _invalid()
        if type(value["basis"]) is not str or value["basis"] != "client-computed":
            _invalid()
        metrics = value["metrics"]
        if type(metrics) is not dict or len(metrics) > 8:
            _invalid()
        for metric, row in metrics.items():
            if type(metric) is not str or metric not in _UNITS:
                _invalid()
            _keys(row, {"verdict", "before", "after", "unit"})
            if type(row["verdict"]) is not str or row["verdict"] not in {"improved", "regressed", "unchanged", "inconclusive"}:
                _invalid()
            if type(row["unit"]) is not str or row["unit"] not in _UNITS[metric]:
                _invalid()
            _number(row["before"])
            _number(row["after"])
        qualifications = value["qualifications"]
        if type(qualifications) is not list or len(qualifications) > 16:
            _invalid()
        for qualification in qualifications:
            _text(qualification, 200, empty=True)
    else:
        _invalid()
    if type(value["record_schema"]) is not int or value["record_schema"] != 1:
        _invalid()
    _text(value["repository"], 256)
    _text(value["workload_id"], 128)
    _hex(value["source_commit"], {40, 64})
    _hex(value["audit_head"], {64})
    revision = value["baseline_revision"]
    if type(revision) is not int or not 1 <= revision <= 2**31 - 1:
        _invalid()
    return _bounded_copy(value)


__all__ = ["validate_record_value"]
