"""Report comparison: loading, signal and drift classification, verdicts."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from helpers import make_alert, make_process, make_snapshot

from src.events import LeakResource, LeakSeverity
from src.report import snapshot_to_dict
from src.report_diff import (
    COMPARABLE,
    IMPROVED,
    INSUFFICIENT_COVERAGE,
    MIXED,
    NOT_COMPARABLE,
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
        self.assertFalse(data["before"]["exact_facts"])
        self.assertEqual(data["before"]["chart_stride"], 1)
        self.assertEqual(data["resources"]["memory"]["episodes_before"], 1)

    def test_exact_facts_feed_the_comparison_and_thinned_old_reports_warn(self) -> None:
        before = _run(snapshot_stride=4)
        after = _run(snapshot_stride=4)
        after["session_facts"] = {
            "signals": {"memory": {"worst": "high", "worst_at": 1001.0, "episodes": 3}},
            "chart": {"stride": 4},
        }
        result = self._compare(before, after)
        warnings = " | ".join(result.warnings)
        self.assertIn("report A keeps 1 in 4 samples and predates exact session facts", warnings)
        self.assertNotIn("report B keeps", warnings)
        memory = result.resources[0]
        self.assertEqual((memory.after.worst, memory.after.episodes), ("high", 3))
        self.assertEqual(memory.signal, "new")


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
                # Some Python JSON decoders accept deep arrays, while others
                # raise RecursionError. Both must reject this non-report clearly.
                "deep.json": ("[" * 100_000 + "]" * 100_000, "report"),
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

    def test_json_decoder_recursion_errors_have_a_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "deep.json"
            path.write_text("{}", encoding="utf-8")
            with mock.patch("src.report_diff.json.loads", side_effect=RecursionError):
                with self.assertRaisesRegex(DiffError, "JSON nested too deeply to be a report"):
                    load_report(path)


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


ENVIRONMENT = {"os": "Windows", "os_release": "11", "cpu_count": 8,
               "total_memory_bytes": 16 * 1024 * MB, "machine": "AMD64"}


def _experiment(args=("--port", "1"), **labels) -> dict:
    """An ``experiment`` section as a session would write it."""
    from src.experiment import ExperimentInfo, experiment_record

    environment = {**ENVIRONMENT, **labels.pop("environment", {})}
    return experiment_record(
        ExperimentInfo(**labels), args=list(args), mode="launch", duration_seconds=10.0,
        sample_interval_ms=500, windows_seconds=[10.0], warmup_seconds=0.0,
        timeout_seconds=None, environment=environment,
    )


class EquivalenceTests(unittest.TestCase):
    setUp = ComparisonTests.setUp
    _load = ComparisonTests._load

    def _compare(self, before: dict, after: dict, strict: bool = False):
        return compare_reports(self._load("a.json", before), self._load("b.json", after),
                               strict=strict)

    def test_window_settings_compare_numeric_values_before_formatting(self) -> None:
        for first, second in ((60.1, 60.4), (60.1000000001, 60.1000000002)):
            with self.subTest(first=first, second=second):
                before, after = _run(), _run()
                before["settings"]["windows_seconds"] = [first]
                after["settings"]["windows_seconds"] = [second]
                ordinary = self._compare(before, after)
                self.assertTrue(ordinary.qualified)
                self.assertIn(f"windows {first!r}s vs {second!r}s", " | ".join(ordinary.warnings))
                self.assertEqual(self._compare(before, after, strict=True).verdict, NOT_COMPARABLE)

    def test_equivalent_numeric_windows_remain_comparable(self) -> None:
        before, after = _run(), _run()
        before["settings"]["windows_seconds"] = [60, 120]
        after["settings"]["windows_seconds"] = [60.0, 120.0]
        self.assertEqual(self._compare(before, after, strict=True).caveats, ())

    def test_platform_and_counter_changes_qualify_comparisons(self) -> None:
        before = _run(experiment=_experiment(environment={
            "private_memory_metric": "private_bytes", "handle_metric": "handles",
        }))
        after = _run(experiment=_experiment(environment={
            "os": "Linux", "private_memory_metric": "uss", "handle_metric": "file_descriptors",
        }))
        result = self._compare(before, after)
        self.assertTrue(result.qualified)
        self.assertIn("platform Windows vs Linux", result.caveats[0].message)
        self.assertIn("private memory counter", result.caveats[0].message)
        self.assertIn("handle counter", result.caveats[0].message)
        self.assertEqual(self._compare(before, after, strict=True).verdict, NOT_COMPARABLE)

    def test_a_shorter_lighter_run_cannot_earn_an_unqualified_improvement(self) -> None:
        before = _run(4.0, severity=LeakSeverity.HIGH, samples=41,
                      experiment=_experiment(workload_id="import", operations=1000))
        after = _run(0.0, samples=11, experiment=_experiment(workload_id="import", operations=100))
        result = self._compare(before, after)
        self.assertEqual(result.measured_verdict, IMPROVED)
        self.assertEqual(result.verdict, IMPROVED)
        self.assertTrue(result.qualified)
        self.assertEqual(result.verdict_label, "improved (qualified)")
        self.assertIn("(qualified by 2 caveats)", result.headline())
        self.assertEqual([caveat.kind for caveat in result.caveats], ["operations", "duration"])

        strict = self._compare(before, after, strict=True)
        self.assertEqual(strict.verdict, NOT_COMPARABLE)
        self.assertEqual(strict.verdict_label, NOT_COMPARABLE)
        self.assertFalse(strict.qualified)
        self.assertTrue(strict.headline().startswith("not comparable (operations, duration);"))
        data = strict.to_dict()
        self.assertEqual((data["verdict"], data["measured_verdict"], data["strict"]),
                         ("not comparable", "improved", True))
        self.assertEqual(data["caveats"][0]["blocks"], "not comparable")
        self.assertEqual(data["after"]["experiment"]["operations"], 100)

    def test_equivalent_runs_keep_an_unqualified_verdict_in_strict_mode(self) -> None:
        before = _run(4.0, severity=LeakSeverity.HIGH,
                      experiment=_experiment(workload_id="import", build="1.0", operations=1000))
        after = _run(0.0, experiment=_experiment(workload_id="import", build="1.1",
                                                 operations=1040))
        result = self._compare(before, after, strict=True)
        self.assertEqual(result.caveats, ())
        self.assertEqual(result.comparability, COMPARABLE)
        self.assertEqual((result.verdict, result.verdict_label), (IMPROVED, IMPROVED))
        self.assertFalse(result.qualified)

    def test_unjudged_or_partial_runs_are_insufficient_coverage(self) -> None:
        result = self._compare(_run(4.0, severity=LeakSeverity.HIGH),
                               _run(0.0, detection_ran=False), strict=True)
        self.assertEqual(result.verdict, INSUFFICIENT_COVERAGE)
        self.assertEqual(result.measured_verdict, IMPROVED)
        # Different experiments outrank missing coverage.
        other = self._compare(_run(4.0), _run(0.0, detection_ran=False,
                                              target_path="C:\\other.exe"), strict=True)
        self.assertEqual(other.verdict, NOT_COMPARABLE)

    def test_workload_arguments_and_one_sided_labels_block_strict_comparisons(self) -> None:
        cases = (
            (_experiment(workload_id="a"), _experiment(workload_id="b"),
             "different workloads (a and b)"),
            (_experiment(workload_id="a"), None, "only report A names its workload (a)"),
            (_experiment(args=("--port", "1")), _experiment(args=("--port", "2")),
             "launched with different arguments"),
            (_experiment(operations=10), _experiment(), "only report A records an operation"),
        )
        for before, after, message in cases:
            with self.subTest(message=message):
                extra_a = {"experiment": before} if before else {}
                extra_b = {"experiment": after} if after else {}
                result = self._compare(_run(**extra_a), _run(**extra_b), strict=True)
                self.assertIn(message, " | ".join(result.warnings))
                self.assertEqual(result.verdict, NOT_COMPARABLE)

    def test_different_machines_qualify_without_blocking(self) -> None:
        before = _run(4.0, severity=LeakSeverity.HIGH, experiment=_experiment())
        after = _run(0.0, experiment=_experiment(environment={"cpu_count": 4}))
        result = self._compare(before, after, strict=True)
        self.assertEqual(result.verdict_label, "improved (qualified)")
        self.assertIn("different environments (CPUs 8 vs 4)", result.warnings[0])

    def test_reports_without_experiment_sections_compare_as_before(self) -> None:
        result = self._compare(_run(4.0, severity=LeakSeverity.HIGH), _run(0.0), strict=True)
        self.assertEqual((result.verdict, result.qualified), (IMPROVED, False))
        self.assertIsNone(result.to_dict()["before"]["experiment"])

class PeakAndBudgetTests(unittest.TestCase):
    setUp = ComparisonTests.setUp
    _load = ComparisonTests._load

    def _compare(self, before: dict, after: dict, **kwargs):
        return compare_reports(self._load("a.json", before), self._load("b.json", after),
                               **kwargs)

    def test_stable_but_excessive_usage_is_reported_without_a_leak_signal(self) -> None:
        heavy = _run()
        for snapshot in heavy["snapshots"]:
            snapshot["totals"]["private_bytes"] = 700 * MB  # flat, but large
        result = self._compare(_run(), heavy, budgets={"private_bytes": 500 * MB,
                                                       "handles": 2000})
        memory = result.resources[0]
        self.assertEqual((memory.signal, memory.trend), ("clear", UNCHANGED))
        self.assertEqual(memory.peak_after, 700 * MB)
        self.assertEqual([check.exceeded for check in result.budgets], [True, False])
        self.assertEqual(result.verdict, UNCHANGED)
        self.assertIn("B over budget (private bytes)", result.headline())
        data = result.to_dict()
        self.assertTrue(data["over_budget"])
        self.assertEqual(data["resources"]["memory"]["run_peak_after"], 700 * MB)
        self.assertEqual(data["resources"]["memory"]["metric"], "private_bytes")
        self.assertEqual(self._compare(_run(), _run()).budgets, ())

    def test_growth_is_normalized_per_operation_when_both_runs_count_them(self) -> None:
        before = _run(4.0, experiment=_experiment(operations=1000))
        after = _run(2.0, experiment=_experiment(operations=1050))
        memory = self._compare(before, after).resources[0]
        self.assertTrue(memory.normalized)
        self.assertAlmostEqual(memory.growth_per_operation_before, 80 * MB / 1000)
        self.assertAlmostEqual(memory.growth_per_operation_after, 40 * MB / 1050)
        one_sided = self._compare(_run(4.0, experiment=_experiment(operations=1000)), _run(2.0))
        self.assertIsNone(one_sided.resources[0].growth_per_operation_after)
        self.assertFalse(one_sided.resources[0].normalized)

if __name__ == "__main__":
    unittest.main()
