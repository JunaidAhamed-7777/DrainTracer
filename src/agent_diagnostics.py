"""Read-only agent diagnostics, bounded removal and previewable privacy bundles."""

from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import psutil

from .agent_state import (
    AgentStateError,
    read_agent_state,
    relative_report,
    storage_inventory,
)
from .agent_transport import (
    current_user_sid,
    load_secret,
    verify_private_path,
    verify_trusted_path,
)
from .api import _version
from .checkpoints import read_checkpoint, recovered_report
from .html_report import render_html_report, write_html_report
from .privacy import PROFILES, redact_report
from .process_runner import _windows_process_exited
from .report import write_json_report
from .report_contract import read_report

MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_CONTENT = 16 * 1024 * 1024


def _saved(directory):
    directory = Path(directory).absolute()
    verify_private_path(directory)
    return read_agent_state(directory, app_version=_version(), owner=current_user_sid())


def inspect_agent(directory, client=None):
    """No writer ownership, process control or state creation on either path."""
    if client is not None:
        try:
            live = client.request("diagnostics")
            sessions, offset = [], 0
            while True:
                page = client.request("status", {"offset": offset, "limit": 32})
                sessions.extend(page["sessions"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
                if offset > 256:
                    raise AgentStateError("agent returned excessive status pagination")
            return {**live, "mode": "live", "sessions": sessions}
        except (OSError, ValueError, RuntimeError):
            pass  # Offline inspection reports its own validity without inventing live facts.
    try:
        saved = _saved(directory)
        sessions = []
        for record in saved["sessions"]:
            pending = record["state"] in {"accepted", "starting", "running", "stopping"}
            sessions.append({"session_id": record["session_id"], "mode": record["mode"],
                             "state": "interrupted" if pending else record["state"],
                             "saved_state": record["state"], "sample_count": record["sample_count"],
                             "measurement": record["measurement"], "full_coverage": record["full_coverage"],
                             "last_observed_at": record["updated_at"], "result": record["result"],
                             "reports": record["reports"], "final_outcome_confirmed": not pending and record["state"] != "interrupted"})
        return {"diagnostics_schema": 1, "version": _version(), "mode": "offline", "responsive": False,
                "ready": False, "fresh": False, "queue_depth": None, "active_sessions": None,
                "saved_active_sessions": sum(record["state"] in {"accepted", "starting", "running", "stopping"}
                                             for record in saved["sessions"]),
                "retained_records": len(sessions), "sessions": sessions,
                "storage": storage_inventory(Path(directory)), "storage_error": None,
                "journal_age_seconds": max(0, time.time() - saved["updated_at"])}
    except (OSError, ValueError, TypeError) as error:
        return {"diagnostics_schema": 1, "version": _version(), "mode": "unavailable", "responsive": False,
                "ready": False, "fresh": False, "sessions": [], "error": str(error)[:512]}


def _record(directory, session_id):
    saved = _saved(directory)
    record = next((record for record in saved["sessions"] if record["session_id"] == session_id), None)
    if record is None:
        raise AgentStateError("the requested session is not retained by this agent")
    return record


def assigned_checkpoint(directory, record):
    path = verify_trusted_path(Path(directory) / "checkpoints" / record["checkpoint"])
    report = read_checkpoint(path, app_version=_version())
    metadata = report["checkpoint"]
    if metadata["writer_identity"] != record["writer_identity"]:
        raise AgentStateError("checkpoint writer identity does not match the retained session")
    target = metadata["target_identity"]
    if record["pid"] is not None and (target["pid"] != record["pid"] or target["create_time"] != record["create_time"]):
        raise AgentStateError("checkpoint target identity does not match the retained session")
    return path, report


def recover_session(directory, session_id, output=None):
    record = _record(directory, session_id)
    source, checkpoint = assigned_checkpoint(directory, record)
    report = recovered_report(source, app_version=_version(), session_id=checkpoint["session_id"])
    if output is None:
        return report
    output = Path(output)
    if not output.is_dir():
        raise AgentStateError("recovery output directory must already exist")
    json_path = write_json_report(report, output)
    html_path = write_html_report(report, output)
    return {"partial": True, "final_outcome_confirmed": False,
            "json": str(json_path), "html": str(html_path)}


@contextmanager
def _stopped_lock(directory):
    """Open an existing lock without creating, resizing or replacing a file."""
    path = verify_trusted_path(Path(directory) / "agent-state.lock")
    with path.open("r+b", buffering=0) as handle:
        if os.fstat(handle.fileno()).st_size != 1:
            raise AgentStateError("cannot confirm an owned agent lock file")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise AgentStateError("stop the active agent before removing control files") from error
        if handle.read(2) != b"\0":
            raise AgentStateError("cannot confirm an owned agent lock file")
        yield


def _known_live(pid, created):
    if pid is None:
        return False
    try:
        process = psutil.Process(pid)
        if created is not None and process.create_time() != created:
            return False
        if os.name == "nt" and _windows_process_exited(pid):
            return False
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except (psutil.AccessDenied, OSError) as error:
        raise AgentStateError("known launch cleanup cannot be confirmed with current permissions") from error


def _confirm_no_owned_survivors(directory, records):
    checked, consumed = set(), 0
    for record in records:
        if record["mode"] != "launch":
            continue
        pending = record["state"] in {"accepted", "starting", "running", "stopping", "interrupted"}
        writer = record["writer_identity"]
        if pending and _known_live(writer["pid"], writer["create_time"]):
            raise AgentStateError("the former agent process still exists; cleanup is unconfirmed")
        identities = [(record["pid"], record["create_time"])]
        source = Path(directory) / "checkpoints" / record["checkpoint"]
        if source.exists():
            consumed += source.stat().st_size
            if consumed > 128 * 1024 * 1024:
                raise AgentStateError("cleanup inspection exceeds its aggregate checkpoint bound")
            _path, report = assigned_checkpoint(directory, record)
            summaries = report.get("processes") or {}
            if not isinstance(summaries, dict):
                raise AgentStateError("known process summaries have an unsupported shape")
            for group in ("live", "exited"):
                items = summaries.get(group, [])
                if not isinstance(items, list) or any(not isinstance(process, dict) for process in items):
                    raise AgentStateError("known process summaries have an unsupported shape")
                if len(items) > 512:
                    raise AgentStateError("cleanup inspection exceeds its known-process bound")
                identities.extend((process.get("pid"), process.get("create_time")) for process in items)
        for pid, created in identities:
            if pid is not None and (type(pid) is not int or not 1 <= pid < 2**32):
                raise AgentStateError("known process summary contains an invalid PID")
            if created is not None and (_duration(created) is None or created <= 0):
                raise AgentStateError("known process summary contains an invalid creation time")
            if pid is None or (pid, created) in checked:
                continue
            checked.add((pid, created))
            if len(checked) > 512:
                raise AgentStateError("cleanup inspection exceeds its known-process bound")
            if _known_live(pid, created):
                raise AgentStateError("a known launched process remains alive; cleanup must be resolved before removal")


def remove_control_files(directory, *, apply=False):
    directory = Path(directory).absolute()
    verify_private_path(directory)
    # Validate the credential's format/permissions but never serialize it.
    load_secret(directory)
    with _stopped_lock(directory):
        saved = _saved(directory)
        _confirm_no_owned_survivors(directory, saved["sessions"])
        planned = ["credential", "agent-state.json", "agent-state.lock"]
        result = {"applied": apply, "control_files": planned, "preserved": ["reports", "checkpoints", "unrelated files"]}
        if apply:
            # Explicit known files only. No recursive traversal or process control.
            (directory / "credential").unlink()
            (directory / "agent-state.json").unlink()
    if apply:
        (directory / "agent-state.lock").unlink()
    return result


def _enum(value, allowed, fallback="unknown"):
    return value if isinstance(value, str) and value in allowed else fallback


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _duration(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def support_document(diagnostics, reports=(), *, profile="minimal"):
    """Typed allowlists: no generic recursion through opaque/free-text values."""
    if profile not in PROFILES:
        raise ValueError("unsupported privacy profile")
    version = str(diagnostics.get("version", ""))
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version) or len(version) > 64:
        version = None
    safe = {"mode": _enum(diagnostics.get("mode"), {"live", "offline", "unavailable"}), "version": version,
            "python": ".".join(map(str, sys.version_info[:3]))}
    for name in ("responsive", "ready", "fresh"):
        safe[name] = diagnostics.get(name) if type(diagnostics.get(name)) is bool else None
    for name in ("active_sessions", "saved_active_sessions", "retained_records", "session_limit", "queue_depth"):
        safe[name] = _count(diagnostics.get(name))
    safe["storage_error_present"] = bool(diagnostics.get("storage_error"))
    safe["diagnostic_error_present"] = bool(diagnostics.get("error"))
    storage = diagnostics.get("storage")
    if isinstance(storage, dict):
        safe["storage"] = {name: _count(storage.get(name)) for name in ("bytes", "entries", "limit_bytes")}
        safe["storage"]["admission_available"] = storage.get("admission_available") if type(storage.get("admission_available")) is bool else None
    sessions = []
    for index, record in enumerate(diagnostics.get("sessions", ())):
        if index >= 256:
            raise AgentStateError("support diagnostics exceed their session bound")
        if not isinstance(record, dict):
            continue
        item = {"label": f"session-{index + 1}", "mode": _enum(record.get("mode"), {"attach", "launch"}),
                "state": _enum(record.get("state"), {"accepted", "starting", "running", "stopping", "completed", "failed", "interrupted"}),
                "sample_count": _count(record.get("sample_count")),
                "measurement": _enum(record.get("measurement"), {"conclusive", "partial", "inconclusive"}),
                "full_coverage": record.get("full_coverage") if type(record.get("full_coverage")) is bool else None}
        age = _duration(record.get("observation_age_seconds"))
        stamp = record.get("last_observed_at")
        if age is not None:
            item["observation_age_seconds"] = age
        elif _duration(stamp) is not None:
            item["observation_age_seconds"] = max(0, time.time() - stamp)
        result = record.get("result")
        if isinstance(result, dict):
            item["result"] = {"reason": _enum(result.get("reason"), {"cancelled", "timeout", "target_exited", "failed", "interrupted"}),
                              "startup": _enum(result.get("startup"), {"not_started", "started", "failed", "unknown"}),
                              "cleanup": _enum(result.get("cleanup"), {"not_attempted", "complete", "incomplete", "unknown"}),
                              "error_count": _count(result.get("error_count"))}
        sessions.append(item)
    safe["sessions"] = sessions
    transformed = [redact_report(report, profile) for report in reports]
    if len(transformed) > 2:
        raise AgentStateError("support bundles accept at most two explicit reports")
    document = {"support_schema": 1, "privacy": {"profile": profile, "review_required": True},
                "diagnostics": safe, "reports": transformed}
    if len(json.dumps(document, allow_nan=False).encode("utf-8")) > MAX_BUNDLE_CONTENT:
        raise AgentStateError("support preview exceeds its 16 MiB content bound")
    return document


def support_reports(directory, session_ids):
    if len(session_ids) > 2:
        raise AgentStateError("choose at most two reports for a support bundle")
    reports = []
    for session_id in session_ids:
        record = _record(directory, session_id)
        existing = next((name for name in record["reports"] if name.endswith(".json")), None)
        if existing is not None:
            path = verify_trusted_path(Path(directory) / "reports" / relative_report(existing))
            reports.append(read_report(path))
        else:
            reports.append(recover_session(directory, session_id))
    return reports


def write_support_bundle(document, destination):
    """Exclusive local output; every archive member comes from the preview."""
    files = {"support.json": json.dumps({**document, "reports": []}, indent=2, allow_nan=False).encode("utf-8")}
    for index, report in enumerate(document["reports"], start=1):
        files[f"report-{index}.json"] = json.dumps(report, indent=2, allow_nan=False).encode("utf-8")
        files[f"report-{index}.html"] = render_html_report(report).encode("utf-8")
    if sum(map(len, files.values())) > MAX_BUNDLE_CONTENT:
        raise AgentStateError("support archive exceeds its 16 MiB content bound")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    if buffer.tell() > MAX_BUNDLE_BYTES:
        raise AgentStateError("support archive exceeds its 8 MiB compressed bound")
    destination = verify_trusted_path(destination)
    descriptor, filename = tempfile.mkstemp(prefix=".agent-support-", suffix=".tmp", dir=destination.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(buffer.getvalue())
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic exclusive publication: hard-link creation refuses an existing
        # destination. The temporary is always this operation's own private file.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(destination), "members": list(files), "privacy": document["privacy"], "bytes": buffer.tell()}
