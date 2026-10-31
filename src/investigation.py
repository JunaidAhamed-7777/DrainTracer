"""Shareable evidence summaries built only after an explicit privacy transformation."""

from __future__ import annotations

import math
from typing import Any

from .privacy import redact_report
from .report_contract import SEVERITIES, session_summary

RESOURCES = {"memory": "private_bytes", "handles": "handles", "threads": "threads"}
_WPR = "https://learn.microsoft.com/en-us/troubleshoot/windows-server/support-tools/support-tools-xperf-wpa-wpr"
_PYTHON_MEMORY = "https://docs.python.org/3/library/tracemalloc.html"
_PYTHON_THREADS = "https://docs.python.org/3/library/threading.html#threading.enumerate"
_DOTNET_MEMORY = "https://learn.microsoft.com/en-us/dotnet/core/diagnostics/dotnet-gcdump"


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    try:
        number = float(value) if type(value) in (int, float) else None
        return number if number is not None and math.isfinite(number) else None
    except OverflowError:
        return None


def _amount(metric: str, value: Any) -> str:
    number = _number(value)
    if number is None:
        return "unknown"
    return f"{number:,.0f} bytes ({number / 1024**2:.2f} MiB)" if metric.endswith("bytes") else f"{number:,.6g}"


def _guidance(resource: str, runtime: str, platform: str | None) -> list[str]:
    guidance = []
    if resource == "memory" and runtime == "python":
        guidance.append("For Python allocations, enable tracemalloc before exercising the same workload "
                        "and compare snapshots. It does not account for all native/private memory. " + _PYTHON_MEMORY)
    elif resource == "memory" and runtime == "dotnet":
        guidance.append("For managed .NET objects, compare dotnet-gcdump captures across the same exercise. "
                        "Collection triggers a GC; managed heap evidence does not explain all private memory. "
                        + _DOTNET_MEMORY)
    if platform == "Windows":
        profile = {"memory": "heap/VirtualAlloc", "handles": "handle creation/close",
                   "threads": "process/thread lifetime and CPU"}[resource]
        guidance.append(f"Record a bounded WPR trace with {profile} events for the original process, "
                        "then inspect matching stacks and lifetimes in WPA. " + _WPR)
    elif resource == "threads" and runtime == "python":
        guidance.append("Compare threading.enumerate() around the exercise and inspect thread lifetimes; "
                        "native-extension threads can require a native profiler. " + _PYTHON_THREADS)
    elif resource == "handles" and platform == "Linux":
        guidance.append("For Linux descriptors, inspect the original process's /proc/PID/fd entries and "
                        "instrument matching open/close lifetimes during the same bounded exercise.")
    if not guidance:
        guidance.append(f"Use a runtime/native profiler for {resource} lifetimes during the same bounded "
                        "workload; the runtime/platform was not identified sufficiently to choose a profile.")
    return guidance


