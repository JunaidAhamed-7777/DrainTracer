"""Run bounded native observer failure drills and deterministic identity controls."""

from __future__ import annotations

import argparse
import errno
import io
import platform
import subprocess
import sys
import time
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psutil
from calibrate import ROOT, _DeniedMemory, save, source_record
from rich.console import Console

from src.config import MonitorSettings
from src.events import MetricsStream
from src.headless import HeadlessRunner
from src.process_runner import _windows_process_exited
from src.report_contract import read_report

PYTHON = getattr(sys, "_base_executable", None) or sys.executable


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def runner(directory, *, fixture="steady.py", arguments=(), duration=5):
    return HeadlessRunner(PYTHON,
        args=[str(ROOT / "torture" / fixture), *arguments, "--duration", "15"],
        console=Console(file=io.StringIO()), report_dir=directory, timeout=duration,
        settings=MonitorSettings(sample_interval_ms=200, windows=(2,), timeout=duration))


def cleanup_verified(session):
    require(not session.manager.has_tracked_processes, "Manager retained owned identities")
    identities = {(process.pid, process.create_time) for sample in session._snapshot_history for process in sample.processes}
    for pid, created in identities:
        try:
            process = psutil.Process(pid)
            require(created is not None, "Unknown original identity cannot prove cleanup")
            if process.create_time() == created:
                exited = (_windows_process_exited(pid) if sys.platform == "win32"
                          else process.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD})
                require(exited, "An observed owned identity is still alive")
        except psutil.NoSuchProcess:
            pass
    return len(identities)


def run_owned(session, context=None):
    try:
        with context or nullcontext():
            code = session.run()
    finally:
        if session.manager.has_tracked_processes:
            session.manager.kill()
    count = cleanup_verified(session)
    return code, count


def short_parent(directory):
    session = HeadlessRunner(PYTHON,
        args=[str(ROOT / "torture/parent_exits.py"), "--parent-delay", "1.5", "--child-duration", "12"],
        console=Console(file=io.StringIO()), report_dir=directory, timeout=4,
        settings=MonitorSettings(sample_interval_ms=200, windows=(2,), timeout=4))
    code, count = run_owned(session)
    samples = list(session._snapshot_history)
    root = session._root_pid
    surviving = [sample for sample in samples if not sample.process_alive
                 and any(process.pid != root for process in sample.processes)]
    require(code == 0 and count >= 2 and surviving, "Surviving child was not observed after root exit")
    return {"kind": "native", "exit_code": code, "observed_identity_count": count,
            "surviving_child_samples_after_root_exit": len(surviving), "owned_cleanup_complete": True}


def access_recovery(directory):
    session = runner(directory, fixture="process_tree.py", arguments=("--depth", "1", "--fanout", "1", "--memory-mb", "1"), duration=6)
    original = session.manager.iter_psutil_processes
    started = time.monotonic()

    def readable():
        denying = 0.8 <= time.monotonic() - started < 2.4
        return [_DeniedMemory(process) if denying and process.pid == session.manager.root_pid else process
                for process in original()]

    code, count = run_owned(session, mock.patch.object(session.manager, "iter_psutil_processes", side_effect=readable))
    samples = list(session._snapshot_history)
    partial = [sample for sample in samples if sample.coverage["private_bytes"].partial]
    require(partial, "Injected read denial was not observed")
    recovered = [sample for sample in samples if sample.timestamp > partial[-1].timestamp
                 and not sample.coverage["private_bytes"].partial
                 and not sample.coverage["private_bytes"].unavailable]
    require(recovered, "Full access did not recover")
    require(not any(window.judged for item in recovered[0].evidence for window in item.windows),
            "Restored coverage reused a stale detection window")
    require(any(window.judged for item in recovered[-1].evidence for window in item.windows),
            "Fresh windows did not refill after access recovery")
    require(code == 0, "Access recovery caused an unexpected exit")
    return {"kind": "native_sampling_with_injected_root_memory_AccessDenied", "partial_samples": len(partial),
            "recovered_full_samples": len(recovered), "fresh_windows_refilled": True,
            "owned_cleanup_complete": True, "observed_identity_count": count}


