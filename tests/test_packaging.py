"""Installed-package paths and startup checks without a source checkout."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import main as cli

REPOSITORY = Path(__file__).resolve().parents[1]
MANIFEST = "runtime-manifest.json"
ASSETS = ("TITLE.txt", "leak_reports/frames/report.css", "leak_reports/frames/report.js")


def _copy_package(root: Path, omitted: tuple[str, ...] = ()) -> None:
    """Lay out the committed inventory as it appears inside a wheel."""
    document = json.loads((REPOSITORY / MANIFEST).read_text(encoding="utf-8"))
    for relative in document["required_files"]:
        if relative in omitted:
            continue
        installed = relative[4:] if relative.startswith("src/") else f"_data/{relative}"
        destination = root / installed
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, destination)


def _run_package(
    root: Path,
    cwd: Path,
    code: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("_DRAINTRACER_REPAIR_ATTEMPTED", None)
    environment["PYTHONPATH"] = str(root.parent)
    environment["PYTHONIOENCODING"] = "utf-8"
    entry = ["-m", "draintracer", "--help"] if code is None else ["-c", code]
    return subprocess.run(
        [sys.executable, "-S", *entry],
        cwd=cwd,
        env=environment,
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )


class PackagedInstallationTests(unittest.TestCase):
    def test_complete_package_needs_no_checkout_or_generated_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "draintracer"
            _copy_package(root)
            self.assertEqual(cli._installation_status(root, packaged=True), ((), None))
            self.assertFalse((root / "src").exists())
            self.assertFalse((root / ".git").exists())
            self.assertFalse((root / "leak_reports/reports").exists())

    def test_missing_packaged_module_asset_and_manifest_are_reported(self) -> None:
        for relative in ("src/events.py", *ASSETS, MANIFEST):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "draintracer"
                _copy_package(root, omitted=(relative,))
                missing, error = cli._installation_status(root, packaged=True)
                self.assertIsNone(error)
                self.assertEqual(missing, (relative,))

    def test_packaged_snapshot_rejects_paths_outside_the_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "draintracer"
            _copy_package(root)
            manifest = root / "_data" / MANIFEST
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["required_files"].append("../outside.py")
            manifest.write_text(json.dumps(document), encoding="utf-8")
            _missing, error = cli._installation_status(root, packaged=True)
            self.assertIsInstance(error, str)
            self.assertTrue(error)

    def test_packaged_failures_give_reinstall_guidance_without_git_or_prompt(self) -> None:
        for status in ((("TITLE.txt",), None), ((), "bad snapshot")):
            with (
                self.subTest(status=status),
                mock.patch.object(cli, "_installation_status", return_value=status),
                mock.patch.object(cli, "_repair_installation") as repair,
                mock.patch.object(cli, "_run_installation_git") as git,
                mock.patch.object(cli, "_restart_installation") as restart,
                mock.patch("builtins.input") as read,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                result = cli._check_installation(Path("installed"), [], packaged=True)
                self.assertEqual(result, 2)
                self.assertIn("pip install", output.getvalue())
                self.assertIn("draintracer", output.getvalue().lower())
                self.assertIn("reinstall", output.getvalue().lower())
                repair.assert_not_called()
                git.assert_not_called()
                restart.assert_not_called()
                read.assert_not_called()

    def test_module_command_checks_files_before_importing_dependencies(self) -> None:
        for relative in ("src/events.py", "src/resources.py", "TITLE.txt", MANIFEST):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                root = workspace / "site-packages" / "draintracer"
                cwd = workspace / "another-project"
                cwd.mkdir()
                _copy_package(root, omitted=(relative,))
                result = _run_package(root, cwd)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 2, output)
                self.assertIn(relative, output)
                self.assertIn("installation check failed", output.lower())
                self.assertIn("pip install", output)
                self.assertNotIn("Traceback", output)
                self.assertNotIn("ModuleNotFoundError", output)
                self.assertNotIn("Press Enter", output)

    def test_healthy_package_without_dependencies_gives_pip_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "site-packages" / "draintracer"
            cwd = workspace / "another-project"
            cwd.mkdir()
            _copy_package(root)
            result = _run_package(root, cwd)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 2, output)
            self.assertIn("pip install", output)
            self.assertIn("draintracer", output.lower())
            self.assertNotIn("installation check failed", output.lower())
            self.assertNotIn("Traceback", output)

    def test_packaged_title_and_report_frames_ignore_the_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "site-packages" / "draintracer"
            cwd = workspace / "another-project"
            _copy_package(root)
            for relative in ASSETS:
                decoy = cwd / relative
                decoy.parent.mkdir(parents=True, exist_ok=True)
                decoy.write_text("unrelated user file", encoding="utf-8")
            result = _run_package(
                root,
                cwd,
                "import json; from draintracer.resources import resource_path; "
                f"print(json.dumps({{name: resource_path(name).read_text(encoding='utf-8') "
                f"for name in {ASSETS!r}}}))",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            resources = json.loads(result.stdout)
            for relative in ASSETS:
                self.assertEqual(
                    resources[relative],
                    (REPOSITORY / relative).read_text(encoding="utf-8"),
                )
                self.assertEqual((cwd / relative).read_text(encoding="utf-8"), "unrelated user file")


if __name__ == "__main__":
    unittest.main()
