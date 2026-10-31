"""Build one preserved proof from native Git or a trusted frozen Git ZIP.

Native checkout:
  python tests/run_sharing_proof.py --output NEW_PRIVATE_LEAF
Windows-created archive consumed from Linux/WSL without invoking Git:
  python tests/run_sharing_proof.py --output NEW_PRIVATE_LEAF \
      --source-archive SOURCE.zip --source-commit FULL_SHA --archive-sha256 SHA256

The hash identifies selected trusted bytes, not signed source attestation.
ZIP paths/types are validated before exclusive extraction. Preparation failures
are retained once the new private output leaf exists; existing output is never
reused, overwritten or deleted. Limits: 64 MiB archive, 256 MiB expanded total,
32 MiB/file, 10,000 entries and 32 path components. No runtime version changes.
"""

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
from pathlib import Path

import helpers  # noqa: F401
from proof_archive import archive_hash, commit_id, copy_frozen, extract, sha256

from src.team_security import secure_directory


def _build_and_verify(source, root, commit, status, run):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="NEW private leaf under an existing trusted parent")
    parser.add_argument("--source-archive", type=Path, help="Explicit trusted Git ZIP; this mode invokes no Git")
    parser.add_argument("--source-commit", help="Full lowercase commit SHA recorded in the Git ZIP comment")
    parser.add_argument("--archive-sha256", help="Expected lowercase SHA256 supplied with the trusted frozen archive")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parent.parent
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Preserve earlier attempts and choose a new output directory")
    root = secure_directory(args.output, create=True)
    status = {"source_commit": args.source_commit, "status": "running", "platform": platform.platform(), "stages": [],
              "source_archive_origin": str(args.source_archive) if args.source_archive else None,
              "expected_archive_sha256": args.archive_sha256}
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
        return result

    try:
        save()  # Even failed Git lookup / frozen preparation remains inspectable.
        archive = root / "source.zip"
        selected = (args.source_archive is not None, args.source_commit is not None, args.archive_sha256 is not None)
        if any(selected) and not all(selected):
            raise ValueError("Frozen mode requires archive, full source commit and expected SHA256 together")
        if all(selected):
            commit = commit_id(args.source_commit)
            status["archive_sha256"] = copy_frozen(args.source_archive, archive, archive_hash(args.archive_sha256))
            status["source_mode"] = "frozen"
        else:
            result = run("git-head", ["git", "rev-parse", "HEAD"], cwd=repo)
            commit = commit_id(result.stdout.decode("ascii").strip())
            status["source_mode"] = "native-git"
            if archive.exists() or archive.is_symlink():
                raise FileExistsError("Archive output already exists")
            run("archive", ["git", "archive", "--format=zip", f"--output={archive}", commit], cwd=repo)
            status["archive_sha256"] = sha256(archive)
        status["source_commit"] = commit
        save()
        source = root / "source"
        extract(archive, source, commit)
        _build_and_verify(source, root, commit, status, run)
        status["status"] = "passed"
        save()
        print(json.dumps({"artifact": str(root), "source_commit": commit, "passed": True}, ensure_ascii=True))
        return 0
    except BaseException:
        status["status"] = "failed"
        archive = root / "source.zip"
        try:
            if archive.is_file():
                status["observed_archive_sha256"] = sha256(archive)
        except (OSError, ValueError):
            pass  # A partial/excessive copy remains, with the original failure.
        save()
        (root / "bootstrap-failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
