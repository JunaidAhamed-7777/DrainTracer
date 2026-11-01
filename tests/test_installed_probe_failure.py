"""Installed fixture failures remain diagnosable after normal cleanup."""

from __future__ import annotations

import importlib.util
import io
import json
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import installed_operating_probe
from installed_probe_failure import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_EVIDENCE_BYTES,
    MAX_OUTPUT_CHARACTERS,
    _evidence_context,
    retain_probe_failure,
)


def transport_probe():
    """Load the test driver without requiring a separately installed package."""
    package = types.ModuleType("draintracer")
    package.__path__ = []
    security = types.ModuleType("draintracer.team_security")

    def directory(path, *, create=False):
        if create:
            path.mkdir(mode=0o700)
        return path

    security.secure_directory = directory
    spec = importlib.util.spec_from_file_location(
        "_transport_probe_fixture", Path(__file__).with_name("installed_transport_operating_probe.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"draintracer": package, "draintracer.team_security": security}):
        spec.loader.exec_module(module)
    return module


class InstalledProbeFailureTests(unittest.TestCase):
    def test_both_drivers_retain_actual_failure_context_before_temporary_cleanup(self):
        for driver in (installed_operating_probe, transport_probe()):
            with self.subTest(driver=driver.__name__):
                paths = []
                original_stdout, original_stderr = [], "original CLI stderr"
                document = {"operating_prerequisites_met": False,
                            "budget_checks": {"driver_sampled_rss": False},
                            "readiness_gates": {"all_checks_and_budgets_passed": False}}

                def failed_command(arguments, *, paths=paths, document=document, original_stdout=original_stdout,
                                   original_stderr=original_stderr, **kwargs):
                    self.assertEqual(kwargs["timeout"], 100)
                    self.assertEqual(arguments[-2:], ["--readiness-seconds", "10"])
                    root = Path(arguments[arguments.index("--workspace") + 1])
                    root.mkdir()
                    evidence = root / "operating-evidence.json"
                    evidence.write_text(json.dumps(document), encoding="utf-8")
                    paths.append(root.parent)
                    output = json.dumps({"evidence": str(evidence), "operating_prerequisites_met": False})
                    original_stdout.append(output)
                    return subprocess.CompletedProcess(arguments, 2, output, original_stderr)

                output = io.StringIO()
                with (mock.patch.object(driver.subprocess, "run", side_effect=failed_command),
                      redirect_stderr(output), self.assertRaises(RuntimeError) as failed):
                    driver.main()
                self.assertIn(original_stdout[0] + original_stderr, str(failed.exception))
                context = json.loads(output.getvalue().split(": ", 1)[1])
                self.assertEqual(context["returncode"], 2)
                self.assertEqual(context["stdout"]["text"], original_stdout[0])
                self.assertEqual(context["stderr"]["text"], original_stderr)
                self.assertEqual(context["evidence"]["document"], document)
                self.assertTrue(paths)
                self.assertTrue(all(not path.exists() for path in paths))

    def test_missing_malformed_nonfinite_and_oversized_context_do_not_replace_failure(self):
        cases = [(None, "unavailable"), (b"not JSON", "unavailable"),
                 (b'{"value":NaN}', "unavailable"), (b'{"value":1e9999}', "unavailable"),
                 (b"[]", "malformed"), (b"x" * (MAX_EVIDENCE_BYTES + 1), "oversized")]
        for raw, expected_status in cases:
            with self.subTest(raw=raw[:30] if raw else raw), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "exercise").mkdir()
                if raw is not None:
                    (root / "exercise" / "operating-evidence.json").write_bytes(raw)
                original = RuntimeError("original failure")
                output = io.StringIO()
                with (redirect_stderr(output), self.assertRaises(RuntimeError) as failed,
                      retain_probe_failure(root, subprocess.CompletedProcess([], 2, "stdout", "stderr"))):
                    raise original
                self.assertIs(failed.exception, original)
                context = json.loads(output.getvalue().split(": ", 1)[1])
                self.assertEqual(context["evidence"]["status"], expected_status)

    def test_unreadable_context_keeps_original_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "exercise").mkdir()
            (root / "exercise" / "operating-evidence.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
                context = _evidence_context(root)
            self.assertEqual(context, {"status": "unavailable", "error_type": "PermissionError"})

    def test_resolved_escape_is_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "exercise").mkdir()
            expected = root / "exercise" / "operating-evidence.json"
            expected.write_text("{}", encoding="utf-8")
            original_resolve = Path.resolve

            def escaped(path, *args, **kwargs):
                return root.parent / "outside.json" if path == expected else original_resolve(path, *args, **kwargs)

            with (mock.patch.object(Path, "resolve", escaped),
                  mock.patch.object(Path, "open", side_effect=AssertionError("must not open")) as opened):
                context = _evidence_context(root)
            opened.assert_not_called()
            self.assertEqual(context, {"status": "unavailable", "error_type": "ValueError"})

    def test_nonregular_context_is_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "exercise").mkdir()
            expected = root / "exercise" / "operating-evidence.json"
            expected.write_text("{}", encoding="utf-8")
            original_stat = Path.stat

            def nonregular(path, *args, **kwargs):
                return types.SimpleNamespace(st_mode=stat.S_IFIFO) if path == expected else original_stat(path, *args, **kwargs)

            with (mock.patch.object(Path, "stat", nonregular),
                  mock.patch.object(Path, "open", side_effect=AssertionError("must not open")) as opened):
                context = _evidence_context(root)
            opened.assert_not_called()
            self.assertEqual(context, {"status": "nonregular"})

    def test_linked_context_is_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "exercise").mkdir()
            expected = root / "exercise" / "operating-evidence.json"
            expected.write_text("{}", encoding="utf-8")
            original_lstat = Path.lstat

            def linked(path, *args, **kwargs):
                return types.SimpleNamespace(st_mode=stat.S_IFLNK) if path == expected else original_lstat(path, *args, **kwargs)

            with (mock.patch.object(Path, "lstat", linked),
                  mock.patch.object(Path, "open", side_effect=AssertionError("must not open")) as opened):
                context = _evidence_context(root)
            opened.assert_not_called()
            self.assertEqual(context, {"status": "linked_path"})

    def test_console_context_is_bounded_and_truncation_is_explicit(self):
        result = subprocess.CompletedProcess([], 2, "\U0001f600" * (MAX_OUTPUT_CHARACTERS + 7), "x" * 9999)
        output = io.StringIO()
        with (tempfile.TemporaryDirectory() as temporary, redirect_stderr(output),
              self.assertRaisesRegex(ValueError, "primary"), retain_probe_failure(Path(temporary), result)):
            raise ValueError("primary")
        rendered = output.getvalue()
        self.assertLess(len(rendered.encode("ascii")), MAX_DIAGNOSTIC_BYTES)
        context = json.loads(rendered.split(": ", 1)[1])
        self.assertEqual(len(context["stdout"]["text"]), MAX_OUTPUT_CHARACTERS)
        self.assertEqual(context["stdout"]["omitted_characters"], 7)
        self.assertEqual(context["stderr"]["omitted_characters"], 9999 - MAX_OUTPUT_CHARACTERS)

    def test_diagnostic_write_error_cannot_mask_original_exception(self):
        original = ValueError("primary")
        with (tempfile.TemporaryDirectory() as temporary,
              mock.patch("installed_probe_failure.sys.stderr.write", side_effect=OSError("diagnostic write failed")),
              self.assertRaises(ValueError) as failed,
              retain_probe_failure(Path(temporary), subprocess.CompletedProcess([], 2, "stdout", "stderr"))):
            raise original
        self.assertIs(failed.exception, original)

    def test_success_does_not_read_or_emit_failure_context(self):
        with mock.patch("installed_probe_failure._evidence_context", side_effect=AssertionError("must not read")):
            output = io.StringIO()
            with redirect_stderr(output), retain_probe_failure(Path("unused"), subprocess.CompletedProcess([], 0, "", "")):
                pass
        self.assertEqual(output.getvalue(), "")
