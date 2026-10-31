"""Archive committed source, build an isolated wheel and preserve one sharing proof attempt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import traceback
import venv
import zipfile
from pathlib import Path

import helpers  # noqa: F401

from src.team_security import secure_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="NEW private leaf under an existing trusted parent")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Preserve earlier attempts and choose a new output directory")
    root = secure_directory(args.output, create=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    status = {"source_commit": commit, "status": "running", "platform": platform.platform(), "stages": []}
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"

    def save():
        (root / "bootstrap.json").write_text(json.dumps(status, indent=2, ensure_ascii=True), encoding="ascii")

    def run(name, arguments, *, cwd, timeout=180):
        started = time.monotonic()
        row = {"name": name, "arguments": list(map(str, arguments))}
        status["stages"].append(row)
        save()
        try:
            result = subprocess.run(list(map(str, arguments)), cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            (root / f"{name}.stdout").write_bytes(error.stdout or b"")
            (root / f"{name}.stderr").write_bytes(error.stderr or b"")
            row.update(exit_code=None, elapsed_seconds=time.monotonic() - started)
            save()
            raise
        (root / f"{name}.stdout").write_bytes(result.stdout)
        (root / f"{name}.stderr").write_bytes(result.stderr)
        row.update(exit_code=result.returncode, elapsed_seconds=time.monotonic() - started)
        save()
        if result.returncode:
            raise RuntimeError(f"Failed stage {name}; all logs retained under the selected private output")

    try:
        archive = root / "source.zip"
        run("archive", ["git", "archive", "--format=zip", f"--output={archive}", commit], cwd=repo)
        status["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        source = root / "source"
        source.mkdir()
        with zipfile.ZipFile(archive) as package:
            package.extractall(source)
        run("snapshot", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_installation.py"], cwd=source)
        run("build", [sys.executable, "-m", "build", "--outdir", root / "distributions"], cwd=source)
        wheel = next((root / "distributions").glob("*.whl"))
        status["distributions"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in (root / "distributions").iterdir()}
        virtual = root / "environment"
        venv.EnvBuilder(with_pip=True).create(virtual)
        python = virtual / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run("install", [python, "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
                        "--index-url", "https://pypi.org/simple", wheel], cwd=source)
        run("installed-sharing", [python, source / "tests/verify_sharing_distribution.py", "--output", root / "evidence",
                                  "--fixtures", source / "tests/fixtures/team-tls", "--source-commit", commit], cwd=source,
            timeout=300)
        status["status"] = "passed"
        save()
        print(json.dumps({"artifact": str(root), "source_commit": commit, "passed": True}, ensure_ascii=True))
        return 0
    except BaseException:
        status["status"] = "failed"
        save()
        (root / "bootstrap-failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
