"""Non-interactive monitoring for CI pipelines and scripted runs."""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from .config import FAIL_ON_CHOICES, MonitorSettings
from .analysis import RESOURCES, summarize
from .budgets import BudgetCheck, check_budgets
from .dashboard import DashboardApp
from .events import EvidenceState, LeakResource, LeakSeverity, severity_rank, unavailable_reason
from .report import DEFAULT_REPORT_DIR
from .report_diff import format_amount, format_drift
from .session import MonitorSession
from .theme import PALETTE
from .units import format_duration

logger = logging.getLogger(__name__)

EXIT_PASS = 0
EXIT_LEAK = 1
EXIT_ERROR = 2
# Only with the optional policies; without them these runs exit 0.
EXIT_INCONCLUSIVE = 3
EXIT_TARGET_ERROR = 4
# Only when a budget is set: a peak exceeded it, with or without a leak signal.
EXIT_BUDGET = 5
EXIT_INTERRUPTED = 130


_LEVEL_TAGS = {
    "ok": ("OK", PALETTE.ok),
    "info": ("INFO", PALETTE.accent),
    "warn": ("WARN", PALETTE.warn),
    "alert": ("ALERT", PALETTE.alert),
    "error": ("ERROR", PALETTE.danger),
    "stat": ("STAT", PALETTE.muted),
}


