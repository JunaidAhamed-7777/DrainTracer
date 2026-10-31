"""Deliberately approved local repeated-run baselines and reviewable exceptions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

from .analysis import RESOURCES, SIGNAL_TOTALS, finite, summarize
from .budgets import BUDGET_OPTIONS, check_budgets, parse_budget
from .repeat_comparison import MAX_GROUP_BYTES, MAX_GROUP_RUNS, compare_repeated_reports
from .report_contract import _check_text_depth, _unique_object, read_report
from .report_diff import LoadedReport, comparability_caveats, load_report

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_AUDIT_ENTRIES = 512
MAX_SUPPRESSIONS = 64
_HASH = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class BaselineError(ValueError):
    """A baseline input, approval or local store is unusable."""


class BaselineEvidenceError(BaselineError):
    """Evidence is insufficient or incompatible; no exception can waive it."""


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _text(value: Any, name: str, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise BaselineError(f"{name} must be nonempty text without controls (at most {limit} characters)")
    return value.strip()


def _source(value: str) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise BaselineError("source_commit must be a full lowercase 40- or 64-character commit hash")
    return value


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        raise BaselineError("expires must be an ISO-8601 UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise BaselineError("expires must include UTC (Z or +00:00)")
    return parsed


def _actor() -> str:
    if os.name != "nt":
        return f"uid:{os.getuid()}"
    import ctypes
    from ctypes import wintypes

    function = ctypes.WinDLL("secur32", use_last_error=True).GetUserNameExW
    function.argtypes = [wintypes.ULONG, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
    function.restype = wintypes.BOOL
    length = wintypes.ULONG(1024)
    buffer = ctypes.create_unicode_buffer(length.value)
    if not function(2, buffer, ctypes.byref(length)):  # NameSamCompatible: token's domain\user.
        raise ctypes.WinError(ctypes.get_last_error())
    return f"windows:{buffer.value}"


def report_budget_defaults(reports: Sequence[LoadedReport]) -> dict[str, float]:
    """Require one unambiguous set of approved report budgets, with no invented zeros."""
    result = None
    for report in reports:
        experiment = report.data.get("experiment") or {}
        raw = experiment.get("budgets", {}) if isinstance(experiment, Mapping) else {}
        if not isinstance(raw, Mapping) or set(raw) - set(SIGNAL_TOTALS.values()):
            raise BaselineError("report budgets must name only private_bytes, handles or threads")
        budgets = {key: parse_budget(key, value) for key, value in raw.items()}
        if result is not None and budgets != result:
            raise BaselineError("approved reports disagree about peak budgets; resolve the policy explicitly")
        result = budgets
    return result or {}


def load_runs(paths: Sequence[str | Path]) -> tuple[LoadedReport, ...]:
    if not 2 <= len(paths) <= MAX_GROUP_RUNS:
        raise BaselineError(f"provide 2 to {MAX_GROUP_RUNS} independent runs")
    total = 0
    for path in paths:
        info = Path(path).stat()
        if not stat.S_ISREG(info.st_mode):
            raise BaselineError("run inputs must be ordinary report files")
        total += info.st_size
        if total > MAX_GROUP_BYTES:
            raise BaselineError("combined run evidence exceeds 128 MiB")
    reports = []
    total = 0
    for path in paths:
        report = load_report(path)
        total += len(_encoded(report.data))
        if total > MAX_GROUP_BYTES:
            raise BaselineError("combined run evidence exceeds 128 MiB")
        reports.append(report)
    _eligible(reports)
    return tuple(reports)


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _provenance(data: Mapping[str, Any]) -> None:
    experiment = _mapping(data.get("experiment"))
    environment = _mapping(experiment.get("environment"))
    arguments = _mapping(experiment.get("arguments"))
    settings = _mapping(data.get("settings"))
    if (data.get("mode") != "launch" or not isinstance(data.get("target_path"), str)
            or not data["target_path"].strip()
            or not isinstance(data.get("version"), str)
            or not re.fullmatch(r"v\d+\.\d+\.\d+", data["version"])
            or environment.get("draintracer_version") != data["version"]):
        raise BaselineEvidenceError("baseline needs a launch target and matching recorded detector versions")
    required = ("os", "os_release", "machine", "python_version", "python_implementation",
                "private_memory_metric", "handle_metric")
    if (any(not isinstance(environment.get(key), str) or not environment[key] for key in required)
            or any(finite(environment.get(key)) is None or environment[key] <= 0
                   for key in ("cpu_count", "total_memory_bytes"))):
        raise BaselineEvidenceError("baseline requires known platform, runtime, counter and host-capacity provenance")
    count = arguments.get("count")
    fingerprint = arguments.get("fingerprint")
    if (type(count) is not int or count < 0
            or (count == 0 and fingerprint is not None)
            or (count > 0 and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{16}", fingerprint)))):
        raise BaselineEvidenceError("baseline requires explicit target argument provenance")
    if (finite(settings.get("sample_interval_ms")) is None
            or settings["sample_interval_ms"] <= 0 or finite(settings.get("warmup_seconds")) is None
            or not isinstance(settings.get("windows_seconds"), list) or not settings["windows_seconds"]
            or any(finite(value) is None or value <= 0 for value in settings["windows_seconds"])
            or any(any(finite(_mapping(_mapping(settings.get("thresholds")).get(resource)).get(key)) is None
                           or _mapping(settings["thresholds"][resource])[key] <= 0
                           for key in ("critical_delta", "critical_slope")) for resource in RESOURCES)):
        raise BaselineEvidenceError("baseline requires complete detector sampling and threshold settings")


def _eligible(reports: Sequence[LoadedReport]) -> str:
    paths = [report.path.resolve() for report in reports]
    identifiers = [report.data.get("report_id") for report in reports]
    if (len(set(paths)) != len(paths) or any(not isinstance(value, str) or not value for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise BaselineError("runs need distinct paths and recorded report IDs")
    workload = None
    for report in reports:
        data = report.data
        measurement = _mapping(_mapping(data.get("outcome")).get("measurement"))
        experiment = _mapping(data.get("experiment"))
        name = experiment.get("workload_id")
        if (data.get("report_schema") != 3 or data.get("detection_ran") is not True
                or measurement.get("full_coverage") is not True or measurement.get("result") != "conclusive"):
            raise BaselineEvidenceError("baseline evidence requires schema 3, judged windows and explicit full conclusive coverage")
        outcome = _mapping(data.get("outcome"))
        startup, cleanup, target = (_mapping(outcome.get(key)) for key in ("startup", "cleanup", "target"))
        headless = data.get("headless")
        if (startup.get("state") != "started" or startup.get("error")
                or cleanup.get("state") != "complete" or cleanup.get("remaining_count") != 0
                or cleanup.get("resume_required") is not False or cleanup.get("errors") != []
                or target.get("failed") is not False or outcome.get("monitor_errors") != []
                or data.get("target_exit_code") not in (None, 0)
                or (headless is not None and (type(_mapping(headless).get("exit_code")) is not int
                                              or headless["exit_code"] not in (0, 1, 5)))):
            raise BaselineEvidenceError("startup, cleanup, target or monitor failures cannot become baseline evidence")
        workflow = _mapping(outcome.get("workload"))
        raw_workflow = data.get("workload")
        if ("workload" in outcome or "workload" in data) and workflow.get("state") != "complete":
            raise BaselineEvidenceError("unfinished or failed workload evidence cannot become a baseline")
        if "workload" in data:
            raw = _mapping(raw_workflow)
            work = _mapping(raw.get("outcome"))
            steps = work.get("exercise")
            exercise_cleanup = _mapping(work.get("exercise_cleanup"))
            if (raw.get("schema") != 1 or work.get("state") != "complete" or work.get("failure")
                    or not isinstance(steps, list)
                    or any(not isinstance(step, Mapping) or step.get("state") != "complete" or step.get("exit_code") != 0
                           for step in steps)
                    or (exercise_cleanup and (exercise_cleanup.get("state") != "complete"
                                              or exercise_cleanup.get("remaining_count") != 0))):
                raise BaselineEvidenceError("raw workload outcome, exercise or cleanup is not confirmed complete")
        _provenance(data)
        if not isinstance(name, str) or not name.strip():
            raise BaselineEvidenceError("baseline evidence requires a declared workload_id")
        if workload is not None and name != workload:
            raise BaselineEvidenceError("runs have different workloads")
        workload = name
        if any(finite(report.summary.resources[resource].drift_per_second) is None for resource in RESOURCES):
            raise BaselineEvidenceError("every resource requires a measured drift")
        if any(finite(report.summary.peaks.get(metric)) is None for metric in SIGNAL_TOTALS.values()):
            raise BaselineEvidenceError("every signal resource requires a known peak")
        if _mapping(data.get("recovery")).get("partial") or data.get("privacy"):
            raise BaselineEvidenceError("recovered or transformed evidence cannot become a controlled baseline")
    for first, second in combinations(reports, 2):
        if comparability_caveats(first, second):
            raise BaselineEvidenceError("runs have compatibility or coverage caveats; inspect strict diff before approval")
    return workload


def _ordinary(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise BaselineError("store files must be ordinary files owned by the current OS user")


class BaselineStore:
    """Cooperative single-writer store; no network identities or automatic approval."""

    def __init__(self, path: str | Path, *, create: bool = False):
        supplied = Path(path)
        if supplied.is_symlink():
            raise BaselineError("baseline directory cannot be a symbolic link")
        if create:
            supplied.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = supplied.resolve(strict=True)
        if not self.path.is_dir():
            raise BaselineError("baseline store must be a directory")
        lock_path = self.path / "writer.lock"
        _ordinary(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self._lock = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.fstat(descriptor).st_size > 1:
                raise BaselineError("invalid baseline lock file")
            if not os.fstat(descriptor).st_size:
                self._lock.write(b"\0")
            self._lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.audit = self._read(create)
            self.state = self._replay()
        except BaseException:
            self._lock.close()
            raise

    def _read(self, create: bool) -> list[dict[str, Any]]:
        path = self.path / "baseline.json"
        _ordinary(path)
        if not path.exists() and create:
            return []
        with path.open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise BaselineError("baseline manifest exceeds 2 MiB")
        text = raw.decode("utf-8")
        _check_text_depth(text)
        document = json.loads(text, object_pairs_hook=_unique_object)
        if (not isinstance(document, dict) or type(document.get("baseline_schema")) is not int
                or document["baseline_schema"] != 1 or not isinstance(document.get("audit"), list)
                or len(document["audit"]) > MAX_AUDIT_ENTRIES):
            raise BaselineError("unsupported or malformed baseline manifest")
        return document["audit"]

    def _replay(self, entries=None) -> dict[str, Any]:
        previous = None
        state = {"revision": 0, "reports": [], "budgets": {}, "workload_id": None, "suppressions": []}
        for number, entry in enumerate(self.audit if entries is None else entries, 1):
            if not isinstance(entry, dict):
                raise BaselineError("invalid audit entry")
            body = {key: value for key, value in entry.items() if key != "digest"}
            if (type(entry.get("sequence")) is not int or entry["sequence"] != number or entry.get("previous") != previous
                    or entry.get("digest") != _digest(body)):
                raise BaselineError("baseline audit chain is broken")
            for key in ("actor", "approved_by", "reason"):
                _text(entry.get(key), key)
            _source(entry.get("source_commit"))
            recorded_at = _time(entry.get("at"))
            details = entry.get("details")
            if not isinstance(details, dict):
                raise BaselineError("invalid audit details")
            if entry.get("action") == "promote":
                references = details.get("reports")
                if (not isinstance(references, list) or not 2 <= len(references) <= MAX_GROUP_RUNS
                        or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in references)
                        or len(set(references)) != len(references)):
                    raise BaselineError("invalid approved report references")
                budgets = details.get("budgets")
                if not isinstance(budgets, dict) or set(budgets) - set(SIGNAL_TOTALS.values()):
                    raise BaselineError("invalid approved budgets")
                budgets = {key: parse_budget(key, value) for key, value in budgets.items()}
                workload = _text(details.get("workload_id"), "workload_id")
                state.update(revision=state["revision"] + 1, reports=references,
                             budgets=budgets, workload_id=workload, suppressions=[])
            elif entry.get("action") == "suppress":
                if (details.get("resource") not in RESOURCES or details.get("kind") not in ("regression", "budget")
                        or details.get("baseline_revision") != state["revision"] or state["revision"] == 0
                        or details.get("workload_id") != state["workload_id"]
                        or len(state["suppressions"]) >= MAX_SUPPRESSIONS):
                    raise BaselineError("invalid scoped suppression")
                expiry = _time(details.get("expires"))
                if not recorded_at < expiry <= recorded_at + timedelta(days=90):
                    raise BaselineError("recorded suppression expiry violates the 90-day policy")
                state["suppressions"].append({**details, "approved_by": entry["approved_by"],
                                               "reason": entry["reason"], "sequence": number})
            else:
                raise BaselineError("unknown baseline audit action")
            previous = entry["digest"]
        return state

    def _append(self, action: str, details: dict[str, Any], *, approved_by: str, reason: str, source_commit: str):
        if len(self.audit) >= MAX_AUDIT_ENTRIES:
            raise BaselineError("audit has reached 512 entries; archive the intact store before creating a new one")
        body = {"sequence": len(self.audit) + 1, "previous": self.audit[-1]["digest"] if self.audit else None,
                "action": action, "details": details, "at": datetime.now(timezone.utc).isoformat(),
                "actor": _actor(), "approved_by": _text(approved_by, "approved_by"),
                "reason": _text(reason, "reason"), "source_commit": _source(source_commit)}
        entries = [*self.audit, {**body, "digest": _digest(body)}]
        state = self._replay(entries)
        encoded = _encoded({"baseline_schema": 1, "audit": entries})
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise BaselineError("baseline manifest would exceed 2 MiB")
        destination = self.path / "baseline.json"
        _ordinary(destination)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".baseline-", dir=self.path, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        self.audit = entries
        self.state = state

    def reports(self) -> tuple[LoadedReport, ...]:
        if not self.state["revision"]:
            raise BaselineError("no baseline has been approved")
        result = []
        total = 0
        for identifier in self.state["reports"]:
            path = self.path / f"{identifier}.json"
            _ordinary(path)
            data = read_report(path)
            total += len(_encoded(data))
            if total > MAX_GROUP_BYTES:
                raise BaselineError("approved evidence exceeds 128 MiB")
            if _digest(data) != identifier:
                raise BaselineError("approved report content hash does not match")
            result.append(LoadedReport(path, data, summarize(data)))
        _eligible(result)
        return tuple(result)

    def promote(self, reports: Sequence[LoadedReport], *, approved_by: str, reason: str,
                source_commit: str, accept_regression: bool = False) -> dict[str, Any]:
        workload = _eligible(reports)
        budgets = report_budget_defaults(reports)
        _text(approved_by, "approved_by")
        _text(reason, "reason")
        _source(source_commit)
        if type(accept_regression) is not bool:
            raise BaselineError("accept_regression must be a bool")
        peaks = {metric: max(report.summary.peaks[metric] for report in reports) for metric in SIGNAL_TOTALS.values()}
        new_budget_checks = check_budgets(budgets, peaks)
        has_signal = any(report.summary.resources[resource].worst != "none"
                         for report in reports for resource in RESOURCES)
        if (any(check.exceeded for check in new_budget_checks) or has_signal) and not accept_regression:
            raise BaselineError("recorded signal or exceeded budget requires explicit accept_regression approval")
        comparison = None
        if self.state["revision"]:
            comparison = compare_repeated_reports(self.reports(), reports, budgets=self.state["budgets"])
            if comparison.caveats or comparison.blocked or comparison.measured_verdict == "inconclusive":
                raise BaselineError("incompatible, insufficient or inconclusive runs cannot replace a baseline")
            if (comparison.measured_verdict in ("regressed", "mixed") or comparison.over_budget) and not accept_regression:
                raise BaselineError("regression/budget change requires explicit accept_regression approval")
        identifiers = []
        total = 0
        for report in reports:
            encoded = _encoded(report.data)
            total += len(encoded)
            if total > MAX_GROUP_BYTES:
                raise BaselineError("combined run evidence exceeds 128 MiB")
            identifier = hashlib.sha256(encoded).hexdigest()
            destination = self.path / f"{identifier}.json"
            _ordinary(destination)
            if destination.exists():
                if _digest(read_report(destination)) != identifier:
                    raise BaselineError("existing evidence object has been changed")
            else:
                self._publish_object(destination, encoded, identifier)
            identifiers.append(identifier)
        self._append("promote", {"reports": identifiers, "budgets": budgets, "workload_id": workload,
                                 "comparison": comparison.to_dict() if comparison else None,
                                 "new_budget_checks": [check.to_dict() for check in new_budget_checks],
                                 "accepted_regression": bool(accept_regression)},
                     approved_by=approved_by, reason=reason, source_commit=source_commit)
        return {"baseline_schema": 1, "revision": self.state["revision"], "audit_head": self.audit[-1]["digest"],
                "budgets": budgets, "reports": identifiers, "comparison": comparison.to_dict() if comparison else None}

    def _publish_object(self, destination: Path, encoded: bytes, identifier: str) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".evidence-", dir=self.path, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            # The OS lock excludes cooperative writers. Keep an existing
            # verified object intact even if one appeared during this write.
            _ordinary(destination)
            if destination.exists():
                if _digest(read_report(destination)) != identifier:
                    raise BaselineError("existing evidence object has been changed")
            else:
                os.replace(temporary, destination)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def suppress(self, *, resource: str, kind: str, expires: str, approved_by: str,
                 reason: str, source_commit: str) -> dict[str, Any]:
        self.reports()  # Never add exceptions to an absent or damaged baseline.
        now = datetime.now(timezone.utc)
        expiry = _time(expires)
        if not now < expiry <= now + timedelta(days=90):
            raise BaselineError("suppression expiry must be in the future and within 90 days")
        if resource not in RESOURCES or kind not in ("regression", "budget"):
            raise BaselineError("suppression must name one resource and regression or budget")
        if len(self.state["suppressions"]) >= MAX_SUPPRESSIONS:
            raise BaselineError("suppression limit reached")
        details = {"resource": resource, "kind": kind, "expires": expiry.isoformat(),
                   "baseline_revision": self.state["revision"], "workload_id": self.state["workload_id"]}
        self._append("suppress", details, approved_by=approved_by, reason=reason, source_commit=source_commit)
        return {"baseline_schema": 1, "suppression": self.state["suppressions"][-1], "audit_head": self.audit[-1]["digest"]}

    def check(self, reports: Sequence[LoadedReport], *, source_commit: str,
              budgets: Mapping[str, float] | None = None) -> tuple[int, dict[str, Any]]:
        _source(source_commit)
        _eligible(reports)
        if budgets is not None and (not isinstance(budgets, Mapping) or set(budgets) - set(SIGNAL_TOTALS.values())):
            raise BaselineError("budget overrides must name only private_bytes, handles or threads")
        resolved = {**self.state["budgets"], **(budgets or {})}
        resolved = {key: parse_budget(key, value) for key, value in resolved.items()}
        comparison = compare_repeated_reports(self.reports(), reports, budgets=resolved)
        now = datetime.now(timezone.utc)
        exceptions = [{**item, "active": _time(item["expires"]) > now} for item in self.state["suppressions"]]
        findings = [{"resource": item.resource, "kind": "regression"}
                    for item in comparison.resources if item.verdict == "regressed"]
        findings += [{"resource": next(resource for resource, metric in SIGNAL_TOTALS.items() if metric == check.metric),
                      "kind": "budget"} for check in comparison.over_budget]
        for finding in findings:
            finding["suppressed_by"] = [item["sequence"] for item in exceptions if item["active"]
                                         and item["resource"] == finding["resource"] and item["kind"] == finding["kind"]]
        # Every caveat is a gate here. Exceptions apply only after conclusive
        # comparison and never turn missing provenance or evidence into a pass.
        if comparison.caveats or comparison.blocked or comparison.measured_verdict == "inconclusive":
            code = 3
        elif any(item["kind"] == "regression" and not item["suppressed_by"] for item in findings):
            code = 1
        elif any(item["kind"] == "budget" and not item["suppressed_by"] for item in findings):
            code = 4
        else:
            code = 0
        return code, {"baseline_schema": 1, "revision": self.state["revision"], "source_commit": source_commit,
                      "audit_head": self.audit[-1]["digest"], "comparison": comparison.to_dict(),
                      "budgets": resolved, "findings": findings, "suppressions": exceptions,
                      "exit_code": code, "passed": code == 0}

    def close(self):
        self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BaselineError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="draintracer-baseline", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("promote", "check", "suppress", "audit"):
        child = commands.add_parser(command)
        child.add_argument("store", type=Path)
        if command in ("promote", "check"):
            child.add_argument("--run", action="append", required=True, type=Path,
                               help="A report from an independent run; provide 2-20.")
        if command != "audit":
            child.add_argument("--source-commit", required=True, help="Declared full source commit hash for this action.")
        if command in ("promote", "suppress"):
            child.add_argument("--approved-by", required=True, help="Declared approver; access policy is enforced externally.")
            child.add_argument("--reason", required=True)
        if command == "promote":
            child.add_argument("--accept-regression", action="store_true",
                               help="Explicitly acknowledge a worse or already over-budget baseline.")
        if command == "suppress":
            child.add_argument("--resource", choices=RESOURCES, required=True)
            child.add_argument("--kind", choices=("regression", "budget"), required=True)
            child.add_argument("--expires", required=True, help="UTC ISO-8601 expiry, at most 90 days away.")
        if command == "check":
            for metric, option in BUDGET_OPTIONS.items():
                child.add_argument(f"--{option.replace('_', '-')}", dest=option,
                                   help=f"Override the approved {metric} peak budget for this check.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        runs = load_runs(args.run) if args.command in ("promote", "check") else None
        with BaselineStore(args.store, create=args.command == "promote") as store:
            if args.command == "promote":
                result = store.promote(runs, approved_by=args.approved_by, reason=args.reason,
                                       source_commit=args.source_commit, accept_regression=args.accept_regression)
                code = 0
            elif args.command == "suppress":
                result = store.suppress(resource=args.resource, kind=args.kind, expires=args.expires,
                                        approved_by=args.approved_by, reason=args.reason, source_commit=args.source_commit)
                code = 0
            elif args.command == "check":
                budgets = {metric: parse_budget(metric, value) for metric, option in BUDGET_OPTIONS.items()
                           if (value := getattr(args, option)) is not None}
                code, result = store.check(runs, source_commit=args.source_commit, budgets=budgets)
            else:
                result = {"baseline_schema": 1, "state": store.state, "audit": store.audit}
                code = 0
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return code
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        code = 3 if isinstance(error, BaselineEvidenceError) else 2
        print(json.dumps({"baseline_schema": 1, "error": str(error), "exit_code": code}, ensure_ascii=True), file=sys.stderr)
        return code


__all__ = ["BaselineError", "BaselineEvidenceError", "BaselineStore", "load_runs", "main", "report_budget_defaults"]


if __name__ == "__main__":
    raise SystemExit(main())
