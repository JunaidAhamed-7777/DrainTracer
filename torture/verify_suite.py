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
from src.leak_detector import LeakDetector


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
    headless: bool = False
    fail_on: str = "high"
    expected_exit_code: int = 0


@dataclass(frozen=True)
class CaseResult:
    case: TortureCase
    passed: bool
    max_processes: int
    alert_resources: tuple[str, ...]
    errors: tuple[str, ...]
    report_path: Path


TORTURE_ROOT = Path(__file__).resolve().parent
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
        )
    else:
        app = DashboardApp(
            str(case.target),
            args=list(case.args),
            console=console,
            report_dir=reports_dir / case.name,
        )
    app.detector = LeakDetector(
        app.manager,
        sample_interval_ms=100,
        window_seconds=(4.0,),
    )

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
    try:
        for case in selected:
            result = _run_case(case, args.monitor_seconds, reports_dir)
            status = "PASS" if result.passed else "FAIL"
            alerts = ",".join(result.alert_resources) or "none"
            print(
                f"{status:4}  {case.name:22} "
                f"processes={result.max_processes:<2} alerts={alerts}"
            )
            for error in result.errors:
                print(f"      - {error}")
            failures += int(not result.passed)
    finally:
        if temporary_reports is not None:
            temporary_reports.cleanup()

    print(f"\n{len(selected) - failures}/{len(selected)} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