class HeadlessRunner(MonitorSession):
    """Monitor a target without the dashboard and turn the result into an exit code.

    Events and periodic statistics are printed as plain lines suited to CI
    logs. Monitoring ends at the time limit, when the monitored tree exits,
    or on Ctrl+C; a report is then exported and ``run()`` returns
    ``EXIT_LEAK`` if any leak signal reached ``fail_on``. The optional
    ``fail_on_inconclusive`` and ``fail_on_target_error`` policies turn an
    unjudged run or a failing target into their own exit codes; both are
    off by default so existing pipelines keep their results.
    """

    VERSION = DashboardApp.VERSION
    POLL_SECONDS = 0.1
    PROGRESS_SECONDS = 5.0

    def __init__(
        self,
        target_path: str | None,
        args: list[str] | None = None,
        console: Console | None = None,
        report_dir: str | Path = DEFAULT_REPORT_DIR,
        attach_pid: int | None = None,
        timeout: float | None = None,
        fail_on: LeakSeverity = LeakSeverity.HIGH,
        settings: MonitorSettings | None = None,
        fail_on_inconclusive: bool = False,
        fail_on_target_error: bool = False,
        quiet: bool = False,
        require_full_coverage: bool | None = None,
    ) -> None:
        if fail_on == LeakSeverity.NONE:
            raise ValueError("fail_on must be a leak severity, not NONE")
        self.quiet = quiet
        super().__init__(
            target_path,
            args=args,
            console=console,
            report_dir=report_dir,
            attach_pid=attach_pid,
            timeout=timeout,
            settings=settings,
        )
        self.fail_on = fail_on
        self.fail_on_inconclusive = fail_on_inconclusive
        self.fail_on_target_error = fail_on_target_error
        self.require_full_coverage = (
            self.settings.require_full_coverage if require_full_coverage is None else require_full_coverage
        )
        self._github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
        self._export_error: str | None = None
        self._run_error: str | None = None
        self._interrupted = False
        self.budgets = dict(self.settings.budgets)
        # Budgets already reported as exceeded, so the log says it once.
        self._budget_breaches: set[str] = set()
        # The first sample is taken before the launch is logged; its breaches
        # wait so the log does not report a budget before the target started.
        self._start_logged = False

    @property
    def breached(self) -> bool:
        """Whether any leak signal reached the failure threshold."""
        return severity_rank(self.worst_severity) >= severity_rank(self.fail_on)

    def budget_checks(self) -> tuple[BudgetCheck, ...]:
        """Each budget against the run's peak so far, warm-up included."""
        return check_budgets(self.budgets, self._peaks) if self.budgets else ()

    @property
    def over_budget(self) -> tuple[BudgetCheck, ...]:
        return tuple(check for check in self.budget_checks() if check.exceeded)

    def _accept_snapshot(self, snapshot, *, already_observed: bool = False) -> None:
        super()._accept_snapshot(snapshot, already_observed=already_observed)
        if self._start_logged:
            self._log_budget_breaches()

    def _signal_label(self, resource: LeakResource) -> str:
        if self.worst_severity_for(resource) != LeakSeverity.NONE:
            return super()._signal_label(resource)
        if resource in self._judged_resources:
            return "clear"
        snapshot = self.snapshot
        evidence = snapshot.evidence_for(resource) if snapshot is not None else None
        if evidence is not None and evidence.state == EvidenceState.UNAVAILABLE:
            return "unavailable"
        if (snapshot is not None and snapshot.warming_up) or (
            self.detector.warmup_seconds and self.detector.warmup_completed_at is None
        ):
            return "inconclusive (warming up)"
        if evidence is None and self.conclusive:
            return "clear"  # Compatibility for snapshots without per-resource evidence.
        return "inconclusive (collecting)"

    def _log_start(self) -> None:
        super()._log_start()
        self._start_logged = True
        self._log_budget_breaches()

    def _log_budget_breaches(self) -> None:
        for check in self.over_budget:
            if check.metric not in self._budget_breaches:
                self._budget_breaches.add(check.metric)
                self._append_event(f"Budget exceeded: {check.describe()}", "alert")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Monitor until the limit, tree exit, or Ctrl+C; return an exit code."""
        self._print_banner()
        try:
            self.start()
        except Exception as exc:
            self._startup_state = "failed"
            self._startup_error = str(exc) or type(exc).__name__
            self._run_error = f"startup failed: {self._startup_error}"
            self._print_line("error", f"Could not start monitoring: {exc}")
            hint = self.startup_hint(exc)
            if hint:
                self._print_line("info", hint)
            self._export()
            self._print_summary()
            return EXIT_ERROR

        try:
            self._monitor()
        except KeyboardInterrupt:
            self._interrupted = True
            self._append_event("Interrupted; stopping early", "warn")
        except Exception as exc:
            self._fail("Monitoring", exc)
        finally:
            try:
                self.shutdown()
            except KeyboardInterrupt:
                self._interrupted = True
                self._append_event("Cleanup interrupted; exporting available evidence", "warn")
            except Exception as exc:  # noqa: BLE001 - export evidence after any cleanup failure
                self._fail("Cleanup", exc)
        try:
            self.poll()
        except Exception as exc:
            self._fail("Processing the last samples", exc)

        self._export()
        self._print_summary()
        return self.exit_status()

    def _fail(self, action: str, exc: Exception) -> None:
        """Record an unexpected error; the run still stops its target and exports."""
        logger.exception("%s failed", action)
        self._run_error = f"{action.lower()} failed: {exc}"
        self._append_event(f"{action} failed: {exc}", "error")

    def _monitor(self) -> None:
        """Poll until the monitored tree exits or the time limit passes."""
        next_progress = time.monotonic() + self.PROGRESS_SECONDS
        while True:
            self.poll()
            if self.tree_finished:
                self._record_final_sample()
                self._append_event("Monitored process tree exited", "info")
                return
            if self.timed_out:
                self._append_event(
                    f"Time limit of {format_duration(self.timeout or 0.0)} reached", "info"
                )
                return
            now = time.monotonic()
            if now >= next_progress:
                self._print_progress()
                next_progress = now + self.PROGRESS_SECONDS
            time.sleep(self.POLL_SECONDS)

    def _record_final_sample(self) -> None:
        """Sample once more so the exit itself reaches the log and report.

        The detector samples on its own schedule and may not have seen the
        tree disappear yet. Stopping it first keeps the extra sample from
        racing the background thread.
        """
        self.detector.stop()
        self.poll()
        if self.detector.is_running:
            return
        try:
            self._accept_snapshot(self.detector.sample_once())
        except Exception as exc:  # the tree is gone; a failed sample is harmless
            self._print_line("warn", f"Final sample failed: {exc}")

    def build_report(self) -> dict[str, Any]:
        report = super().build_report()
        report["headless"] = {
            "fail_on": self.fail_on.value,
            "breached": self.breached,
            "fail_on_inconclusive": self.fail_on_inconclusive,
            "fail_on_target_error": self.fail_on_target_error,
            "require_full_coverage": self.require_full_coverage,
            "exit_code": self.exit_status(),
            "budgets": [check.to_dict() for check in self.budget_checks()],
        }
        if self._run_error is not None:
            report["outcome"]["monitor_errors"].append(self._run_error)
        return report

    def exit_status(self) -> int:
        """Map the session outcome to the process exit code.

        A leak signal wins, then an interruption, then a monitor or export
        error, then an exceeded budget; the optional policies only apply to
        runs that are otherwise a pass.
        """
        if self._startup_state == "failed":
            return EXIT_ERROR
        if self.breached:
            return EXIT_LEAK
        if self._interrupted:
            return EXIT_INTERRUPTED
        if self._export_error is not None or self._run_error is not None:
            return EXIT_ERROR
        if self.over_budget:
            return EXIT_BUDGET
        if self.fail_on_target_error and self.target_failed:
            return EXIT_TARGET_ERROR
        if self.require_full_coverage and not self.full_coverage:
            return EXIT_INCONCLUSIVE
        if self.fail_on_inconclusive and not self.conclusive:
            return EXIT_INCONCLUSIVE
        return EXIT_PASS

    def _policy_text(self) -> str:
        """``fails on HIGH or above``, plus any optional policy in force."""
        text = f"fails on {self.fail_on.value.upper()} or above"
        if self.fail_on_inconclusive:
            text += ", inconclusive runs"
        if self.fail_on_target_error:
            text += ", target errors"
        if self.require_full_coverage:
            text += ", incomplete coverage"
        if self.budgets:
            text += ", budgets"
        return text

    def _export(self) -> None:
        try:
            self.export_log()
        except Exception as exc:
            if not isinstance(exc, OSError):
                logger.exception("Report export failed")
            self._export_error = str(exc)
            self._print_line("error", f"Report export failed: {exc}")

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _append_event(self, message: str, level: str = "info") -> None:
        super()._append_event(message, level)
        self._print_line(level, message)

    def _print_line(self, level: str, message: str | Text) -> None:
        tag, color = _LEVEL_TAGS.get(level, _LEVEL_TAGS["info"])
        if self.quiet and tag in {"OK", "INFO", "STAT"}:
            return
        line = Text.assemble(
            (self._format_duration(self._uptime()), PALETTE.muted),
            "  ",
            (f"{tag:<5}", f"bold {color}"),
            "  ",
            message,
        )
        self.console.print(line, soft_wrap=True)

    def _print_banner(self) -> None:
        if self.quiet:
            for warning in replace(self.settings, timeout=self.timeout).warnings():
                self._print_line("warn", warning)
            return
        sep = self.glyphs.sep
        if self.attached:
            action = f"attaching to PID {self.attach_pid}"
            name = Path(self.target_path).name
            if name != f"PID {self.attach_pid}":
                action += f" ({name})"
        else:
            action = f"launching {Path(self.target_path).name}"
        limit = (
            f"time limit {format_duration(self.timeout)}"
            if self.timeout
            else "until the process tree exits"
        )
        warmup = self.detector.warmup_seconds
        warmup_text = f"warm-up {format_duration(warmup)} {sep} " if warmup else ""
        self.console.print(
            Text.assemble(
                (f"{self.TITLE} {self.VERSION} headless", f"bold {PALETTE.accent}"),
                (f" {sep} {action} {sep} {limit} {sep} {warmup_text}", PALETTE.muted),
                (self._policy_text(), PALETTE.muted),
            ),
            soft_wrap=True,
        )
        if self.settings.source is not None:
            self.console.print(
                Text(f"Using {self.settings.describe()}", style=PALETTE.muted), soft_wrap=True
            )
        for warning in replace(self.settings, timeout=self.timeout).warnings():
            self._print_line("warn", warning)

    def _metric_text(self, metric: str, *, peak: bool = False) -> str:
        if peak:
            highest = self.facts.peak(metric)
            value = highest[0] if highest is not None else None
        else:
            snapshot = self.snapshot
            coverage = (snapshot.coverage or {}).get(metric) if snapshot is not None else None
            measured = snapshot is not None and any(
                not process.access_denied and process.measured(metric)
                for process in snapshot.processes
            )
            value = snapshot.totals.get(metric) if measured and (
                coverage is None or coverage.measured > 0
            ) else None
        if value is None:
            return "n/a"
        if metric.endswith("_bytes"):
            return self._format_bytes(value)
        return f"{value:.1f}%" if metric == "cpu_percent" else f"{int(value):,}"

    def _print_progress(self) -> None:
        sep = self.glyphs.sep
        snapshot = self.snapshot
        counts = snapshot.process_counts if snapshot is not None else None
        processes = counts.live if counts is not None else (
            len(snapshot.processes) if snapshot is not None else 0
        )
        severity = self.snapshot.severity if self.snapshot else LeakSeverity.NONE
        active = ", ".join(
            alert.resource.value for alert in (self.snapshot.alerts if self.snapshot else ())
        )
        signal = (severity.value.upper() if severity != LeakSeverity.NONE else "clear") + (
            f" ({active})" if active else ""
        )
        if self.snapshot is not None and self.snapshot.warming_up:
            left = format_duration(max(1.0, math.ceil(self.detector.warmup_remaining)))
            signal = f"WARMING UP ({left} left)"
        elif self.snapshot is not None and not active:
            signal = self._evidence_text(self.snapshot) or signal
        self._print_line(
            "stat",
            f"cpu {self._metric_text('cpu_percent')} {sep} "
            # Private bytes first: they drive the memory signal; RSS is context.
            f"private {self._metric_text('private_bytes')} {sep} "
            f"rss {self._metric_text('rss_bytes')} {sep} "
            f"handles {self._metric_text('handles')} {sep} "
            f"threads {self._metric_text('threads')} {sep} "
            f"{processes} live process{'es' if processes != 1 else ''} {sep} "
            f"signal {signal}",
        )

    @staticmethod
    def _evidence_text(snapshot) -> str:
        """The detector's evidence for a progress line without an active signal."""
        states = {item.state for item in snapshot.evidence}
        if not states:
            return ""
        if states == {EvidenceState.UNAVAILABLE}:
            text = f"UNAVAILABLE ({unavailable_reason(snapshot.evidence)})"
        elif EvidenceState.COLLECTING in states:
            least = min(
                (item for item in snapshot.evidence if item.state == EvidenceState.COLLECTING),
                key=lambda item: item.elapsed_seconds,
            )
            text = (
                f"collecting {format_duration(least.elapsed_seconds)}"
                f"/{format_duration(least.needed_seconds)}"
            )
        elif states == {EvidenceState.NO_GROWTH}:
            window = min(item.evaluated_window_seconds or 0.0 for item in snapshot.evidence)
            text = f"no sustained growth ({format_duration(window)} window)"
        else:
            return ""
        unreadable = len(snapshot.unmeasured_pids)
        if unreadable and states != {EvidenceState.UNAVAILABLE}:
            text += f", partial: {unreadable} unreadable"
        return text

    def _measurement_text(self) -> str:
        """Measurement outcome for the summary: conclusive, partial, or inconclusive."""
        outcome = self.measurement_outcome
        text = outcome
        if not self.conclusive:
            text += f": {self.inconclusive_reason()}"
        coverage = self.coverage_text()
        if coverage:
            text += f" {self.glyphs.sep} {coverage}"
        return text

    def _print_summary(self) -> None:
        sep = self.glyphs.sep
        status = self.exit_status()
        verdict, color = {
            EXIT_PASS: ("PASS", PALETTE.ok),
            EXIT_LEAK: ("FAIL", PALETTE.danger),
            EXIT_ERROR: ("ERROR", PALETTE.danger),
            EXIT_INCONCLUSIVE: ("INCONCLUSIVE", PALETTE.danger),
            EXIT_TARGET_ERROR: ("TARGET ERROR", PALETTE.danger),
            EXIT_BUDGET: ("OVER BUDGET", PALETTE.danger),
            EXIT_INTERRUPTED: ("INTERRUPTED", PALETTE.warn),
        }[status]
        worst = self.worst_severity
        reason = (
            "no leak signals observed"
            if worst == LeakSeverity.NONE
            else f"worst leak signal {worst.value.upper()}"
        )
        if status == EXIT_ERROR:
            reason = self._run_error or f"the report could not be written: {self._export_error}"
        elif status == EXIT_TARGET_ERROR:
            reason = f"the target exited with code {self.manager.exit_code}"
        elif status == EXIT_INCONCLUSIVE:
            if self.require_full_coverage and not self.full_coverage:
                verdict = "INSUFFICIENT COVERAGE"
                reason = "full private-byte, handle, and thread coverage was not observed for every live sample"
            else:
                reason = self.inconclusive_reason()
        elif status == EXIT_BUDGET:
            reason = "; ".join(check.describe() for check in self.over_budget)
        if status == EXIT_PASS and not self.conclusive:
            # Still exit 0: a short smoke run is not a failure, but say so.
            verdict, color = "INCONCLUSIVE", PALETTE.warn
            reason = self.inconclusive_reason()
            self._print_line("warn", f"Inconclusive: {reason}")
        coverage = self.coverage_text()
        if coverage:
            self._print_line("warn", f"Partial coverage: {coverage}; totals leave them out")

        errors = list(self.monitor_errors)
        if self._run_error is not None:
            errors.append(self._run_error)
        if self._export_error is not None:
            errors.append(f"report export failed: {self._export_error}")
        rows = [
            ("target", f"{Path(self.target_path).name} {sep} PID {self.root_pid or '-'} "
                       f"({self.outcome_text()})"),
            ("measured", self._measurement_text()),
            ("duration", f"{self._format_duration(self._uptime())} {sep} "
                         f"{self._sample_count:,} samples"),
            ("peaks", f"private {self._metric_text('private_bytes', peak=True)} {sep} "
                      f"RSS (working set) {self._metric_text('rss_bytes', peak=True)} {sep} "
                      f"{self._metric_text('handles', peak=True)} handles {sep} "
                      f"{self._metric_text('threads', peak=True)} threads"),
            ("signals", self.signals_text()),
        ]
        checks = self.budget_checks()
        if checks:
            rows.append(("budgets", f" {sep} ".join(check.describe() for check in checks)))
        warmup = self.warmup_text()
        if warmup:
            rows.append(("warm-up", warmup))
        monitor = self.monitor_cost_text()
        if monitor:
            rows.append(("monitor", monitor))
        if errors:
            rows.append(("errors", f" {sep} ".join(errors)))
        if self._last_report is not None:
            rows.append(("report", str(self._last_report)))
        if self._last_html_report is not None:
            rows.append(("html", str(self._last_html_report)))

        self.console.print()
        for label, value in rows:
            self.console.print(
                Text.assemble((f"  {label:<10}", f"bold {PALETTE.muted}"), value),
                soft_wrap=True,
            )
        self.console.print(
            Text.assemble(
                ("RESULT ", "bold"),
                (f" {verdict} ", f"bold {PALETTE.ink} on {color}"),
                f"  {reason} {sep} {self._policy_text()}",
            ),
            soft_wrap=True,
        )
        self._annotate(status, reason)
        self._write_step_summary(verdict, reason)

    def _write_step_summary(self, verdict: str, reason: str) -> None:
        """Append a Markdown result table to the GitHub Actions job summary."""
        destination = os.environ.get("GITHUB_STEP_SUMMARY")
        if not self._github_actions or not destination:
            return
        lines = [
            f"### DrainTracer leak check: {verdict}",
            "",
            f"**{_markdown(Path(self.target_path).name)}** · {_markdown(self.outcome_text())} · "
            f"{format_duration(self._uptime())} · {_markdown(self._policy_text())}",
            "",
            f"Measurement: {_markdown(self._measurement_text())}",
            "",
        ]
        if self._last_report_data is not None:
            summary = summarize(self._last_report_data)
            lines += [
                "| Resource | Strongest signal | Growth | Drift | Peak |",
                "| --- | --- | --- | --- | --- |",
            ]
            for resource in RESOURCES:
                stats = summary.resources[resource]
                signal = self._signal_label(LeakResource(resource))
                if stats.worst != "none":
                    signal = f"**{signal}**"
                lines.append(
                    f"| {resource} | {signal} | {format_amount(resource, stats.growth, signed=True)}"
                    f" | {format_drift(resource, stats.drift_per_second)}"
                    f" | {format_amount(resource, stats.peak)} |"
                )
            lines.append("")
        else:
            lines += [_markdown(self.signals_text()), ""]
        lines.append(_markdown(reason[:1].upper() + reason[1:]) + ".")
        if self._last_report is not None:
            lines.append("")
            lines.append(f"Report: `{self._last_report}` (HTML alongside)")
        try:
            # Windows names may hold unpaired surrogates that UTF-8 cannot.
            with open(destination, "a", encoding="utf-8", errors="replace") as handle:
                handle.write("\n".join(lines) + "\n\n")
        except OSError as exc:
            self._print_line("warn", f"Could not write the GitHub step summary: {exc}")

    def _annotate(self, status: int, reason: str) -> None:
        """Surface the verdict as a GitHub Actions annotation on CI runs."""
        if not self._github_actions:
            return
        if status in (EXIT_LEAK, EXIT_ERROR, EXIT_INCONCLUSIVE, EXIT_TARGET_ERROR, EXIT_BUDGET):
            command = "error"
        elif (
            self.worst_severity != LeakSeverity.NONE
            or status != EXIT_PASS
            or not self.conclusive
            or self.partial_coverage
        ):
            command = "warning"
        else:
            return
        message = f"{Path(self.target_path).name}: {reason} ({self._policy_text()})"
        if self._last_report is not None:
            message += f". Report: {self._last_report}"
        title = _escape_property("DrainTracer leak check")
        self.console.print(
            f"::{command} title={title}::{_escape_data(message)}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )


def _markdown(value: str) -> str:
    """Escape text for a GitHub Markdown table cell."""
    for character in "\\`*_[]<>|":
        value = value.replace(character, "\\" + character)
    return value.replace("\r", " ").replace("\n", " ")


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


__all__ = [
    "EXIT_BUDGET",
    "EXIT_ERROR",
    "EXIT_INCONCLUSIVE",
    "EXIT_INTERRUPTED",
    "EXIT_LEAK",
    "EXIT_PASS",
    "EXIT_TARGET_ERROR",
    "FAIL_ON_CHOICES",
    "HeadlessRunner",
]
