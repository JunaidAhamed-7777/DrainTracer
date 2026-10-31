"""Preserved installed W4 -> approval -> comparison -> viewer proof; synthetic credentials only.

Run with an installed wheel interpreter. The output must be a NEW private path.
Every command result, including accepted nonzero W4 outcomes, is retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from draintracer.team_security import secure_directory


class Evidence:
    def __init__(self, root):
        self.root, self.commands = root, []

    def run(self, arguments, *, codes=(0,), timeout=120):
        result = self.observe(list(map(str, arguments)), cwd=self.root, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout, check=False)
        self.commands[-1]["accepted_codes"] = list(codes)
        self.save()
        if result.returncode not in codes:
            raise RuntimeError(f"Unexpected result retained in {self.commands[-1]['id']}")
        return result.returncode, json.loads(result.stdout) if result.stdout.strip() else None

    def observe(self, arguments, **kwargs):
        identity = f"command-{len(self.commands):03d}"
        started = time.monotonic()
        try:
            result = subprocess.run(arguments, **kwargs)
        except subprocess.TimeoutExpired as error:
            result = SimpleNamespace(returncode=None, stdout=error.stdout or b"", stderr=error.stderr or b"")
            self.record(identity, arguments, result, started)
            raise
        self.record(identity, arguments, result, started)
        return result

    def record(self, identity, arguments, result, started):
        for stream in ("stdout", "stderr"):
            raw = getattr(result, stream)
            (self.root / f"{identity}.{stream}").write_bytes(raw.encode("utf-8") if isinstance(raw, str) else raw)
        self.commands.append({"id": identity, "arguments": list(map(str, arguments)), "exit_code": result.returncode,
                              "accepted_codes": None, "elapsed_seconds": time.monotonic() - started})
        self.save()

    def save(self, **fields):
        path = self.root / "proof.json"
        value = json.loads(path.read_bytes()) if path.exists() else {}
        value.update(fields)
        value["commands"] = self.commands
        path.write_text(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False), encoding="ascii")


def verify(output, fixtures, source_commit):
    if output.exists() or output.is_symlink():
        raise ValueError("Proof requires a new output path")
    root = secure_directory(output, create=True)
    evidence = Evidence(root)
    evidence.save(proof_schema=1, source_commit=source_commit, status="running",
                  started_at=datetime.now(timezone.utc).isoformat(),
                  machine={"platform": platform.platform(), "python": sys.version,
                           "machine_hash": hashlib.sha256(platform.node().encode()).hexdigest(),
                           "processor": platform.processor(), "cpu_count": os.cpu_count()},
                  qualifications=["Disposable local technical credentials, not human participants or consent",
                                  "Client-computed receipts; the server did not recompute private measurements",
                                  "All command outcomes and raw private report evidence are retained"])
    python = Path(sys.executable)
    scripts = python.parent
    def command(name):
        return scripts / (name + ".exe" if os.name == "nt" else name)
    team, share, ops = map(command, ("draintracer-team", "draintracer-share", "draintracer-ops"))
    server = None
    logs = []
    try:
        # Keep four real independent clean runs plus the explicit environment
        # refusals from the already-reviewed installed controlled-workload proof.
        import verify_controlled_workload as recipes
        subprocess_module = recipes.subprocess
        recipes.subprocess = SimpleNamespace(run=evidence.observe)
        try:
            quality = recipes.verify_controlled_workload(python, root, os.environ.copy(), source_commit)
        finally:
            recipes.subprocess = subprocess_module
        evidence.save(controlled_workloads=quality)
        groups = [next((root / f"controlled-clean-{index}").rglob("*.json")) for index in range(4)]
        baseline = root / "controlled-recipe-baseline"
        certificates = secure_directory(root / "certificates", create=True)
        for name in ("cert.pem", "key.pem"):
            path = certificates / name
            path.write_bytes((fixtures / name).read_bytes())
            if os.name != "nt":
                path.chmod(0o600)
        cert, key = certificates / "cert.pem", certificates / "key.pem"
        _code, ops_receipt = evidence.run([ops, "--workspace", root / "operating-baseline", "--operator", "Synthetic reference owner",
                      "--contact", "Local verification only", "--readiness-seconds", "10"])
        operating = Path(ops_receipt["evidence"])
        if not ops_receipt["operating_prerequisites_met"]:
            raise RuntimeError("Actual operating baseline failed")
        # This second retained artifact exercises actual lost-ACK replay,
        # endpoint outage/restart and local cleanup during the outage.
        _code, extension = evidence.run([ops, "--workspace", root / "operating-transport", "--operator", "Synthetic reference owner",
                      "--contact", "Local verification only", "--readiness-seconds", "10", "--with-transport",
                      "--certificate", cert, "--key", key, "--ca", cert])
        if not extension["operating_prerequisites_met"]:
            raise RuntimeError("Actual transport recovery exercise failed")
        store, admin = root / "authority", root / "admin.json"
        evidence.run([team, "--store", store, "init"])
        evidence.run([team, "--store", store, "bootstrap", "synthetic-sharing", "--credential-output", admin])
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        # A Windows venv redirector must not become the process we terminate.
        # Its base interpreter sees the installed package's site-packages.
        import draintracer
        interpreter = (getattr(sys, "_base_executable", None) or sys.executable) if os.name == "nt" else sys.executable
        bootstrap = "import sys;sys.path.insert(0,sys.argv.pop(1));from draintracer.share_command import main;raise SystemExit(main())"
        logs = [(root / "foreground.stdout").open("xb"), (root / "foreground.stderr").open("xb")]
        server = subprocess.Popen([interpreter, "-c", bootstrap, str(Path(draintracer.__file__).parent.parent),
                    "serve", "--store", str(store), "--certificate", str(cert), "--key", str(key),
                    "--operating-evidence", str(operating), "--port", str(port)],
                    stdin=subprocess.DEVNULL, stdout=logs[0], stderr=logs[1],
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 15
        while not (root / "foreground.stdout").read_bytes().strip():
            if server.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Owned foreground service failed readiness; logs retained")
            time.sleep(0.02)
        if not json.loads((root / "foreground.stdout").read_bytes())["listening"]:
            raise RuntimeError("Owned foreground service did not confirm readiness")
        endpoint = ["--endpoint", f"tls://localhost:{port}", "--ca", cert]
        admin_args = [share, *endpoint, "--credentials", admin]
        operator, viewer = root / "operator.json", root / "viewer.json"
        evidence.run([*admin_args, "issue-credential", "operator", "--output", operator])
        evidence.run([*admin_args, "issue-credential", "viewer", "--output", viewer])
        approval_request = uuid4().hex
        approval_args = [*admin_args, "approve-baseline", baseline, "--repository", "synthetic/reference",
                         "--request-id", approval_request]
        _code, approval = evidence.run(approval_args)
        _code, replay = evidence.run(approval_args)
        if approval["record_id"] != replay["record_id"]:
            raise RuntimeError("Uncertain approval retry created another approval")
        receipt_path = root / "public-receipt.json"
        check_code, _prepared = evidence.run([share, "prepare-comparison", baseline, "--run", groups[2], "--run", groups[3],
                            "--source-commit", source_commit, "--repository", "synthetic/reference",
                            "--approval-id", approval["record_id"], "--output", receipt_path], codes=(0, 3))
        prepared = json.loads(receipt_path.read_bytes())
        if prepared["outcome"] != ("pass" if check_code == 0 else "inconclusive"):
            raise RuntimeError("Local W4 check exit meaning changed in receipt projection")
        operator_args = [share, *endpoint, "--credentials", operator]
        viewer_args = [share, *endpoint, "--credentials", viewer]
        upload_queue = root / "upload-queue"
        for path in groups[2:]:
            evidence.run([*operator_args, "enqueue-report", "--queue", upload_queue, path])
        evidence.run([*operator_args, "flush", "--queue", upload_queue])
        _code, reports = evidence.run([*viewer_args, "history"])
        report_ids = [item["report_id"] for item in reports["reports"]]
        if len(report_ids) != 2:
            raise RuntimeError("Two sanitized run reports were not committed")
        enqueue = [*operator_args, "enqueue-comparison", "--queue", upload_queue, receipt_path]
        for identity in report_ids:
            enqueue.extend(("--report-id", identity))
        evidence.run(enqueue)
        evidence.run([*operator_args, "flush", "--queue", upload_queue])
        filters = ["history", "--repository", prepared["repository"], "--source-commit", prepared["source_commit"],
                   "--workload-id", prepared["workload_id"]]
        _code, history = evidence.run([*viewer_args, *filters])
        if len(history["records"]) != 1:
            raise RuntimeError("Approved comparison history is missing or duplicated")
        identity = history["records"][0]["record_id"]
        _code, submitted = evidence.run([*operator_args, "comparison", identity])
        _code, observed = evidence.run([*viewer_args, "comparison", identity])
        if (submitted != observed or observed["value"] != prepared
                or observed["value"]["basis"] != "client-computed" or observed["value"]["metrics"] != prepared["metrics"]):
            raise RuntimeError("Distinct technical reader did not retrieve identical qualified typed evidence")
        evidence.save(status="passed", finished_at=datetime.now(timezone.utc).isoformat(),
                      local_check_code=check_code, comparison_outcome=prepared["outcome"],
                      record_id=identity, baseline_revision=prepared["baseline_revision"],
                      public_metrics=prepared["metrics"], current_operating_evidence=str(operating),
                      transport_recovery_evidence=extension["evidence"], sanitized_reports=len(report_ids))
        return root / "proof.json"
    except BaseException:
        (root / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        evidence.save(status="failed", finished_at=datetime.now(timezone.utc).isoformat())
        raise
    finally:
        try:
            if server is not None:
                if server.poll() is None:
                    server.kill()  # Only the original owned process handle.
                server.wait(timeout=15)
            evidence.save(foreground_process_reaped=server is None or server.poll() is not None)
        except BaseException:
            evidence.save(status="failed", foreground_process_reaped=False)
            raise
        finally:
            for handle in logs:
                handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    path = verify(args.output, args.fixtures, args.source_commit)
    print(json.dumps({"proof": str(path), "passed": True}))


if __name__ == "__main__":
    main()