def investigation_summary(report: dict[str, Any], *, profile: str = "share",
                          resource: str | None = None, runtime: str = "unknown") -> dict[str, Any]:
    """Return versioned JSON containing copyable, privacy-transformed evidence text."""
    if resource is not None and resource not in RESOURCES:
        raise ValueError("resource must be memory, handles, or threads")
    if runtime not in ("unknown", "python", "dotnet", "native"):
        raise ValueError("runtime must be unknown, python, dotnet, or native")
    safe = redact_report(report, profile)
    summary = session_summary(safe)
    facts = _object(safe.get("session_facts"))
    signals = _object(facts.get("signals"))
    alerts = [(sample["timestamp"], alert) for sample in safe["snapshots"]
              for alert in sample.get("alerts", ()) if alert.get("resource") in RESOURCES]

    def rank(candidate: str) -> int:
        recorded = _object(signals.get(candidate)).get("worst", "none")
        levels = [SEVERITIES.index(recorded)] if recorded in SEVERITIES else [0]
        levels += [SEVERITIES.index(alert["severity"]) for _stamp, alert in alerts
                   if alert.get("resource") == candidate and alert.get("severity") in SEVERITIES]
        return max(levels)

    resource = resource or max(RESOURCES, key=rank)
    metric = RESOURCES[resource]
    severity = SEVERITIES[rank(resource)]
    selected = [(stamp, alert) for stamp, alert in alerts if alert.get("resource") == resource]
    selected.sort(key=lambda item: (SEVERITIES.index(item[1].get("severity", "none"))
                                   if item[1].get("severity", "none") in SEVERITIES else 0, item[0]),
                  reverse=True)
    origin = facts.get("first_sample_at")
    if _number(origin) is None:
        origin = min((sample["timestamp"] for sample in safe["snapshots"]), default=0)
    lines = [f"DrainTracer investigation - privacy profile: {profile}",
             f"Resource: {resource}; metric: {metric}; strongest recorded signal: {severity.upper()}."]
    recovery = _object(safe.get("recovery"))
    if recovery.get("partial") is True:
        lines.append("Recovered prefix: partial checkpoint evidence through the recorded cutoff; "
                     "the final target and monitoring outcomes are unconfirmed.")
    outcome = _object(safe.get("outcome"))
    startup, cleanup = _object(outcome.get("startup")), _object(outcome.get("cleanup"))
    if startup.get("state") == "failed":
        lines.append("Startup outcome: failed; this is separate from a resource-growth signal.")
    if cleanup.get("state") == "incomplete":
        lines.append(f"Cleanup outcome: incomplete; {cleanup.get('remaining_count', 'unknown')} "
                     "processes remained unresolved. "
                     + ("Known processes still require resume." if cleanup.get("resume_required") else
                        "Consult the private cleanup outcome before further process control."))
    if selected:
        stamp, alert = selected[0]
        window = _number(alert.get("window_seconds"))
        lines.append(f"Interval: {window:g} seconds ending +{max(0, stamp - origin):.1f}s from the first sample."
                     if window is not None else "Interval: retained alert did not record its window duration.")
        before, after = _number(alert.get("baseline_value")), _number(alert.get("current_value"))
        growth = after - before if before is not None and after is not None else None
        lines.append(f"Observed change: {_amount(metric, growth)} "
                     f"({_amount(metric, before)} to {_amount(metric, after)}).")
        slope = _number(alert.get("slope_per_second"))
        if slope is not None:
            unit = "bytes" if metric.endswith("bytes") else "counts"
            lines.append(f"Recorded slope: {slope:,.6g} {unit} per second.")
        contributors = [item for item in alert.get("contributors", ()) if isinstance(item, dict)]
        contributors.sort(key=lambda item: _number(item.get("delta")) or 0, reverse=True)
        if contributors:
            top = contributors[0]
            lines.append(f"Leading contributor: process-{top.get('pid')} (pseudonymous PID {top.get('pid')}); "
                         f"recorded change {_amount(metric, top.get('delta'))}.")
        else:
            lines.append("Contributing process: attribution unavailable or removed by the privacy profile.")
    else:
        lines.append("Interval and contributing process: no retained alert details; "
                     "exact session signal counts can outlive chart thinning.")
    coverage = _object(facts.get("coverage"))
    partial = _object(coverage.get("partial_samples")).get(metric)
    unavailable = _object(coverage.get("unavailable_samples")).get(metric)
    total = coverage.get("samples")
    if all(type(value) is int for value in (partial, unavailable, total)):
        lines.append(f"Coverage: {partial} of {total} live-tree samples had missing readings; "
                     f"{unavailable} were wholly unavailable for {metric}.")
    else:
        partial_state = summary["measurement"]["partial_coverage"]
        label = "partial" if partial_state is True else "complete" if partial_state is False else "unknown"
        lines.append(f"Coverage: {label}; per-resource whole-session counts were not recorded.")
    measurement = summary["measurement"]
    judged = ("yes" if measurement["detection_ran"] is True else
              "no" if measurement["detection_ran"] is False else "unknown")
    lines.append(f"Measurement outcome: {measurement['result'] or 'unknown'}; "
                 f"detection window judged: {judged}.")
    peak = summary["peaks"][metric]
    lines.append(f"Peak {metric}: {_amount(metric, peak['value'])}; basis: {peak['basis']}.")
    lines.append("Limits: sustained resource growth is a heuristic signal, not proof of an allocation "
                 "source or source-code root cause. Missing coverage and thinned detail limit attribution.")
    lines.append("Profiler handoff: use the private original report to identify the actual PID; "
                 "the identifiers above are pseudonyms. No profiling or tracing was started.")
    platform = _object(_object(safe.get("experiment")).get("environment")).get("os")
    guidance = _guidance(resource, runtime, platform)
    lines.extend(f"- {step}" for step in guidance)
    lines.append("Review this transformed evidence before sharing; resource and timing patterns remain visible.")
    return {"investigation_schema": 1, "privacy_profile": profile, "resource": resource,
            "metric": metric, "severity": severity, "text": "\n".join(lines)}