def timing(directory, *, clock_steps):
    session = runner(directory, duration=5)
    started = time.monotonic()
    if clock_steps:
        def wall():
            return time.time() + (600 if 0.8 <= time.monotonic() - started < 1.8 else 0)
        context = mock.patch("src.leak_detector.time", SimpleNamespace(
            time=wall, monotonic=time.monotonic, perf_counter=time.perf_counter))
    else:
        original = session.detector._sample_processes
        stalled = False

        def sample():
            nonlocal stalled
            if not stalled and time.monotonic() - started >= 0.8:
                stalled = True
                time.sleep(1.2)
            return original()
        context = mock.patch.object(session.detector, "_sample_processes", side_effect=sample)
    code, count = run_owned(session, context)
    report = read_report(session._last_report)
    key = "clock_adjustments" if clock_steps else "sampling_gaps"
    resets = [sample for sample in session._snapshot_history if sample.metadata.get("timing_reset")]
    require(report["session_facts"]["monitor"][key] >= (2 if clock_steps else 1), "Timing reset was not recorded")
    require(resets and all(not window.judged for sample in resets for item in sample.evidence for window in item.windows),
            "A reset retained a judged window")
    require(code == 0 and not session.breached, "A timing fault manufactured a leak failure")
    return {"kind": "native_sampling_with_injected_wall_steps" if clock_steps else "native_sampling_with_injected_collector_stall",
            "clock_adjustments": report["session_facts"]["monitor"]["clock_adjustments"],
            "sampling_gaps": report["session_facts"]["monitor"]["sampling_gaps"],
            "windows_restarted": True, "physical_sleep_or_OS_clock_changed": False,
            "owned_cleanup_complete": True, "observed_identity_count": count}


def slow_consumer(directory):
    session = runner(directory)
    session.detector.stream = MetricsStream(max_queue_size=4, facts=session.facts)
    published = []
    session.detector.stream.subscribe(lambda sample: published.append(sample.totals["private_bytes"]))
    try:
        session.start()
        time.sleep(3)
        queued = session.detector.stream._queue.qsize()
        before = session.facts.to_dict()
        require(queued <= 4 and session.detector.stream.dropped > 0, "Consumer queue did not remain bounded while dropping")
        require(before["samples"]["taken"] == len(published) + 1, "Producer facts lost an evicted sample")
        require(before["peaks"]["private_bytes"]["value"] >= max(published), "Eviction lost the producer peak")
        session.poll()
    finally:
        session.shutdown()
        session.poll()
    count = cleanup_verified(session)
    session._export()
    saved = read_report(session._last_report)
    return {"kind": "native_sampler_with_three_second_consumer_stall", "queue_capacity": 4,
            "queued_at_stall_end": queued, "producer_samples_before_poll": before["samples"]["taken"],
            "dropped_snapshots": saved["session_facts"]["monitor"]["dropped_snapshots"],
            "exact_producer_count_and_peak_preserved": True,
            "owned_cleanup_complete": True, "observed_identity_count": count}


def export_failure(directory, fault):
    session = runner(directory, duration=0.8)
    if fault == "disk_full":
        context = mock.patch("src.report.os.replace", side_effect=OSError(errno.ENOSPC, "Injected full disk"))
    elif fault == "interrupt_after_json":
        context = mock.patch("src.session.write_html_report", side_effect=KeyboardInterrupt)
    else:
        context = mock.patch("src.session.write_json_report", side_effect=KeyboardInterrupt)
    code, count = run_owned(session, context)
    require(code == (2 if fault == "disk_full" else 130), "Export failure exit policy was lost")
    require(not list(directory.rglob("*.tmp")), "An atomic export temporary survived failure")
    require(session.facts.to_dict()["samples"]["taken"] > 0, "Export failure discarded in-memory evidence")
    if fault == "interrupt_after_json":
        saved = read_report(session._last_report)
        require(saved["report_id"] == session._last_report_data["report_id"], "Corrected JSON changed report identity")
        require("HTML report interrupted" in saved["outcome"]["monitor_errors"], "JSON hid HTML interruption")
        require(saved["headless"]["exit_code"] == 130, "Saved JSON hid interruption status")
    else:
        require(session._last_report is None, "A failed initial export claimed a saved report")
    return {"kind": "native_target_with_injected_export_fault", "fault": fault, "exit_code": code,
            "temporary_artifacts_remaining": 0, "in_memory_evidence_retained": True,
            "corrected_same_id_json": fault == "interrupt_after_json",
            "owned_cleanup_complete": True, "observed_identity_count": count}


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def attached_shutdown(directory):
    # The original Popen object belongs to this fixture; monitor cleanup is
    # detach-only. Its owner cleans the same unreaped process afterward.
    child = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(15)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session = None
    try:
        process = psutil.Process(child.pid)
        created = process.create_time()
        session = HeadlessRunner(None, attach_pid=child.pid,
                                 console=Console(file=io.StringIO()), report_dir=directory)
        session.attach_create_time = created
        session.start()
        require(session.manager.pause(), "Explicit attached pause failed")
        require(wait_for(lambda: process.status() == psutil.STATUS_STOPPED), "Attached fixture was not suspended")
        session.shutdown()
        require(child.poll() is None and process.create_time() == created, "Ordinary shutdown ended or replaced attached fixture")
        require(wait_for(lambda: process.status() != psutil.STATUS_STOPPED), "Ordinary shutdown did not resume attached fixture")
        require(not session.manager.has_tracked_processes, "Successful detach retained an identity")
        return {"kind": "native_external_target_explicit_pause_then_ordinary_shutdown",
                "same_identity_alive_after_detach": True, "resumed_after_detach": True,
                "monitor_terminated_attached_target": False}
    finally:
        try:
            if session is not None and session.manager.has_tracked_processes:
                session.manager.detach()
        finally:
            child.kill()
            child.wait(timeout=5)


