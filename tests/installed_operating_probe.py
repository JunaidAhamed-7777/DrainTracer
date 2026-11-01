"""Run the installed operating CLI from an unrelated directory, with private fixtures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    command = Path(sys.executable).parent / ("draintracer-ops.exe" if os.name == "nt" else "draintracer-ops")
    with tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None,
                                      prefix=".draintracer-installed-ops-") as temporary:
        root = Path(temporary)
        result = subprocess.run([str(command), "--workspace", str(root / "exercise"), "--operator", "Distribution verifier",
                                 "--contact", "Local fixture only",
                                 "--readiness-seconds", "10"], capture_output=True, text=True, timeout=100, check=False)
        if result.returncode:
            raise RuntimeError(f"Installed operating exercise failed: {result.stdout}{result.stderr}")
        receipt = json.loads(result.stdout)
        document = json.loads(Path(receipt["evidence"]).read_bytes())
        if not receipt["operating_prerequisites_met"] or not all(document["readiness_gates"].values()):
            raise RuntimeError("Installed operating exercise did not satisfy its declared checks")
        if document["rates"]["authorized_read_availability"]["successful"] != 5:
            raise RuntimeError("Installed exercise did not perform actual authoritative reads")
        if "transport" in document["components"]:
            raise RuntimeError("Default operating baseline unexpectedly activated transport")
        if os.name == "nt" and document["rates"]["agent_completed_runs"]["successful"] != 2:
            raise RuntimeError("Installed exercise did not complete both native agent runs")
        print(json.dumps({"operating_prerequisites_met": True, "components": document["components"],
                          "probe_count": len(document["probes"])}))


if __name__ == "__main__":
    main()
