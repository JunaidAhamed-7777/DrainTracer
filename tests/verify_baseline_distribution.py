"""Exercise installed baseline commands with four independently monitored runs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def verify_baseline_runtime(python: Path, cwd: Path, environment: dict[str, str], source_commit: str) -> None:
    target = cwd / "baseline_steady.py"
    target.write_text("import time\ntime.sleep(20)\n", encoding="utf-8")
    reports = cwd / "baseline-runs"
    paths = []
    for _index in range(4):
        before = set(reports.rglob("*.json"))
        command = [str(python), "-m", "draintracer", "--headless", "--target", str(target),
                   "--interval", "100ms", "--window", "1s", "--warmup", "2s", "--timeout", "4s",
                   "--report-dir", str(reports), "--workload-id", "baseline-distribution-steady",
                   "--build", source_commit, "--no-config", "--fail-on-inconclusive", "--require-full-coverage"]
        result = subprocess.run(command, check=False, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if result.returncode:
            raise RuntimeError(f"Independent baseline run failed ({result.returncode}): {result.stdout}{result.stderr}")
        added = set(reports.rglob("*.json")) - before
        if len(added) != 1:
            raise RuntimeError("Each independent baseline run must export exactly one new report")
        paths.append(added.pop())
    identifiers = [json.loads(path.read_text(encoding="utf-8"))["report_id"] for path in paths]
    if len(set(identifiers)) != 4:
        raise RuntimeError("Baseline smoke requires independent recorded report identities")
    store = cwd / "reviewed-baseline"
    def run(arguments):
        return subprocess.run([str(python), "-m", "draintracer.baseline_control", *arguments], check=False, cwd=cwd,
                              env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    approval = run(["promote", str(store), "--run", str(paths[0]), "--run", str(paths[1]),
                    "--approved-by", "distribution-smoke", "--reason", "Review independent steady fixture runs",
                    "--source-commit", source_commit])
    if approval.returncode or json.loads(approval.stdout).get("revision") != 1:
        raise RuntimeError(f"Installed baseline promotion failed: {approval.stdout}{approval.stderr}")
    checked = run(["check", str(store), "--run", str(paths[2]), "--run", str(paths[3]),
                   "--source-commit", source_commit])
    if checked.returncode not in (0, 3):
        raise RuntimeError(f"Installed baseline check failed unexpectedly: {checked.stdout}{checked.stderr}")
    comparison = json.loads(checked.stdout)["comparison"]
    if comparison["caveats"] or comparison["comparability"] != "comparable":
        raise RuntimeError("Real baseline runs unexpectedly lost provenance or full measurement coverage")
    if checked.returncode == 3 and comparison["measured_verdict"] != "inconclusive":
        raise RuntimeError("Only actual observed overlap may qualify the real steady comparison")
    audited = run(["audit", str(store)])
    if audited.returncode or len(json.loads(audited.stdout)["audit"]) != 1:
        raise RuntimeError(f"Installed baseline audit failed: {audited.stdout}{audited.stderr}")