def identity_controls(_directory):
    sys.path.insert(0, str(ROOT / "tests"))
    names = ["test_process_attach.PidReuseTests", "test_process_attach.UnverifiedControlTests",
             "test_linux_processes.ProcessCleanupTests.test_repeated_opens_never_adopt_a_reused_descendant_pid",
             "test_linux_processes.ProcessCleanupTests.test_cleanup_does_not_adopt_a_replacement_after_refresh_retires_its_pid",
             "test_linux_processes.ProcessCleanupTests.test_retired_descendants_require_validated_discovery_before_reopening",
             "test_process_attach.WindowsLivenessTests.test_close_but_distinct_windows_identities_never_reach_process_controls"]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    require(result.wasSuccessful(), "Deterministic identity controls failed")
    return {"kind": "deterministic_identity_regressions_not_forced_kernel_PID_reuse", "tests": result.testsRun,
            "skipped": len(result.skipped), "failures": len(result.failures), "errors": len(result.errors),
            "test_selections": names}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("refusing to overwrite an existing stress record")
    source = source_record()
    if source["tracked_changes"]:
        parser.error("commit tracked changes before recording stress evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    record = {"observer_stress_schema": 1, "state": "running", "source": source,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"os": platform.system(), "version": platform.version(), "release": platform.release(),
                              "python": platform.python_version(), "psutil": version("psutil"), "rich": version("rich")},
              "cases": []}
    cases = [("identity_controls", identity_controls), ("short_lived_parent", short_parent),
             ("access_loss_and_recovery", access_recovery), ("collector_stall", lambda path: timing(path, clock_steps=False)),
             ("forward_backward_wall_steps", lambda path: timing(path, clock_steps=True)),
             ("slow_consumer", slow_consumer), ("disk_full", lambda path: export_failure(path, "disk_full")),
             ("interrupt_after_json", lambda path: export_failure(path, "interrupt_after_json")),
             ("interrupt_before_json", lambda path: export_failure(path, "interrupt_before_json")),
             ("attached_pause_shutdown", attached_shutdown)]
    save(args.output, record)
    try:
        for name, function in cases:
            record["active_case"] = name
            save(args.output, record)
            print(f"START {name}", flush=True)
            result = function(args.output.parent / (args.output.stem + "-reports") / name)
            record["cases"].append({"name": name, "state": "passed", **result})
            save(args.output, record)
            print(f"DONE {name}", flush=True)
        record["state"] = "complete"
        record["active_case"] = None
    except KeyboardInterrupt:
        record["state"] = "interrupted"
    except Exception as error:  # noqa: BLE001 - keep failed evidence and stop, never retry until green.
        record["state"] = "failed"
        record["error_type"] = type(error).__name__
        print(f"FAILED {record['active_case']}: {type(error).__name__}: {error}", flush=True)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(args.output, record)
    return 0 if record["state"] == "complete" else 130 if record["state"] == "interrupted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
