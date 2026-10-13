"""The diff command: rendering, JSON output, exit codes, and dispatch."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import make_console
from test_report_diff import _run

from src import main as cli
from src.diff_command import EXIT_ERROR, EXIT_OK, EXIT_REGRESSION, run_diff
from src.events import LeakSeverity


class DiffCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.leaky = root / "leaky.json"
        self.fixed = root / "fixed.json"
        self.leaky.write_text(json.dumps(_run(4.0, severity=LeakSeverity.HIGH)), encoding="utf-8")
        self.fixed.write_text(json.dumps(_run(0.0)), encoding="utf-8")
        self.root = root

    def _run(self, *argv: str, width: int = 100, encoding: str = "utf-8") -> tuple[int, str]:
        console = make_console(width=width, encoding=encoding)
        code = run_diff(list(argv), console)
        return code, console.file.getvalue()

    def test_narrow_consoles_get_one_block_per_resource(self) -> None:
        code, output = self._run(str(self.leaky), str(self.fixed), width=80)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("MEMORY   HIGH → clear  resolved", output)
        self.assertIn("drift  +8.0 MB/s →", output)
        self.assertIn("VERDICT  IMPROVED", output)
        self.assertTrue(all(len(line) <= 80 for line in output.splitlines()))

    def test_wide_consoles_get_a_table(self) -> None:
        _code, output = self._run(str(self.leaky), str(self.fixed), width=140)
        self.assertIn("SIGNAL A → B", output)
        self.assertIn("▼ 100%", output)

    def test_legacy_code_pages_stay_encodable(self) -> None:
        _code, output = self._run(str(self.leaky), str(self.fixed), width=80, encoding="cp1252")
        output.encode("cp1252")
        self.assertIn("HIGH -> clear", output)

    def test_json_output(self) -> None:
        code, output = self._run(str(self.leaky), str(self.fixed), "--json")
        self.assertEqual(code, EXIT_OK)
        data = json.loads(output)
        self.assertEqual(data["verdict"], "improved")
        self.assertEqual(data["resources"]["memory"]["signal"], "resolved")

    def test_regressions_fail_only_when_asked(self) -> None:
        self.assertEqual(self._run(str(self.fixed), str(self.leaky))[0], EXIT_OK)
        code, output = self._run(str(self.fixed), str(self.leaky), "--fail-on-regression")
        self.assertEqual(code, EXIT_REGRESSION)
        self.assertIn("REGRESSED", output)
        improved = self._run(str(self.leaky), str(self.fixed), "--fail-on-regression")[0]
        self.assertEqual(improved, EXIT_OK)

    def test_bad_inputs_exit_2(self) -> None:
        code, output = self._run(str(self.leaky), str(self.root / "missing.json"))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("Diff error:", output)
        self.assertIn("no such file", output)

    def test_main_dispatches_the_diff_command(self) -> None:
        console = make_console()
        code = cli._main(console, ["diff", str(self.fixed), str(self.leaky), "--fail-on-regression"])
        self.assertEqual(code, EXIT_REGRESSION)
        self.assertIn("DRAINTRACER REPORT DIFF", console.file.getvalue())

    def test_missing_arguments_are_usage_errors(self) -> None:
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            run_diff([str(self.leaky)], make_console())
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
