"""Frozen-source setup is local, bounded and inspectable even when preparation fails."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401
import proof_archive
import run_sharing_proof

from src.team_security import secure_directory

COMMIT = "a" * 40


def process_api(**kwargs):
    """Replace only the helper binding, leaving stdlib metadata queries real."""
    return SimpleNamespace(run=mock.Mock(**kwargs), DEVNULL=subprocess.DEVNULL,
                           TimeoutExpired=subprocess.TimeoutExpired)


class SharingProofBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home() if os.name == "nt" else None)
        self.addCleanup(self.temp.cleanup)
        self.root = secure_directory(Path(self.temp.name) / "private", create=True)
        self.archive = self.root / "input.zip"
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.comment = COMMIT.encode()
            archive.writestr("src/example.py", "fixture")
        self.digest = proof_archive.sha256(self.archive)

    def frozen(self, output):
        return ["--output", str(output), "--source-archive", str(self.archive), "--source-commit", COMMIT,
                "--archive-sha256", self.digest]

    def fake_git(self, arguments, **_kwargs):
        self.assertEqual(arguments[0], "git")
        if arguments[1] == "rev-parse":
            return subprocess.CompletedProcess(arguments, 0, (COMMIT + "\n").encode(), b"")
        self.assertEqual(arguments[1], "archive")
        destination = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("--output=")))
        destination.write_bytes(self.archive.read_bytes())
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    def test_frozen_mode_validates_prepares_and_skips_all_git_calls(self):
        output = self.root / "frozen"
        with (mock.patch.object(run_sharing_proof, "_build_and_verify") as build,
              mock.patch.object(run_sharing_proof, "subprocess", process_api(
                  side_effect=AssertionError("unexpected command"))),
              redirect_stdout(io.StringIO())):
            self.assertEqual(run_sharing_proof.main(self.frozen(output)), 0)
        state = json.loads((output / "bootstrap.json").read_bytes())
        self.assertEqual(state["source_mode"], "frozen")
        self.assertEqual(state["archive_sha256"], self.digest)
        self.assertEqual(state["source_commit"], COMMIT)
        self.assertEqual((output / "source/src/example.py").read_text(), "fixture")
        build.assert_called_once()

    def test_native_git_preparation_continues_to_validate_the_created_archive(self):
        output = self.root / "native"
        with (mock.patch.object(run_sharing_proof, "subprocess", process_api(side_effect=self.fake_git)),
              mock.patch.object(run_sharing_proof, "_build_and_verify") as build,
              redirect_stdout(io.StringIO())):
            self.assertEqual(run_sharing_proof.main(["--output", str(output)]), 0)
        state = json.loads((output / "bootstrap.json").read_bytes())
        self.assertEqual(state["source_mode"], "native-git")
        self.assertEqual([stage["name"] for stage in state["stages"]], ["git-head", "archive"])
        build.assert_called_once()

    def test_metadata_subprocess_remains_independent_of_the_fake_git_adapter(self):
        output = self.root / "metadata-query"
        commands = process_api(side_effect=self.fake_git)
        original_run = subprocess.run
        original_platform = run_sharing_proof.platform
        metadata_values = []

        def metadata_query():
            self.assertIs(subprocess.run, original_run)
            # Windows Python3.10 platform.platform uses check_output internally.
            # Exercise that dependency on every platform, without global mocks.
            value = subprocess.check_output(
                [sys.executable, "-I", "-c", "import platform; print(platform.system())"],
                text=True, timeout=10).strip()
            metadata_values.append(value)
            return value

        with (mock.patch.object(run_sharing_proof, "subprocess", commands),
              mock.patch.object(run_sharing_proof, "platform", SimpleNamespace(platform=metadata_query)),
              mock.patch.object(run_sharing_proof, "_build_and_verify") as build,
              redirect_stdout(io.StringIO())):
            self.assertEqual(run_sharing_proof.main(["--output", str(output)]), 0)
        state = json.loads((output / "bootstrap.json").read_bytes())
        self.assertEqual(len(metadata_values), 1)
        self.assertTrue(metadata_values[0])
        self.assertEqual(state["platform"], metadata_values[0])
        self.assertEqual([stage["name"] for stage in state["stages"]], ["git-head", "archive"])
        self.assertEqual(commands.run.call_count, 2)
        self.assertIs(subprocess.run, original_run)
        self.assertIs(run_sharing_proof.platform, original_platform)
        build.assert_called_once()

    def test_failed_git_metadata_is_recorded_before_any_archive_or_product(self):
        output = self.root / "git-failed"
        with (mock.patch.object(run_sharing_proof, "subprocess", process_api(return_value=subprocess.CompletedProcess(
                ["git", "rev-parse", "HEAD"], 128, b"", b"fatal: incompatible worktree metadata"))),
              mock.patch.object(run_sharing_proof, "_build_and_verify") as build,
              self.assertRaises(RuntimeError)):
            run_sharing_proof.main(["--output", str(output)])
        state = json.loads((output / "bootstrap.json").read_bytes())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["stages"][0]["exit_code"], 128)
        self.assertIn("incompatible", (output / "git-head.stderr").read_text())
        self.assertTrue((output / "bootstrap-failure.txt").is_file())
        self.assertFalse((output / "source").exists())
        build.assert_not_called()

    def test_mismatched_hash_and_partial_frozen_arguments_remain_failed_private_attempts(self):
        for name, arguments in (("mismatch", self.frozen(self.root / "mismatch")[:-1] + ["0" * 64]),
                                ("partial", ["--output", str(self.root / "partial"), "--source-commit", COMMIT])):
            output = self.root / name
            with (self.subTest(name=name), mock.patch.object(run_sharing_proof, "_build_and_verify") as build,
                  mock.patch.object(run_sharing_proof, "subprocess", process_api(
                      side_effect=AssertionError("unexpected Git"))),
                  self.assertRaises(ValueError)):
                run_sharing_proof.main(arguments)
            self.assertEqual(json.loads((output / "bootstrap.json").read_bytes())["status"], "failed")
            self.assertTrue((output / "bootstrap-failure.txt").is_file())
            self.assertFalse((output / "source").exists())
            build.assert_not_called()

    def test_existing_output_is_preserved_and_help_never_prepares(self):
        output = secure_directory(self.root / "existing", create=True)
        sentinel = output / "keep.txt"
        sentinel.write_text("unchanged")
        with self.assertRaises(ValueError):
            run_sharing_proof.main(self.frozen(output))
        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertFalse((output / "bootstrap.json").exists())
        with (mock.patch.object(run_sharing_proof, "subprocess", process_api(
                side_effect=AssertionError("unexpected command"))),
              redirect_stdout(io.StringIO()) as text, self.assertRaises(SystemExit) as caught):
            run_sharing_proof.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--source-archive", text.getvalue())


if __name__ == "__main__":
    unittest.main()
