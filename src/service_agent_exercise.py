"""Native agent acceptance and abrupt-exit recovery using only owned fixtures."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from .service_operations import ExerciseEvidence, ResourceSampler


def _wait(predicate, seconds):
    deadline = time.monotonic() + seconds
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError("Operating fixture exceeded its bounded wait")
        time.sleep(0.02)


def exercise_agent(root: Path, evidence: ExerciseEvidence, sampler: ResourceSampler) -> bool:
    if os.name != "nt":
        return False
    from .agent_command import AgentClient
    from .agent_diagnostics import inspect_agent, recover_session
    from .api import _version
    from .checkpoints import read_checkpoint
    from .process_runner import _windows_process_exited
    from .report_contract import read_report

    state, script = root / "agent", root / "owned-workload.py"
    script.write_text("import sys,time\nfrom pathlib import Path\npayload=bytearray(2*1024*1024)\n"
                      "if len(sys.argv)>2:\n"
                      "    gate=Path(sys.argv[2]); deadline=time.monotonic()+30\n"
                      "    while not gate.exists():\n"
                      "        if time.monotonic()>=deadline: raise SystemExit(70)\n"
                      "        time.sleep(.02)\n"
                      "time.sleep(float(sys.argv[1]))\n", encoding="ascii")
    profile = "ops-" + uuid4().hex[:20]
    # Windows venv python.exe may be a redirector. Use the base interpreter
    # with this installed package's parent on sys.path so the Popen handle is
    # the actual agent, even in a clean wheel venv. All imports stay local.
    package = __package__
    bootstrap = ("import sys;sys.path.insert(0,sys.argv.pop(1));"
                 f"from {package}.agent_command import main;raise SystemExit(main())")
    process = subprocess.Popen([getattr(sys, "_base_executable", None) or sys.executable, "-c", bootstrap,
                                str(Path(__file__).resolve().parent.parent), "--state-dir", str(state),
                                "--profile", profile, "serve", "--allow-launch", "--launch-root", str(root),
                                "--max-timeout", "60"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    client = None
    crashed = False
    accepted, completed, durable = 0, 0, 0
    try:
        sampler.add("agent", process.pid)
        def connect_ready():
            if process.poll() is not None:
                raise RuntimeError("Owned reference agent exited before readiness")
            try:
                candidate = AgentClient(state, profile=profile, timeout=0.3)
                if candidate.request("diagnostics")["ready"]:
                    return True
            except (OSError, ValueError, RuntimeError):
                pass
            return False

        ready = evidence.probe("agent_ready", lambda: _wait(connect_ready, 10), timing="readiness")
        if ready is None:
            return False
        # A short startup probe must not become the operation deadline.
        # The single pipe can still be finishing/rearming after its reply;
        # subsequent admission/control uses the normal bounded client contract.
        client = AgentClient(state, profile=profile)

        def status(identity):
            return client.request("status", {"session_id": identity})

        def start(seconds, gate=None):
            arguments = [str(seconds)] + ([str(gate)] if gate is not None else [])
            return client.request("start", {"mode": "launch", "target": str(script), "args": arguments,
                                             "timeout_seconds": 45, "sample_interval_ms": 100,
                                             "windows_seconds": [1]})["session_id"]

        def sampled(identity):
            current = status(identity)
            if current["result"] is not None:
                raise RuntimeError("Owned reference workload finished before its sample check")
            if current["sample_count"] >= 3:
                return current
            return None

        for cancel in (False, True):
            # The natural fixture begins its unchanged work duration only
            # after enough live observations. A late client cannot turn
            # startup/sampling latency into an insufficient short recording.
            gate = None if cancel else root / "release-natural-workload"
            identity = start(30 if cancel else 1.3, gate)
            accepted += 1
            current = _wait(lambda identity=identity: sampled(identity), 10)
            age = current["observation_age_seconds"]
            evidence.timings.setdefault("freshness", []).append(age)
            evidence.check("agent_sample_observation_is_fresh", age >= 0)
            if gate is not None:
                gate.touch(exist_ok=False)

            def finish(cancel=cancel, identity=identity):
                if cancel:
                    client.request("cancel", {"session_id": identity})
                return _wait(lambda: (current if (current := status(identity))["result"] is not None else None),
                             evidence.budgets.cleanup_seconds)

            final = evidence.probe("agent_cancel_completed" if cancel else "agent_natural_run_completed", finish,
                                   timing="cleanup" if cancel else None)
            success = final is not None and final["result"]["startup"] == "started" and final["result"]["cleanup"] == "complete"
            success = (success and final["result"]["error_count"] == 0
                       and (cancel or (final["result"]["reason"] == "target_exited"
                                       and final["result"]["target_exit_code"] == 0)))
            completed += int(success)
            evidence.check("owned_workload_cleanup_confirmed", success)
            persisted = False
            if final is not None:
                json_paths = [state / "reports" / name for name in final["reports"] if name.endswith(".json")]
                html_paths = [state / "reports" / name for name in final["reports"] if name.endswith(".html")]
                persisted = bool(json_paths and html_paths) and all(path.is_file() for path in (*json_paths, *html_paths))
                for path in json_paths:
                    read_report(path)
            durable += int(persisted)
            evidence.check("completed_agent_reports_readable", persisted)
        evidence.rate("agent_completed_runs", completed, accepted)
        evidence.rate("agent_report_persistence", durable, accepted)

        # A separate deliberate failure is excluded from baseline completion
        # counts. Kill only the original owned agent handle, which closes its
        # launch job; recover the matching prefix without reopening any PID.
        identity = start(30)
        current = _wait(lambda: sampled(identity), 10)
        target_pid = current["target_identity"]["pid"]
        checkpoint = state / "checkpoints" / (identity + ".json")

        def checkpoint_ready():
            try:
                return bool(read_checkpoint(checkpoint, app_version=_version(), session_id=identity)["snapshots"])
            except (OSError, ValueError):
                return False

        _wait(checkpoint_ready, 10)
        process.kill()
        process.wait(timeout=10)
        crashed = True
        evidence.probe("crashed_agent_owned_job_stops", lambda: _wait(lambda: _windows_process_exited(target_pid), 10),
                       timing="cleanup")
        offline = inspect_agent(state)
        evidence.check("crashed_agent_detected_offline", not offline["ready"] and not offline["responsive"])
        prefix = recover_session(state, identity)
        recovery = prefix["recovery"]
        evidence.check("crash_prefix_preserves_partial_qualification", recovery["partial"]
                       and recovery["exact_through_cutoff"] and not recovery["final_outcome_confirmed"])
        evidence.check("offline_recovery_does_not_restart_agent", process.poll() is not None)
        return completed == accepted == durable == 2
    finally:
        if process.poll() is None:
            if client is not None:
                try:
                    client.request("shutdown")
                    process.wait(timeout=20)
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        evidence.check("agent_driver_handle_reaped", process.poll() is not None)
        if not crashed:
            evidence.check("agent_exercise_reached_crash_recovery", False)


__all__ = ["exercise_agent"]
