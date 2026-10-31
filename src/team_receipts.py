"""Typed public receipts of local W4 decisions; no private report/path forwarding."""

from __future__ import annotations

import hashlib
import json

from .report_diff import COMPARABLE, INSUFFICIENT_COVERAGE, NOT_COMPARABLE
from .team_records import validate_record_value
from .team_transport import SharingError

_METRICS = {"memory": ("private_bytes", "bytes/second"),
            "handles": ("handles", "count/second"), "threads": ("threads", "count/second")}


def _invalid():
    raise SharingError("invalid", "Use a supported, internally consistent local W4 result") from None


def prepare_approval(store, repository: str, *, note="") -> dict:
    """Caller opens a real BaselineStore, whose replay/objects must validate."""
    store.reports()
    promotions = [entry for entry in store.audit if entry.get("action") == "promote"]
    if not promotions or not store.audit:
        _invalid()
    value = {"record_schema": 1, "repository": repository, "workload_id": store.state["workload_id"],
             "source_commit": promotions[-1]["source_commit"], "baseline_revision": store.state["revision"],
             "audit_head": store.audit[-1]["digest"], "note": note}
    return validate_record_value("baseline_approval", value)


def _suppressed(findings, suppressions, revision, workload_id):
    if not isinstance(findings, list) or not findings or not isinstance(suppressions, list):
        return False
    active = {item["sequence"]: item for item in suppressions if isinstance(item, dict)
              and type(item.get("sequence")) is int and item.get("active") is True}
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("suppressed_by"), list) or not finding["suppressed_by"]:
            return False
        for sequence in finding["suppressed_by"]:
            if type(sequence) is not int or sequence not in active:
                return False
            exception = active[sequence]
            if (any(exception.get(key) != finding.get(key) for key in ("resource", "kind"))
                    or exception.get("baseline_revision") != revision or exception.get("workload_id") != workload_id):
                return False
    return True


def prepare_receipt(result: dict, *, repository: str, workload_id: str, approval_id: str) -> dict:
    """Summarize a local decision, preserving inconclusive/budget/exception meaning.

    This checks consistency; it does not authenticate the caller's private
    measurements. The server revalidates the live approval and current role.
    """
    if (not isinstance(result, dict) or type(result.get("baseline_schema")) is not int
            or result["baseline_schema"] != 1 or "error" in result
            or type(result.get("exit_code")) is not int or result["exit_code"] not in {0, 1, 2, 3, 4}
            or not isinstance(result.get("findings"), list) or len(result["findings"]) > 6
            or not isinstance(result.get("suppressions"), list) or len(result["suppressions"]) > 64
            or type(result.get("passed")) is not bool or result["passed"] != (result["exit_code"] == 0)):
        _invalid()
    comparison = result.get("comparison")
    if (not isinstance(comparison, dict) or comparison.get("mode") != "repeated" or comparison.get("strict") is not True
            or type(comparison.get("comparability")) is not str
            or comparison["comparability"] not in {COMPARABLE, NOT_COMPARABLE, INSUFFICIENT_COVERAGE}
            or type(comparison.get("measured_verdict")) is not str
            or comparison["measured_verdict"] not in {"improved", "regressed", "mixed", "unchanged", "inconclusive"}
            or not isinstance(comparison.get("caveats"), list) or len(comparison["caveats"]) > 8192
            or type(comparison.get("over_budget")) is not bool):
        _invalid()
    resources = comparison.get("resources")
    if not isinstance(resources, dict) or set(resources) != set(_METRICS):
        _invalid()
    metrics = {}
    for resource, (metric, unit) in _METRICS.items():
        row = resources[resource]
        if (not isinstance(row, dict) or type(row.get("verdict")) is not str
                or row["verdict"] not in {"improved", "regressed", "unchanged", "overlap", "unknown"}):
            _invalid()
        spreads = (row.get("drift_before"), row.get("drift_after"))
        if any(not isinstance(spread, dict) or "median" not in spread for spread in spreads):
            _invalid()
        metrics[metric] = {"verdict": "inconclusive" if row["verdict"] in {"overlap", "unknown"} else row["verdict"],
                           "before": spreads[0]["median"], "after": spreads[1]["median"], "unit": unit}
    code = result["exit_code"]
    exceptions_used = any(isinstance(item, dict) and item.get("suppressed_by") for item in result.get("findings", []))
    if code == 0:
        if (comparison["comparability"] != COMPARABLE or comparison.get("measured_verdict") == "inconclusive"
                or any(not isinstance(item, dict) or item.get("blocks") is not None for item in comparison["caveats"])
                or any(row["verdict"] == "inconclusive" or row["before"] is None or row["after"] is None
                       for row in metrics.values())):
            _invalid()
        if ((comparison["over_budget"] or any(row["verdict"] == "regressed" for row in metrics.values()))
                and not _suppressed(result["findings"], result["suppressions"], result.get("revision"), workload_id)):
            _invalid()
    qualifications = ["Client-computed on a controlled runner; the server did not recompute private evidence.",
                      "Observed run ranges describe these runs, not confidence intervals."]
    if comparison["caveats"]:
        qualifications.append("Recorded-choice qualifications are present in the protected local comparison.")
    if exceptions_used:
        qualifications.append("Local scoped exceptions influenced policy; consult the approved W4 audit.")
    if comparison["over_budget"]:
        qualifications.append("A resource budget was exceeded in the local check; policy may include an explicit scoped exception.")
    value = {"record_schema": 1, "repository": repository, "workload_id": workload_id,
             "source_commit": result.get("source_commit"), "baseline_revision": result.get("revision"),
             "audit_head": result.get("audit_head"), "approval_id": approval_id,
             "basis": "client-computed", "outcome": {0: "pass", 1: "regression", 2: "failure", 3: "inconclusive", 4: "regression"}[code],
             "metrics": metrics, "qualifications": qualifications}
    # Fingerprint only the typed public content; never hash path-bearing raw
    # result text, selected environment values or arbitrary private caveats.
    value["comparison_sha256"] = "0" * 64
    value = validate_record_value("comparison_receipt", value)
    value.pop("comparison_sha256")
    value["comparison_sha256"] = hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")).hexdigest()
    return validate_record_value("comparison_receipt", value)


def comparison_text(record: dict) -> str:
    if not isinstance(record, dict) or record.get("kind") != "comparison_receipt":
        _invalid()
    value = validate_record_value("comparison_receipt", record.get("value"))
    lines = ["Client-computed comparison (recorded evidence)", f"Repository: {value['repository']}",
             f"Commit: {value['source_commit']}", f"Workload: {value['workload_id']}",
             f"Approved baseline revision: {value['baseline_revision']}", f"Outcome: {value['outcome']}"]
    for metric, row in value["metrics"].items():
        before = "unknown" if row["before"] is None else f"{row['before']:g}"
        after = "unknown" if row["after"] is None else f"{row['after']:g}"
        lines.append(f"{metric}: {row['verdict']}; {before} -> {after} {row['unit']}")
    lines.extend(f"Qualification: {qualification}" for qualification in value["qualifications"])
    return "\n".join(lines)
