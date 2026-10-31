"""Implementation of the pinned Windows composite action, never a runtime module."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import venv
from pathlib import Path

MAX_LOG = 2 * 1024 * 1024
_JOB_HANDLE = None


def _contain_children():
    """Fence this helper and descendants until process exit, including hard timeouts."""
    global _JOB_HANDLE
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                    ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes, api.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.GetCurrentProcess.restype = wintypes.HANDLE
    _JOB_HANDLE = api.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; handle is never inherited.
    if (not _JOB_HANDLE or not api.SetInformationJobObject(_JOB_HANDLE, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not api.AssignProcessToJobObject(_JOB_HANDLE, api.GetCurrentProcess())):
        raise OSError("Could not contain this CI helper's owned descendants")
    # Deliberately retain the handle until helper process exit: closing a job
    # containing this helper would terminate it before artifacts/outputs flush.


def _output(name, value):
    value = str(value)
    if any(character in value for character in "\r\n\0"):
        raise ValueError("Invalid action output")
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with Path(destination).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def _run(arguments, logfile, *, timeout):
    """Drain after the log cap, while preserving the real exit code."""
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def drain():
        written = 0
        with logfile.open("wb") as handle:
            while chunk := process.stdout.read(16384):
                retained = chunk[:max(0, MAX_LOG - written)]
                if retained:
                    handle.write(retained)
                    sys.stdout.buffer.write(retained)
                    sys.stdout.buffer.flush()
                    written += len(retained)
            if written >= MAX_LOG:
                handle.write(b"\n[CI command log capped at 2 MiB; remaining output drained]\n")

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        code = 2
    finally:
        reader.join(2)
    return code


def main():
    temporary_parent = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    root = Path(tempfile.mkdtemp(prefix="draintracer-ci-", dir=temporary_parent))
    evidence = root / "evidence"
    evidence.mkdir()
    _output("report-directory", evidence)
    result = {"ci_schema": 1, "run_exit": 2, "comparison_exit": None, "exit_code": 2, "report_file": None}
    try:
        if os.name != "nt":
            raise ValueError("This action requires a Windows runner")
        _contain_children()
        version = os.environ.get("DRAIN_VERSION", "")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError("Use an exact package MAJOR.MINOR.PATCH version")
        recipe = Path(os.environ.get("DRAIN_RECIPE", "")).resolve(strict=True)
        if not recipe.is_file() or recipe.stat().st_size > 65536:
            raise ValueError("Use a bounded recipe file")
        environment = root / "environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = str(environment / "Scripts" / "python.exe")
        wheel = os.environ.get("DRAIN_WHEEL", "")
        package = str(Path(wheel).resolve(strict=True)) if wheel else f"draintracer=={version}"
        if wheel and (not Path(package).is_file() or Path(package).suffix != ".whl"):
            raise ValueError("Candidate input must be a local wheel")
        code = _run([python, "-I", "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
                     "--index-url", "https://pypi.org/simple", package], evidence / "install.log", timeout=300)
        if code:
            raise RuntimeError("Exact-version installation failed")
        probe = ("import json,sys,importlib.metadata; from draintracer.workload import read_recipe; "
                 "r=read_recipe(sys.argv[1]); "
                 "assert importlib.metadata.version('draintracer')==sys.argv[2], 'Version mismatch'; "
                 "assert r.timeout<=300, 'Recipe exceeds five minutes'; "
                 "assert all((r.settings.fail_on_inconclusive,r.settings.fail_on_target_error,"
                 "r.settings.require_full_coverage)), 'Enable all strict outcome policies'; "
                 "print(json.dumps({'timeout':r.timeout}))")
        if _run([python, "-I", "-c", probe, str(recipe), version], evidence / "policy.json", timeout=30):
            raise ValueError("Recipe or version policy check failed")
        policy = json.loads((evidence / "policy.json").read_text(encoding="utf-8"))
        result["run_exit"] = _run([python, "-I", "-m", "draintracer.workload", str(recipe), "--no-color",
                                   "--report-dir", str(evidence / "reports")], evidence / "workload.log",
                                  timeout=policy["timeout"] + 120)
        files = list((evidence / "reports").rglob("*.json"))
        if len(files) == 1:
            report = files[0]
            result["report_file"] = str(report)
            if _run([python, "-I", "-m", "draintracer.report_contract", "summary", str(report)],
                    evidence / "summary.json", timeout=30):
                result["run_exit"] = result["run_exit"] or 2
            baseline = os.environ.get("DRAIN_BASELINE", "")
            if baseline:
                baseline_path = Path(baseline).resolve(strict=True)
                result["comparison_exit"] = _run(
                    [python, "-I", "-m", "draintracer", "diff", str(baseline_path), str(report),
                     "--strict", "--fail-on-regression", "--json", "--no-color"],
                    evidence / "comparison.json", timeout=60)
                comparison = json.loads((evidence / "comparison.json").read_text(encoding="utf-8"))
                if destination := os.environ.get("GITHUB_STEP_SUMMARY"):
                    with Path(destination).open("a", encoding="utf-8") as handle:
                        handle.write("\n### DrainTracer strict comparison\n\n")
                        handle.write(f"Comparability: **{comparison['comparability']}**; verdict: **{comparison['verdict']}**.\n\n")
                        for name in ("memory", "handles", "threads"):
                            resource = comparison["resources"][name]
                            handle.write(f"- {name}: {resource['signal']}; drift {resource['drift_before_per_second']} → "
                                         f"{resource['drift_after_per_second']} {resource['metric']}/second.\n")
                        handle.write("\nReview all qualifications in the retained comparison JSON. No baseline was updated.\n")
        elif result["run_exit"] == 0:
            result["run_exit"] = 2
        result["exit_code"] = result["run_exit"] or result["comparison_exit"] or 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError) as error:
        # Configuration/OS exceptions can include caller paths; retain a type,
        # not arbitrary exception text or input values, in the machine record.
        result["error"] = type(error).__name__
        result["exit_code"] = 2
    finally:
        (evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        for name, key in (("run-exit", "run_exit"), ("comparison-exit", "comparison_exit"),
                          ("exit-code", "exit_code"), ("report-file", "report_file")):
            _output(name, "" if result[key] is None else result[key])
    return 0  # The composite enforces the recorded code after artifact upload.


if __name__ == "__main__":
    raise SystemExit(main())
