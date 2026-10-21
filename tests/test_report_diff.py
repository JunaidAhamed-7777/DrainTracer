"""Report comparison: loading, signal and drift classification, verdicts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import make_alert, make_process, make_snapshot

from src.events import LeakResource, LeakSeverity
from src.report import snapshot_to_dict
from src.report_diff import (
    IMPROVED,
    MIXED,
    REGRESSED,
    UNCHANGED,
    DiffError,
    compare_reports,
    format_drift,
    load_report,
)

MB = 1024 * 1024


def _run(
    rss_step_mb: float = 0.0,
    handles_step: int = 0,
    severity: LeakSeverity | None = None,
    resource: LeakResource = LeakResource.MEMORY,
    samples: int = 21,
    **extra,
) -> dict:
    """A report sampled every 0.5 s with linear growth per sample."""
    snapshots = []
    for index in range(samples):
        process = make_process(
            100,
            rss=int(100 * MB + index * rss_step_mb * MB),
            handles=200 + index * handles_step,
        )
        alerts = (make_alert(resource, severity),) if severity and index > samples // 2 else ()
        snapshots.append(
            snapshot_to_dict(make_snapshot(processes=(process,), alerts=alerts,
                                           timestamp=1000.0 + index * 0.5))
        )
    report = {
        "report_id": "id",
        "version": "v1.5",
        "target_path": "C:\\app\\server.exe",
        "detection_ran": True,
        "settings": {"windows_seconds": [10.0], "sample_interval_ms": 500,
                     "warmup_seconds": 0.0, "thresholds": {}},
        "snapshots": snapshots,
    }
    report.update(extra)
    return report


class ComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def _load(self, name: str, report: dict):
        path = self.root / name
        path.write_text(json.dumps(report), encoding="utf-8")
        return load_report(path)

    def _compare(self, before: dict, after: dict):
        return compare_reports(self._load("a.json", before), self._load("b.json", after))

    def test_a_fixed_memory_leak_is_an_improvement(self) -> None:
        result = self._compare(_run(4.0, severity=LeakSeverity.HIGH), _run(0.0))
        memory = result.resources[0]
        self.assertEqual((memory.signal, memory.trend), ("resolved", IMPROVED))
        self.assertAlmostEqual(memory.drift_change_ratio, -1.0, places=3)
        self.assertEqual(result.verdict, IMPROVED)
        self.assertEqual(
            result.headline(), "memory leak signal resolved; memory drift down 100%"
        )

    def test_a_new_leak_is_a_regression(self) -> None:
        result = self._compare(_run(), _run(handles_step=10, severity=LeakSeverity.MEDIUM,
                                            resource=LeakResource.HANDLES))
        handles = result.resources[1]
        self.assertEqual((handles.signal, handles.trend), ("new", REGRESSED))
        self.assertEqual(result.verdict, REGRESSED)
        self.assertIn("new handles leak signal (MEDIUM)", result.headline())

    def test_better_memory_with_worse_handles_is_mixed(self) -> None:
        result = self._compare(_run(4.0), _run(0.0, handles_step=10))
        self.assertEqual(result.verdict, MIXED)

    def test_noise_is_unchanged(self) -> None:
        result = self._compare(_run(0.0002), _run(0.0001))  # ~200 vs ~100 B/s
        self.assertEqual(result.resources[0].trend, UNCHANGED)
        self.assertEqual(result.verdict, UNCHANGED)
        self.assertEqual(result.headline(), "no meaningful change in leak signals or drift")
        same = self._compare(_run(4.0, severity=LeakSeverity.HIGH),
                             _run(4.0, severity=LeakSeverity.HIGH))
        self.assertEqual(same.resources[0].signal, "persists")
        self.assertEqual(same.verdict, UNCHANGED)

    def test_faster_shrinking_is_not_a_regression(self) -> None:
        result = self._compare(_run(-4.0), _run(-1.0))  # still shrinking, just slower
        self.assertEqual(result.resources[0].trend, UNCHANGED)

    def test_warnings_explain_why_results_may_not_compare(self) -> None:
        after = _run(samples=5, target_path="C:\\other\\client.exe", detection_ran=False)
        after["settings"]["windows_seconds"] = [30.0, 60.0]
        warnings = " | ".join(self._compare(_run(), after).warnings)
        self.assertIn("different targets (server.exe and client.exe)", warnings)
        self.assertIn("report B never filled a detection window", warnings)
        self.assertIn("windows 10s vs 30s/1m", warnings)
        self.assertIn("differ in length", warnings)
        self.assertEqual(self._compare(_run(), _run()).warnings, ())

    def test_handle_type_growth_is_paired(self) -> None:
        def typed(report: dict, file_step: int, socket_step: int) -> dict:
            for index, snapshot in enumerate(report["snapshots"]):
                snapshot["handle_types"] = {
                    "file": 10 + index * file_step, "socket": 2 + index * socket_step, "event": 4,
                }
            return report

        result = self._compare(typed(_run(), 5, 0), typed(_run(), 0, 3))
        # Types that changed in neither run (event) are left out.
        self.assertEqual(result.handle_type_growth, {"file": (100, 0), "socket": (0, 60)})
        self.assertEqual(self._compare(_run(), _run()).handle_type_growth, {})

    def test_json_output_round_trips(self) -> None:
        result = self._compare(_run(4.0, severity=LeakSeverity.HIGH), _run(0.0))
        data = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(data["verdict"], "improved")
        self.assertEqual(data["resources"]["memory"]["signal"], "resolved")
        self.assertEqual(data["before"]["target"], "server.exe")


class LoadTests(unittest.TestCase):
    def test_unreadable_reports_raise_clear_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cases = {
                "missing.json": (None, "no such file"),
                "bad.json": ("{not json", "not valid JSON"),
                "list.json": ("[1, 2]", "not a DrainTracer report"),
                "empty.json": ('{"snapshots": []}', "has no snapshots"),
                "binary.json": (b"\xff\xfe\x00", "not a UTF-8 JSON report"),
                "deep.json": ("[" * 100_000 + "]" * 100_000, "nested too deeply"),
            }
            for name, (content, message) in cases.items():
                with self.subTest(name=name):
                    path = root / name
                    if isinstance(content, bytes):
                        path.write_bytes(content)
                    elif content is not None:
                        path.write_text(content, encoding="utf-8")
                    with self.assertRaises(DiffError) as caught:
                        load_report(path)
                    self.assertIn(message, str(caught.exception))


class FormatTests(unittest.TestCase):
    def test_drift_units(self) -> None:
        self.assertEqual(format_drift("memory", 8 * MB), "+8.0 MB/s")
        self.assertEqual(format_drift("memory", -512), "-30.0 KB/min")
        self.assertEqual(format_drift("handles", 0.05), "+3.00/min")
        self.assertEqual(format_drift("threads", None), "—")


class CoverageWarningTests(unittest.TestCase):
    setUp = ComparisonTests.setUp
    _load = ComparisonTests._load
    _compare = ComparisonTests._compare

    def test_partial_coverage_is_warned_about(self) -> None:
        from helpers import make_denied

        after = _run()
        for index in range(5):
            after["snapshots"][index] = snapshot_to_dict(make_snapshot(
                processes=(make_process(100), make_denied(200)),
                timestamp=1000.0 + index * 0.5, measured_coverage=True))
        comparison = self._compare(_run(), after)
        warnings = " | ".join(comparison.warnings)
        self.assertIn("report B could not read every process", warnings)
        self.assertIn("partial in 5 of 21 samples", warnings)
        self.assertEqual(comparison.to_dict()["after"]["partial_coverage"],
                         ["memory", "handles", "threads"])
        self.assertEqual(comparison.to_dict()["before"]["partial_coverage"], [])


if __name__ == "__main__":
    unittest.main()
