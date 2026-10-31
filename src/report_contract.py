"""Public, dependency-free report validation and versioned machine summaries.

Run ``python -m draintracer.report_contract --help`` in an installed package.
No target is launched and no terminal/UI dependency is imported.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

SUPPORTED_REPORT_VERSIONS = (1, 2, 3)
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_REPORT_DEPTH = 64
MAX_REPORT_VALUES = 1_000_000
METRIC_UNITS = {
    "private_bytes": "bytes", "rss_bytes": "bytes", "vms_bytes": "bytes",
    "handles": "count", "threads": "count", "cpu_percent": "percent",
    "io_read_bytes_per_sec": "bytes/second", "io_write_bytes_per_sec": "bytes/second",
}
SEVERITIES = ("none", "low", "medium", "high", "critical")


class ReportContractError(ValueError):
    """An untrusted report violates a supported format or ingestion bound."""


def report_schema() -> dict[str, Any]:
    """Return a standalone JSON Schema for supported reports (missing version means 1)."""
    numeric = {"type": ["number", "null"]}
    process = {"type": "object", "properties": {
        "pid": {"type": "integer", "minimum": 0}, "create_time": numeric,
        "name": {"type": "string"}, "status": {"type": "string"},
        "access_denied": {"type": "boolean"},
        "unmeasured": {"type": "array", "items": {"type": "string"}},
        **{metric: numeric for metric in METRIC_UNITS},
    }}
    snapshot = {"type": "object", "required": ["timestamp"], "properties": {
        "timestamp": {"type": "number"},
        "processes": {"type": "array", "items": process},
        "totals": {"type": "object", "properties": {metric: numeric for metric in METRIC_UNITS}},
        "alerts": {"type": "array", "items": {"type": "object"}},
        "coverage": {"type": ["object", "null"]},
        "metadata": {"type": ["object", "null"]},
        "monitoring_paused": {"type": "boolean"}, "warming_up": {"type": "boolean"},
        "severity": {"enum": list(SEVERITIES)},
    }}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/JunaidAhamed-7777/DrainTracer/raw/main/schemas/report.json",
        "title": "DrainTracer report versions 1–3", "type": "object", "required": ["snapshots"],
        "properties": {
            "report_schema": {"type": "integer", "enum": list(SUPPORTED_REPORT_VERSIONS)},
            "report_id": {"type": ["string", "null"]}, "version": {"type": ["string", "null"]},
            "target_path": {"type": ["string", "null"]},
            "mode": {"type": ["string", "null"]},
            "target_exit_code": {"type": ["integer", "null"]},
            "snapshot_stride": {"type": "integer", "minimum": 1},
            "snapshots": {"type": "array", "items": snapshot},
            "detection_ran": {"type": ["boolean", "null"]},
            **{key: {"type": ["object", "null"]}
               for key in ("settings", "experiment", "coverage", "outcome", "session_facts")},
        },
    }


def summary_schema() -> dict[str, Any]:
    """Return the stable session-summary version 1 JSON Schema."""
    nullable_string = {"type": ["string", "null"]}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/JunaidAhamed-7777/DrainTracer/raw/main/schemas/session-summary.json",
        "title": "DrainTracer session summary version 1", "type": "object",
        "required": ["summary_schema", "report_schema", "report_id", "version", "target",
                     "measurement", "monitor_errors", "peaks", "worst_severity", "counts"],
        "properties": {
            "summary_schema": {"const": 1},
            "report_schema": {"enum": list(SUPPORTED_REPORT_VERSIONS)},
            "report_id": nullable_string, "version": nullable_string,
            "target": {"type": "object", "required": ["path", "mode", "exit_code"],
                       "properties": {"path": nullable_string, "mode": nullable_string,
                                      "exit_code": {"type": ["integer", "null"]}}},
            "measurement": {"type": "object", "required": ["detection_ran", "partial_coverage", "result"],
                            "properties": {"detection_ran": {"type": ["boolean", "null"]},
                                           "partial_coverage": {"type": ["boolean", "null"]},
                                           "result": nullable_string}},
            "monitor_errors": {"type": ["array", "null"], "items": {"type": "string"}},
            "workload_state": {"enum": [None, "not_started", "starting", "running", "complete", "failed",
                                         "interrupted", "monitor_error", "startup_failed"]},
            "peaks": {"type": "object", "required": list(METRIC_UNITS), "properties": {
                metric: {"type": "object", "required": ["value", "timestamp", "unit", "basis"],
                         "properties": {"value": {"type": ["number", "null"]},
                                        "timestamp": {"type": ["number", "null"]},
                                        "unit": {"const": unit},
                                        "basis": {"enum": ["session_facts", "retained_snapshots", "unavailable"]}}}
                for metric, unit in METRIC_UNITS.items()}},
            "worst_severity": {"enum": [*SEVERITIES, None]},
            "counts": {"type": "object", "required": ["stored_snapshots", "observed_samples", "chart_stride"],
                       "properties": {key: {"type": ["integer", "null"], "minimum": 0}
                                      for key in ("stored_snapshots", "observed_samples", "chart_stride")}},
        },
    }


def _matches_type(value: Any, kind: str) -> bool:
    return {"object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "boolean": type(value) is bool,
            "integer": type(value) is int, "number": type(value) in (int, float),
            "null": value is None}[kind]


def _validate(value: Any, schema: dict[str, Any], location: str = "$") -> None:
    kinds = schema.get("type")
    if kinds and not any(_matches_type(value, kind) for kind in ([kinds] if isinstance(kinds, str) else kinds)):
        raise ReportContractError(f"{location}: expected {kinds}")
    if "enum" in schema and value not in schema["enum"]:
        raise ReportContractError(f"{location}: unsupported value")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ReportContractError(f"{location}: expected {schema['const']!r}")
    if type(value) in (int, float) and "minimum" in schema and value < schema["minimum"]:
        raise ReportContractError(f"{location}: must be at least {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                raise ReportContractError(f"{location}: missing {key}")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                _validate(value[key], child, f"{location}.{key}")
    elif isinstance(value, list) and "items" in schema:
        for index, child in enumerate(value):
            _validate(child, schema["items"], f"{location}[{index}]")


def _bounded_values(data: Any) -> None:
    pending = [(data, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > MAX_REPORT_VALUES or depth > MAX_REPORT_DEPTH:
            raise ReportContractError("report exceeds the value-count or nesting-depth limit")
        if isinstance(value, (dict, list)) and count + len(pending) + len(value) > MAX_REPORT_VALUES:
            raise ReportContractError("report exceeds the value-count limit")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
        elif type(value) in (int, float):
            try:
                representable = math.isfinite(value)
            except OverflowError:
                representable = False
            if not representable:
                raise ReportContractError("report contains a non-finite or unrepresentable number")


def validate_report(data: Any) -> dict[str, Any]:
    """Validate a decoded report; optional unknown fields are retained unchanged."""
    _bounded_values(data)
    if isinstance(data, dict):
        version = data.get("report_schema", 1)
        if type(version) is not int or version not in SUPPORTED_REPORT_VERSIONS:
            description = str(version) if type(version) is int else type(version).__name__
            raise ReportContractError(f"unsupported report_schema {description}; supported: 1, 2, 3")
    _validate(data, report_schema())
    return data


def _check_text_depth(text: str) -> None:
    depth, quoted, escaped = 0, False, False
    tokens, scalar = 0, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
            tokens += 1
            scalar = False
        elif character in "[{":
            tokens += 1
            scalar = False
            depth += 1
            if depth > MAX_REPORT_DEPTH:
                raise ReportContractError(f"JSON nested too deeply to be a report (limit {MAX_REPORT_DEPTH})")
        elif character in "]}":
            depth -= 1
            scalar = False
        elif character.isspace() or character in ",:":
            scalar = False
        elif not scalar:
            tokens += 1
            scalar = True
        if tokens > MAX_REPORT_VALUES:
            raise ReportContractError(f"report exceeds {MAX_REPORT_VALUES} JSON token limit")


def read_report(path: str | Path) -> dict[str, Any]:
    """Read at most 64 MiB and reject excessive depth before decoding JSON."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ReportContractError(f"report exceeds {MAX_REPORT_BYTES} byte limit")
        text = raw.decode("utf-8")
        _check_text_depth(text)
        data = json.loads(text, object_pairs_hook=_unique_object)
    except FileNotFoundError:
        raise ReportContractError("no such file") from None
    except UnicodeDecodeError:
        raise ReportContractError("not a UTF-8 JSON report") from None
    except json.JSONDecodeError as exc:
        raise ReportContractError(f"not valid JSON ({exc.msg} at line {exc.lineno})") from None
    except RecursionError:
        raise ReportContractError("JSON nested too deeply to be a report") from None
    except ReportContractError:
        raise
    except ValueError as exc:
        raise ReportContractError(f"not a usable JSON report ({exc})") from None
    except OSError as exc:
        raise ReportContractError(f"cannot read report ({exc.strerror or exc})") from None
    return validate_report(data)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReportContractError("report contains a duplicate JSON object key")
        result[key] = value
    return result


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def session_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Summarize exact recorded facts, qualifying legacy retained-snapshot fallbacks."""
    validate_report(report)
    facts = _object(report.get("session_facts"))
    peaks, signals = _object(facts.get("peaks")), _object(facts.get("signals"))
    measurement = _object(_object(report.get("outcome")).get("measurement"))
    result_peaks = {}
    for metric, unit in METRIC_UNITS.items():
        exact = _object(peaks.get(metric))
        value, stamp, basis = exact.get("value"), exact.get("timestamp"), "session_facts"
        if type(value) not in (int, float):
            points = []
            for sample in report["snapshots"]:
                coverage = _object(_object(sample.get("coverage")).get(metric))
                number = _object(sample.get("totals")).get(metric)
                if (sample.get("processes") and not sample.get("monitoring_paused")
                        and not _object(sample.get("metadata")).get("sampling_error")
                        and coverage.get("measured", 1) != 0 and type(number) in (int, float)):
                    points.append((number, sample["timestamp"]))
            value, stamp = max(points, default=(None, None), key=lambda point: point[0])
            basis = "retained_snapshots" if points else "unavailable"
        result_peaks[metric] = {"value": value, "timestamp": stamp, "unit": unit, "basis": basis}
    severities = [item.get("worst") for item in signals.values() if isinstance(item, dict)]
    severities += [sample.get("severity") for sample in report["snapshots"]]
    known = [severity for severity in severities if severity in SEVERITIES]
    counts = _object(facts.get("samples"))
    observed = counts.get("taken")
    if type(observed) is not int:
        observed = (sum(counts.get(key, 0) for key in ("collected", "failed", "paused"))
                    if type(counts.get("collected")) is int
                    and all(type(counts.get(key, 0)) is int for key in ("failed", "paused")) else None)
    summary = {
        "summary_schema": 1, "report_schema": report.get("report_schema", 1),
        "report_id": report.get("report_id"), "version": report.get("version"),
        "target": {"path": report.get("target_path"), "mode": report.get("mode"),
                   "exit_code": report.get("target_exit_code")},
        "measurement": {"detection_ran": measurement.get("detection_ran", report.get("detection_ran")),
                        "partial_coverage": measurement.get("partial_coverage",
                                                            _object(report.get("coverage")).get("partial")),
                        "result": measurement.get("result")},
        "monitor_errors": _object(report.get("outcome")).get("monitor_errors"),
        "workload_state": _object(_object(report.get("outcome")).get("workload")).get("state"),
        "peaks": result_peaks,
        "worst_severity": max(known, key=SEVERITIES.index) if known else None,
        "counts": {"stored_snapshots": len(report["snapshots"]), "observed_samples": observed,
                   "chart_stride": _object(facts.get("chart")).get("stride", report.get("snapshot_stride"))},
    }
    _validate(summary, summary_schema())
    return summary


def main(argv: list[str] | None = None) -> int:
    """Machine-only stdout; structured failures go to stderr with exit status 2."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "summary", "schema", "redact"))
    parser.add_argument("path", nargs="?")
    parser.add_argument("--kind", choices=("report", "summary"), default="report")
    parser.add_argument("--profile", choices=("share", "minimal"), default="share")
    parser.add_argument("--output-dir", help="write a redacted JSON/HTML pair; otherwise preview JSON only")
    args = parser.parse_args(argv)
    try:
        if args.action == "schema":
            result = report_schema() if args.kind == "report" else summary_schema()
        else:
            if not args.path:
                raise ReportContractError("a report path is required")
            report = read_report(args.path)
            if args.action == "redact":
                from .privacy import export_private_report, redact_report

                if args.output_dir:
                    paths = export_private_report(report, args.output_dir, args.profile)
                    result = {"privacy_schema": 1, "profile": args.profile,
                              "json_path": str(paths[0]), "html_path": str(paths[1])}
                else:
                    result = redact_report(report, args.profile)
            else:
                result = (session_summary(report) if args.action == "summary" else
                          {"validation_schema": 1, "valid": True, "report_schema": report.get("report_schema", 1)})
        print(json.dumps(result, allow_nan=False))
        return 0
    except (ReportContractError, ValueError, OSError) as exc:
        code = "export_failed" if isinstance(exc, OSError) else "invalid_report"
        print(json.dumps({"error_schema": 1, "error": {"code": code, "message": str(exc)}}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
