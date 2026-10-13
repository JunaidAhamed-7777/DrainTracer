"""Run every supported torture target through the real DrainTracer dashboard."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.dashboard import DashboardApp
from src.events import LeakSeverity
from src.headless import HeadlessRunner
from src.config import resolve_settings
from src.report_diff import DiffError, compare_reports, load_report


@dataclass(frozen=True)
class TortureCase:
    name: str
    target: Path
    args: tuple[str, ...] = ()
    expected_alert: str | None = None
    expect_no_alerts: bool = False
    min_processes: int = 1
    expect_root_exit: bool = False
    expect_root_exit_with_child: bool = False
    attach: bool = False
    # The handle category a handles alert must attribute most growth to.
    expected_handle_type: str | None = None
    headless: bool = False
    fail_on: str = "high"
    expected_exit_code: int = 0
    # A profile from examples/drainrc.toml applied under the suite's
    # short sampling settings.
    profile: str | None = None
    warmup: float = 0.0
    # Overrides --monitor-seconds for cases that need longer to judge.
    monitor_seconds: float | None = None


@dataclass(frozen=True)
class CaseResult:
    case: TortureCase
    passed: bool
    max_processes: int
    alert_resources: tuple[str, ...]
    errors: tuple[str, ...]
    report_path: Path


TORTURE_ROOT = Path(__file__).resolve().parent
EXAMPLE_SETTINGS = REPOSITORY_ROOT / "examples" / "drainrc.toml"
CASES = (
    TortureCase(
        name="steady-python",
        target=TORTURE_ROOT / "steady.py",
        args=("--duration", "30"),
        expect_no_alerts=True,
    ),
    TortureCase(
        name="memory-growth",
        target=TORTURE_ROOT / "memory_leak.py",
        args=("--duration", "30", "--max-mb", "128"),
        expected_alert="memory",
    ),
    TortureCase(
        name="handle-growth",
        target=TORTURE_ROOT / "handle_leak.py",
        args=("--duration", "30", "--max-handles", "800"),
        expected_alert="handles",
        expected_handle_type="file",
    ),
    TortureCase(
        name="socket-growth",
        target=TORTURE_ROOT / "socket_leak.py",
        args=("--duration", "30", "--max-connections", "300"),
        expected_alert="handles",
        expected_handle_type="socket",
    ),
    TortureCase(
        name="memory-growth-tolerated",
        target=TORTURE_ROOT / "memory_leak.py",
        args=("--duration", "30", "--max-mb", "128"),
        expect_no_alerts=True,
        profile="heavy",
    ),
    TortureCase(
        name="startup-spike",
        target=TORTURE_ROOT / "startup_spike.py",
        args=("--duration", "30", "--startup-seconds", "2.5", "--startup-mb", "96"),
        expected_alert="memory",
    ),
    TortureCase(
        name="startup-spike-warmup",
        target=TORTURE_ROOT / "startup_spike.py",
        args=("--duration", "30", "--startup-seconds", "2.5", "--startup-mb", "96"),
        expect_no_alerts=True,
        warmup=3.0,
        monitor_seconds=9.0,
    ),
    TortureCase(
        name="thread-growth",
        target=TORTURE_ROOT / "thread_leak.py",
        args=("--duration", "30", "--max-threads", "80"),
        expected_alert="threads",
    ),
    TortureCase(
        name="recovering-bursts",
        target=TORTURE_ROOT / "burst_recovery.py",
        args=("--duration", "30"),
        expect_no_alerts=True,
    ),
    TortureCase(
        name="attach-memory-growth",
        target=TORTURE_ROOT / "memory_leak.py",
        args=("--duration", "30", "--max-mb", "128"),
        expected_alert="memory",
        attach=True,
    ),
    TortureCase(
        name="root-exits-first",
        target=TORTURE_ROOT / "parent_exits.py",
        args=("--parent-delay", "1.0", "--child-duration", "15"),
        min_processes=2,
        expect_root_exit=True,
        expect_root_exit_with_child=True,
    ),
    TortureCase(
        name="batch-process-tree",
        target=TORTURE_ROOT / "process_tree.bat",
        args=(
            "--duration",
            "30",
            "--depth",
            "2",
            "--fanout",
            "2",
            "--memory-mb",
            "1",
        ),
        min_processes=4,
    ),
    TortureCase(
        name="command-quick-exit",
        target=TORTURE_ROOT / "quick_exit.cmd",
        expect_root_exit=True,
    ),
    TortureCase(
        name="steady-executable",
        target=TORTURE_ROOT / "fixtures" / "steady.exe",
        args=("--duration", "30", "--memory-mb", "8", "--threads", "2"),
        expect_no_alerts=True,
    ),
    TortureCase(
        name="headless-memory-fails",
        target=TORTURE_ROOT / "memory_leak.py",
        args=("--duration", "30", "--max-mb", "128"),
        expected_alert="memory",
        headless=True,
        fail_on="medium",
        expected_exit_code=1,
    ),
    TortureCase(
        name="headless-steady-passes",
        target=TORTURE_ROOT / "steady.py",
        args=("--duration", "30"),
        expect_no_alerts=True,
        headless=True,
        fail_on="low",
    ),
)


def _monitor_seconds(value: str) -> float:
    parsed = float(value)
    if parsed < 5.0:
        raise argparse.ArgumentTypeError(
            "monitor duration must be at least 5 seconds"
        )
    return parsed


def _run_case(
    case: TortureCase,
    monitor_seconds: float,
    reports_dir: Path,
) -> CaseResult:
    errors: list[str] = []
    if not case.target.is_file():
        return CaseResult(
            case=case,
            passed=False,
            max_processes=0,
            alert_resources=(),
            errors=(f"target does not exist: {case.target}",),
            report_path=reports_dir / case.name,
        )

    capture = io.StringIO()
    console = Console(
        file=capture,
        force_terminal=False,
        color_system=None,
        width=140,
    )
    # Short samples and a 4 s window keep every case to a few seconds.
    settings = resolve_settings(
        config_path=EXAMPLE_SETTINGS if case.profile else None,
        profile=case.profile,
        discover=False,
        overrides={"sample_interval_ms": 100, "windows": (4.0,), "warmup": case.warmup},
    )
    monitor_seconds = case.monitor_seconds or monitor_seconds
    external: subprocess.Popen[bytes] | None = None
    app: DashboardApp | HeadlessRunner
    if case.headless:
        # The runner stops itself at the time limit and exports on exit.
        app = HeadlessRunner(
            str(case.target),
            args=list(case.args),
            console=console,
            report_dir=reports_dir / case.name,
            timeout=monitor_seconds,
            fail_on=LeakSeverity(case.fail_on),
            settings=settings,
        )
    elif case.attach:
        # Start the target outside DrainTracer, then attach by PID.
        external = subprocess.Popen(
            [sys.executable, str(case.target), *case.args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        app = DashboardApp(
            None,
            console=console,
            report_dir=reports_dir / case.name,
            attach_pid=external.pid,
            settings=settings,
        )
    else:
        app = DashboardApp(
            str(case.target),
            args=list(case.args),
            console=console,
            report_dir=reports_dir / case.name,
            settings=settings,
        )
    app.detector.handle_types_interval = 1.0

    timer: threading.Timer | None = None
    if not case.headless:
        timer = threading.Timer(
            monitor_seconds,
            setattr,
            args=(app, "_quit_requested", True),
        )
        timer.daemon = True
        timer.start()
    try:
        exit_code = app.run()
    finally:
        if timer is not None:
            timer.cancel()
        attached_survived = external is not None and external.poll() is None
        if external is not None:
            external.kill()
            external.wait(timeout=10)

    if case.attach and not attached_survived:
        errors.append("quitting an attached session terminated the target")

    if exit_code != case.expected_exit_code:
        errors.append(
            f"{'headless run' if case.headless else 'dashboard'} returned exit code "
            f"{exit_code}; expected {case.expected_exit_code}"
        )
        rendered = capture.getvalue().strip()
        if rendered:
            errors.append(f"output ended with: {rendered[-300:]}")

    report_path = reports_dir / case.name
    try:
        if case.headless:
            if app._last_report is None:
                raise OSError("the headless run did not export a report")
            report_path = app._last_report
        else:
            report_path = app.export_log()
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"report export failed: {exc}")
        report = {"snapshots": []}
    if report_path.name != f"leak_report_{report.get('report_id')}.json":
        errors.append(f"report filename {report_path.name!r} does not embed its UUID")
    if not report.get("created_at"):
        errors.append("report has no ISO 8601 created_at timestamp")
    html_path = app._last_html_report
    if html_path is None or not html_path.is_file():
        errors.append("no HTML report was written next to the JSON report")
    elif html_path.with_suffix(".json") != report_path:
        errors.append(f"HTML report {html_path.name!r} does not match {report_path.name!r}")
    elif 'id="drain-data"' not in html_path.read_text(encoding="utf-8"):
        errors.append("HTML report has no embedded chart data")

    snapshots = report.get("snapshots", [])
    if not snapshots:
        errors.append("no snapshots were exported")

    max_processes = max(
        (
            max(
                len(snapshot.get("processes", [])),
                len(snapshot.get("metadata", {}).get("tracked_pids", [])),
            )
            for snapshot in snapshots
        ),
        default=0,
    )
    alert_resources = tuple(
        sorted(
            {
                alert.get("resource", "unknown")
                for snapshot in snapshots
                for alert in snapshot.get("alerts", [])
            }
        )
    )
    saw_root_exit = any(
        snapshot.get("process_alive") is False for snapshot in snapshots
    )
    saw_root_exit_with_child = any(
        snapshot.get("process_alive") is False
        and bool(snapshot.get("processes"))
        for snapshot in snapshots
    )

    if max_processes < case.min_processes:
        errors.append(
            f"observed at most {max_processes} processes; "
            f"expected at least {case.min_processes}"
        )
    if case.expected_handle_type:
        attributed = {
            max(growth, key=growth.get)
            for snapshot in snapshots
            for alert in snapshot.get("alerts", [])
            if alert.get("resource") == "handles"
            and (growth := alert.get("growth_by_type"))
        }
        if case.expected_handle_type not in attributed:
            errors.append(
                f"handle alerts were not attributed to {case.expected_handle_type!r} "
                f"handles; top types seen: {sorted(attributed) or 'none'}"
            )
    if case.expected_alert and case.expected_alert not in alert_resources:
        errors.append(
            f"missing {case.expected_alert!r} alert; "
            f"observed {list(alert_resources)}"
        )
    if case.expect_no_alerts and alert_resources:
        errors.append(f"unexpected alerts: {list(alert_resources)}")
    if case.expect_root_exit and not saw_root_exit:
        errors.append("the root process was never observed exiting")
    if case.expect_root_exit_with_child and not saw_root_exit_with_child:
        errors.append("no snapshot retained a child after the root exited")
    if app.manager.has_tracked_processes:
        errors.append(
            "DrainTracer cleanup left tracked processes: "
            f"{sorted(app.manager.tracked_pids)}"
        )
    if case.warmup:
        warmup = report.get("warmup") or {}
        if not warmup.get("completed") or not report.get("detection_ran"):
            errors.append(f"the {case.warmup:g}s warm-up did not hand over to detection: {warmup}")
    recorded = report.get("settings") or {}
    if recorded.get("profile") != case.profile or recorded.get("windows_seconds") != [4.0]:
        errors.append(f"report settings do not match the case: {recorded}")
    if report.get("version") != DashboardApp.VERSION:
        errors.append(
            f"report version {report.get('version')!r} did not match "
            f"{DashboardApp.VERSION!r}"
        )

    return CaseResult(
        case=case,
        passed=not errors,
        max_processes=max_processes,
        alert_resources=alert_resources,
        errors=tuple(errors),
        report_path=report_path,
    )


# Report pairs compared with the diff command after their cases ran: the
# tolerant profile must turn the memory signal into a resolved one.
DIFF_CHECKS = (("memory-growth", "memory-growth-tolerated", "memory", "resolved"),)


def _check_diff(
    results: dict[str, CaseResult], before: str, after: str, resource: str, expected: str
) -> tuple[bool, str, list[str]]:
    """Compare two cases' reports; return (passed, summary, errors)."""
    try:
        comparison = compare_reports(
            load_report(results[before].report_path), load_report(results[after].report_path)
        )
    except DiffError as exc:
        return False, "verdict=error", [str(exc)]
    item = next(entry for entry in comparison.resources if entry.resource == resource)
    errors = []
    if item.signal != expected:
        errors.append(f"{resource} signal was {item.signal!r}; expected {expected!r}")
    return not errors, f"verdict={comparison.verdict} {resource}={item.signal}", errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        action="append",
        choices=[case.name for case in CASES],
        help="Run only this case. Repeat the option to select multiple cases.",
    )
    parser.add_argument(
        "--monitor-seconds",
        type=_monitor_seconds,
        default=5.5,
        help="Per-case dashboard runtime (minimum: 5; default: 5.5).",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        help="Keep exported JSON reports in this directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    selected = [
        case for case in CASES if not args.case or case.name in args.case
    ]

    os.environ["DRAINTRACER_PYTHON"] = sys.executable
    temporary_reports: tempfile.TemporaryDirectory[str] | None = None
    if args.reports_dir is None:
        temporary_reports = tempfile.TemporaryDirectory(
            prefix="draintracer-reports-"
        )
        reports_dir = Path(temporary_reports.name)
    else:
        reports_dir = args.reports_dir.resolve()
        reports_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    checks = 0
    name_width = max(len(case.name) for case in selected)
    results: dict[str, CaseResult] = {}
    try:
        for case in selected:
            result = _run_case(case, args.monitor_seconds, reports_dir)
            results[case.name] = result
            status = "PASS" if result.passed else "FAIL"
            alerts = ",".join(result.alert_resources) or "none"
            print(
                f"{status:4}  {case.name:{name_width}} "
                f"processes={result.max_processes:<2} alerts={alerts}"
            )
            for error in result.errors:
                print(f"      - {error}")
            failures += int(not result.passed)

        for before, after, resource, expected in DIFF_CHECKS:
            if before not in results or after not in results:
                continue
            checks += 1
            passed, summary, errors = _check_diff(results, before, after, resource, expected)
            print(f"{'PASS' if passed else 'FAIL':4}  diff {before} -> {after}  {summary}")
            for error in errors:
                print(f"      - {error}")
            failures += int(not passed)
    finally:
        if temporary_reports is not None:
            temporary_reports.cleanup()

    total = len(selected) + checks
    print(f"\n{total - failures}/{total} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
