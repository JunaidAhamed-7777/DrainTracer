"""Install a wheel in a clean environment and exercise its public CLI.

Run explicitly with ``python tests/verify_distribution.py path/to/package.whl``.
This smoke check downloads runtime dependencies from PyPI; it is deliberately
outside unittest discovery so normal tests need no build tools or network.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int = 30,
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout}s: {subprocess.list2cmdline(command)}") from exc
    if result.returncode:
        raise RuntimeError(
            f"Command exited with {result.returncode}: {subprocess.list2cmdline(command)}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout


def _wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        _require(len(names) == 1, "The wheel must contain exactly one distribution metadata file.")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    _require(str(metadata.get("Name", "")).lower() == "draintracer", "Expected a DrainTracer wheel.")
    version = str(metadata.get("Version", ""))
    _require(bool(version), "The wheel metadata has no version.")
    return version


def verify_distribution(wheel: Path) -> str:
    version = _wheel_version(wheel)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "DRAINTRACER_LOG", "_DRAINTRACER_REPAIR_ATTEMPTED"}
        and not key.upper().startswith("GITHUB_")
    }
    environment.update(PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8")

    with tempfile.TemporaryDirectory(prefix="draintracer-distribution-") as temporary:
        workspace = Path(temporary)
        environment_dir = workspace / "environment"
        cwd = workspace / "user-project"
        cwd.mkdir()
        venv.EnvBuilder(with_pip=True).create(environment_dir)
        scripts = environment_dir / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        command = scripts / ("draintracer.exe" if os.name == "nt" else "draintracer")

        _run(
            [
                str(python), "-m", "pip", "--isolated", "--disable-pip-version-check",
                "install", "--index-url", "https://pypi.org/simple", str(wheel),
            ],
            cwd=cwd,
            environment=environment,
            timeout=120,
        )
        _require(command.is_file(), "Installing the wheel did not create the draintracer command.")
        displayed_version = _run([str(command), "--version"], cwd=cwd, environment=environment)
        _require(displayed_version.strip() == f"DrainTracer v{version}", "CLI and wheel versions differ.")
        for entry in ([str(command)], [str(python), "-m", "draintracer"]):
            help_output = _run([*entry, "--help"], cwd=cwd, environment=environment)
            _require("--target" in help_output and "--headless" in help_output, "CLI help is incomplete.")
        diff_help = _run([str(command), "diff", "--help"], cwd=cwd, environment=environment)
        _require("--fail-on-regression" in diff_help, "The installed diff command is unavailable.")

        probe = json.loads(_run(
            [
                str(python), "-c",
                ("import json; from pathlib import Path; from importlib.metadata import version; "
                "import draintracer; from draintracer.dashboard import DashboardApp; "
                "from draintracer.main import _load_title_art; "
                "from draintracer.resources import resource_path; "
                "print(json.dumps({'metadata_version': version('draintracer'), "
                "'runtime_version': DashboardApp.VERSION, "
                "'package': str(Path(draintracer.__file__).resolve().parent), "
                "'title': _load_title_art(), "
                "'bundled_title': resource_path('TITLE.txt').read_text(encoding='utf-8').strip(), "
                "'css': resource_path('leak_reports/frames/report.css').read_text(encoding='utf-8'), "
                "'js': resource_path('leak_reports/frames/report.js').read_text(encoding='utf-8')}))"),
            ],
            cwd=cwd,
            environment=environment,
        ))
        _require(probe["metadata_version"] == version, "Installed and wheel metadata versions differ.")
        _require(probe["runtime_version"] == f"v{version}", "Runtime and package metadata versions differ.")
        package = Path(probe["package"])
        _require(package.is_relative_to(environment_dir), "The smoke test imported code outside its fresh environment.")
        _require(bool(probe["title"]) and probe["title"] == probe["bundled_title"], "Bundled title artwork was not loaded.")
        _require(bool(probe["css"].strip()) and bool(probe["js"].strip()), "Bundled report frames are empty.")

        target = cwd / "bounded_worker.py"
        target.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
        _run(
            [
                str(command), "--headless", "--target", str(target), "--timeout", "4s",
                "--warmup", "2s", "--interval", "100ms", "--window", "1s", "--no-config",
            ],
            cwd=cwd,
            environment=environment,
        )
        reports = cwd / "leak_reports" / "reports"
        json_reports = list(reports.rglob("*.json"))
        html_reports = list(reports.rglob("*.html"))
        _require(len(json_reports) == len(html_reports) == 1, "Expected one JSON/HTML report pair in the caller's directory.")
        _require(json_reports[0].with_suffix(".html") == html_reports[0], "The JSON and HTML exports are not a matching pair.")
        report = json.loads(json_reports[0].read_text(encoding="utf-8"))
        _require(report.get("version") == f"v{version}", "The exported report has a different runtime version.")
        page = html_reports[0].read_text(encoding="utf-8")
        _require(f"<style>{probe['css']}</style>" in page, "The HTML export does not embed its bundled CSS.")
        _require(f"<script>{probe['js']}</script>" in page, "The HTML export does not embed its bundled JavaScript.")
        _require(not list(package.rglob("leak_report_*.json")), "Reports were written inside the installed package.")
        _require(not list(package.rglob("leak_report_*.html")), "HTML reports were written inside the installed package.")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="Path to a built DrainTracer .whl file")
    args = parser.parse_args(argv)
    try:
        wheel = args.wheel.resolve(strict=True)
        _require(wheel.suffix == ".whl", "Pass a built .whl file.")
        version = verify_distribution(wheel)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"Distribution verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"DrainTracer {version}: clean pip install, CLI, bundled assets, and report export passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
