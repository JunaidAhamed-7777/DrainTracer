"""Consent, Git recovery, and restart behavior for incomplete installations."""

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


class InstallationGitTests(unittest.TestCase):
    def test_git_uses_argument_list_bounded_wait_and_noninteractive_credentials(self) -> None:
        root = Path.cwd()
        completed = subprocess.CompletedProcess([], 0, stdout="up to date\n", stderr="")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            output = cli._run_installation_git(root, ["pull", "--ff-only", "origin", "main"])
        self.assertEqual(output.strip(), "up to date")
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[-4:], ["pull", "--ff-only", "origin", "main"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs["timeout"], 60)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(run.call_args.kwargs["env"]["GCM_INTERACTIVE"], "Never")

    def test_missing_git_and_timeout_are_runtime_errors(self) -> None:
        failures = (
            FileNotFoundError("git not installed"),
            subprocess.TimeoutExpired(["git", "pull"], 60),
        )
        for failure in failures:
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(subprocess, "run", side_effect=failure),
                self.assertRaises(RuntimeError),
            ):
                cli._run_installation_git(Path.cwd(), ["pull"])

    def test_git_failure_reports_the_reason(self) -> None:
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="remote unavailable")
        with (
            mock.patch.object(subprocess, "run", return_value=completed),
            self.assertRaisesRegex(RuntimeError, "remote unavailable"),
        ):
            cli._run_installation_git(Path.cwd(), ["pull"])


class InstallationPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def _invoke(
        self,
        *,
        status: tuple[tuple[str, ...], str | None] = (("src/missing.py",), None),
        answers: tuple[object, ...] = ("",),
        terminal: bool = True,
        argv: list[str] | None = None,
        repaired: bool = True,
        restart_error: OSError | None = None,
    ) -> tuple[int | None, str, mock.Mock, mock.Mock, mock.Mock]:
        output = io.StringIO()
        stdin = mock.Mock()
        stdin.isatty.return_value = terminal
        root = Path.cwd().resolve()
        with (
            mock.patch.object(cli, "_installation_status", return_value=status),
            mock.patch.object(cli.sys, "stdin", stdin),
            mock.patch("builtins.input", side_effect=answers) as read,
            mock.patch.object(cli, "_repair_installation", return_value=repaired) as repair,
            mock.patch.object(cli.os, "execv", side_effect=restart_error) as restart,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            result = cli._check_installation(root, [] if argv is None else argv)
        return result, output.getvalue(), read, repair, restart

    def test_complete_installation_does_not_prompt_pull_or_restart(self) -> None:
        result, _output, read, repair, restart = self._invoke(status=((), None))
        self.assertIsNone(result)
        read.assert_not_called()
        repair.assert_not_called()
        restart.assert_not_called()

    def test_bad_snapshot_reports_error_without_attempting_recovery(self) -> None:
        result, output, read, repair, restart = self._invoke(status=((), "bad snapshot"))
        self.assertEqual(result, 2)
        self.assertIn("bad snapshot", output)
        read.assert_not_called()
        repair.assert_not_called()
        restart.assert_not_called()

    def test_missing_files_are_listed_and_enter_repairs_then_restarts_same_arguments(self) -> None:
        argv = ["--target", "C:/work/my worker.py", "--", "--port", "8181"]
        result, output, read, repair, restart = self._invoke(argv=argv)
        self.assertEqual(result, 0)
        self.assertIn("src/missing.py", output)
        read.assert_called_once()
        root = Path.cwd().resolve()
        repair.assert_called_once_with(root)
        restart.assert_called_once_with(
            sys.executable,
            [sys.executable, str(root / "src/main.py"), *argv],
        )
        self.assertEqual(os.environ.get("_DRAINTRACER_REPAIR_ATTEMPTED"), "1")

    def test_repair_failure_does_not_restart_or_repeat_pull(self) -> None:
        result, _output, read, repair, restart = self._invoke(repaired=False)
        self.assertEqual(result, 2)
        read.assert_called_once()
        repair.assert_called_once()
        restart.assert_not_called()

    def test_restart_failure_is_reported_without_an_uncaught_exception(self) -> None:
        result, output, _read, repair, restart = self._invoke(
            restart_error=OSError("restart unavailable"),
        )
        self.assertEqual(result, 2)
        self.assertIn("restart unavailable", output)
        repair.assert_called_once()
        restart.assert_called_once()

    def test_only_an_empty_answer_authorizes_repair(self) -> None:
        result, _output, read, repair, restart = self._invoke(answers=("yes", "pull", ""))
        self.assertEqual(result, 0)
        self.assertEqual(read.call_count, 3)
        repair.assert_called_once()
        restart.assert_called_once()

    def test_quit_eof_and_interrupt_never_pull_or_restart(self) -> None:
        for answer, expected in (("q", 2), ("Q", 2), (EOFError(), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(answer=type(answer).__name__ if isinstance(answer, BaseException) else answer):
                result, _output, _read, repair, restart = self._invoke(answers=(answer,))
                self.assertEqual(result, expected)
                repair.assert_not_called()
                restart.assert_not_called()

    def test_noninteractive_input_never_prompts_or_changes_source(self) -> None:
        result, output, read, repair, restart = self._invoke(terminal=False)
        self.assertEqual(result, 2)
        self.assertIn("src/missing.py", output)
        read.assert_not_called()
        repair.assert_not_called()
        restart.assert_not_called()

    def test_headless_mode_never_prompts_even_with_terminal_input(self) -> None:
        result, _output, read, repair, restart = self._invoke(
            argv=["--headless", "--target", "worker.py"],
        )
        self.assertEqual(result, 2)
        read.assert_not_called()
        repair.assert_not_called()
        restart.assert_not_called()

    def test_target_argument_named_headless_does_not_disable_interactive_repair(self) -> None:
        result, _output, read, repair, restart = self._invoke(
            argv=["--target", "worker.py", "--", "--headless"],
        )
        self.assertEqual(result, 0)
        read.assert_called_once()
        repair.assert_called_once()
        restart.assert_called_once()

    def test_restart_marker_is_consumed_and_prevents_a_repair_loop(self) -> None:
        os.environ["_DRAINTRACER_REPAIR_ATTEMPTED"] = "1"
        result, _output, read, repair, restart = self._invoke()
        self.assertEqual(result, 2)
        self.assertNotIn("_DRAINTRACER_REPAIR_ATTEMPTED", os.environ)
        read.assert_not_called()
        repair.assert_not_called()
        restart.assert_not_called()

    def test_restart_marker_does_not_block_a_now_complete_installation(self) -> None:
        os.environ["_DRAINTRACER_REPAIR_ATTEMPTED"] = "1"
        result, _output, read, repair, restart = self._invoke(status=((), None))
        self.assertIsNone(result)
        self.assertNotIn("_DRAINTRACER_REPAIR_ATTEMPTED", os.environ)
        read.assert_not_called()
        repair.assert_not_called()
        restart.assert_not_called()


@unittest.skipUnless(shutil.which("git"), "Git is required for isolated recovery integration")
class InstallationRecoveryTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        return result.stdout.strip()

    def _checkout(self, workspace: Path) -> Path:
        root = workspace / "installation with spaces"
        root.mkdir()
        origin = workspace / "origin.git"
        self._git(workspace, "init", "--bare", str(origin))
        self._git(root, "init", "-b", "main")
        self._git(root, "config", "user.name", "Fixture User")
        self._git(root, "config", "user.email", "fixture@example.invalid")
        self._git(root, "config", "commit.gpgsign", "false")
        self._git(root, "config", "core.hooksPath", str(workspace / "disabled-hooks"))
        required = sorted(cli._INSTALLATION_CORE | {
            "src/missing.py", "src/modified.py", "src/staged.py",
        })
        for relative in required:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original fixture\n", encoding="utf-8")
        (root / "runtime-manifest.json").write_text(
            json.dumps({"schema": 1, "required_files": required}), encoding="utf-8",
        )
        self._git(root, "add", ".")
        self._git(root, "commit", "-m", "Create runtime fixture")
        self._git(root, "remote", "add", "origin", str(origin))
        self._git(root, "push", "-u", "origin", "main")
        return root

    def _repair(self, root: Path) -> tuple[bool, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = cli._repair_installation(root)
        return result, output.getvalue()

    def test_up_to_date_pull_restores_missing_file_and_preserves_user_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._checkout(Path(temporary))
            (root / "src/missing.py").unlink()
            modified = root / "src/modified.py"
            modified.write_text("user working change\n", encoding="utf-8")
            staged = root / "src/staged.py"
            staged.write_text("user staged change\n", encoding="utf-8")
            self._git(root, "add", "src/staged.py")
            staged_before = self._git(root, "diff", "--cached")
            report = root / "leak_reports/reports/user-run/report.json"
            report.parent.mkdir(parents=True)
            report.write_text('{"private": true}\n', encoding="utf-8")

            result, output = self._repair(root)

            self.assertTrue(result, output)
            self.assertEqual((root / "src/missing.py").read_text(encoding="utf-8"), "original fixture\n")
            self.assertEqual(modified.read_text(encoding="utf-8"), "user working change\n")
            self.assertEqual(staged.read_text(encoding="utf-8"), "user staged change\n")
            self.assertEqual(self._git(root, "diff", "--cached"), staged_before)
            self.assertEqual(report.read_text(encoding="utf-8"), '{"private": true}\n')
            self.assertEqual(cli._installation_status(root), ((), None))

    def test_missing_snapshot_is_restored_before_discovering_other_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._checkout(Path(temporary))
            (root / "runtime-manifest.json").unlink()
            (root / "src/missing.py").unlink()
            self.assertEqual(cli._installation_status(root), (("runtime-manifest.json",), None))

            result, output = self._repair(root)

            self.assertTrue(result, output)
            self.assertEqual(cli._installation_status(root), ((), None))

    def test_staged_deletion_is_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._checkout(Path(temporary))
            (root / "src/missing.py").unlink()
            self._git(root, "add", "-u", "--", "src/missing.py")
            staged_before = self._git(root, "diff", "--cached")

            result, _output = self._repair(root)

            self.assertFalse(result)
            self.assertFalse((root / "src/missing.py").exists())
            self.assertEqual(self._git(root, "diff", "--cached"), staged_before)

    def test_directory_in_place_of_file_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._checkout(Path(temporary))
            replacement = root / "src/missing.py"
            replacement.unlink()
            replacement.mkdir()
            marker = replacement / "user-data.txt"
            marker.write_text("preserve me", encoding="utf-8")

            result, _output = self._repair(root)

            self.assertFalse(result)
            self.assertTrue(replacement.is_dir())
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me")

    def test_subdirectory_is_not_accepted_as_the_checkout_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._checkout(Path(temporary))
            result, _output = self._repair(root / "src")
            self.assertFalse(result)

    def test_pull_failure_does_not_restore_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._checkout(Path(temporary))
            (root / "src/missing.py").unlink()
            self._git(root, "remote", "set-url", "origin", str(Path(temporary) / "absent.git"))

            result, _output = self._repair(root)

            self.assertFalse(result)
            self.assertFalse((root / "src/missing.py").exists())


if __name__ == "__main__":
    unittest.main()
