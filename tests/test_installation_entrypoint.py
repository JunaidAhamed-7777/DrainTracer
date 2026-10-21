"""Exercise installation checks before optional packages and app imports."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MANIFEST = "runtime-manifest.json"
REPOSITORY = Path(__file__).resolve().parents[1]


def _copy_installation(root: Path, omitted: tuple[str, ...] = ()) -> None:
    document = json.loads((REPOSITORY / MANIFEST).read_text(encoding="utf-8"))
    for relative in document["required_files"]:
        if relative in omitted:
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, destination)


def _run(
    root: Path,
    *,
    arguments: tuple[str, ...] = ("--help",),
    cwd: Path | None = None,
    module: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.pop("_DRAINTRACER_REPAIR_ATTEMPTED", None)
    environment["PYTHONIOENCODING"] = "utf-8"
    entry = ["-m", "src.main"] if module else [str(root / "src/main.py")]
    return subprocess.run(
        [sys.executable, "-S", *entry, *arguments],
        cwd=cwd or root,
        env=environment,
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
    )


class InstallationEntrypointTests(unittest.TestCase):
    def _assert_missing(self, result: subprocess.CompletedProcess[str], relative: str) -> str:
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("installation check failed", output.lower())
        self.assertIn(relative, output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("ModuleNotFoundError", output)
        self.assertNotIn("Press Enter", output)
        return output

    def test_missing_required_files_are_reported_before_dependency_imports(self) -> None:
        missing_files = (
            "src/events.py",
            "leak_reports/frames/report.css",
            "leak_reports/frames/report.js",
            "TITLE.txt",
            MANIFEST,
            "src/__init__.py",
        )
        for relative in missing_files:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _copy_installation(root, omitted=(relative,))
                self._assert_missing(_run(root), relative)

    def test_healthy_installation_without_dependencies_gives_install_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_installation(root)
            result = _run(root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 2, output)
            self.assertIn("-m pip install", output)
            self.assertIn("requirements.txt", output)
            self.assertNotIn("installation check failed", output.lower())
            self.assertNotIn("Traceback", output)
            self.assertNotIn("ModuleNotFoundError", output)

    def test_module_entrypoint_reports_missing_files_before_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_installation(root, omitted=("src/events.py",))
            self._assert_missing(_run(root, module=True), "src/events.py")

    def test_absolute_script_checks_installation_when_started_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "DrainTracer"
            elsewhere = workspace / "other-project"
            elsewhere.mkdir()
            _copy_installation(root, omitted=("leak_reports/frames/report.css",))
            output = self._assert_missing(
                _run(root, cwd=elsewhere),
                "leak_reports/frames/report.css",
            )
            self.assertIn(str(root), output)

    def test_headless_missing_files_exit_without_a_recovery_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_installation(root, omitted=("src/events.py",))
            result = _run(
                root,
                arguments=("--headless", "--target", "unused.exe", "--timeout", "1s"),
            )
            output = self._assert_missing(result, "src/events.py")
            self.assertIn("no files were changed", output)

    def test_null_device_stdin_does_not_trigger_an_interactive_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_installation(root, omitted=("src/events.py",))
            environment = os.environ.copy()
            environment.pop("_DRAINTRACER_REPAIR_ATTEMPTED", None)
            result = subprocess.run(
                [sys.executable, "-S", str(root / "src/main.py"), "--help"],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            output = self._assert_missing(result, "src/events.py")
            self.assertIn("no files were changed", output)


if __name__ == "__main__":
    unittest.main()
