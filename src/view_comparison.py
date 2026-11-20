"""Compare two running processes or the monitored session and a saved report."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from queue import Empty, Queue

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from .analysis import summarize
from .diff_command import render_comparison
from .keys import BACKSPACE, DOWN, END, ENTER, ESCAPE, HOME, PAGE_DOWN, PAGE_UP, UP
from .report_diff import LoadedReport, ReportComparison, compare_reports, load_report
from .running_processes import ProcessReading, sample_process
from .units import format_bytes, safe_label
from .widgets import Sized

ProcessPair = tuple[ProcessReading, ProcessReading]


class ComparisonViewMixin:
    """Publish comparison results on the UI thread while monitoring continues."""

    def _init_comparison_view(self) -> None:
        self._comparison_mode = "choose"
        self._pid_draft: str | None = None
        self._live_pids: tuple[int, int] | None = None
        self._live_pair: ProcessPair | None = None
        self._live_job: tuple[tuple[int, int], Future[ProcessPair]] | None = None
        self._live_due = 0.0
        self._live_error: str | None = None
        self._baseline_draft: str | None = None
        self._baseline_report: LoadedReport | None = None
        self._comparison: ReportComparison | None = None
        self._comparison_error: str | None = None
        self._comparison_offset = 0
        self._comparison_loading = False
        self._comparison_worker: threading.Thread | None = None
        self._comparison_results: Queue = Queue(maxsize=1)
        self._comparison_at: float | None = None

    def _comparison_input_key(self, key: str) -> bool:
        if self._pid_draft is not None:
            if key == ESCAPE:
                self._pid_draft = None
                self._live_error = None
                if self._live_pids is None:
                    self._comparison_mode = "choose"
                return False
            if key == ENTER:
                fields = self._pid_draft.replace(",", " ").split()
                if (len(fields) != 2 or not all(s.isascii() and s.isdecimal() for s in fields)
                        or not all(0 < int(s) <= 0xffffffff for s in fields)):
                    self._live_error = "Enter two positive PIDs separated by a space."
                elif int(fields[0]) == int(fields[1]):
                    self._live_error = "Choose two different PIDs."
                else:
                    self._live_pids = int(fields[0]), int(fields[1])
                    self._pid_draft = None
                    self._live_pair = None
                    self._live_error = None
                    self._live_due = 0.0
                    self._poll_live_pair()
            elif key == BACKSPACE:
                self._pid_draft = self._pid_draft[:-1]
            elif len(key) == 1 and key in "0123456789, " and len(self._pid_draft) < 32:
                self._pid_draft += key
            return True
        if self._baseline_draft is None:
            return False
        if key == ESCAPE:
            self._baseline_draft = None
            return False
        elif key == ENTER:
            value, self._baseline_draft = self._baseline_draft.strip(), None
            if value:
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = value[1:-1]
                try:
                    path = Path(value).expanduser()
                except (OSError, RuntimeError, ValueError) as exc:
                    self._comparison_error = str(exc)
                else:
                    self._request_comparison(path)
        elif key == BACKSPACE:
            self._baseline_draft = self._baseline_draft[:-1]
        elif len(key) == 1 and key.isprintable() and len(self._baseline_draft) < 4096:
            self._baseline_draft += key
        return True

    def _comparison_key(self, key: str) -> bool:
        self._drain_comparison_result()
        if key.lower() == "p":
            self._comparison_mode = "live"
            self._comparison_offset = 0
            self._live_error = None
            self._pid_draft = ""
            return True
        if key.lower() == "b":
            self._comparison_mode = "report"
            self._comparison_offset = 0
            if not self._comparison_loading:
                self._baseline_draft = ""
            return True
        if key.lower() == "r" and self._comparison_mode == "report":
            if self._baseline_report is not None and not self._comparison_loading:
                self._request_comparison(None)
            return True
        steps = {UP: -1, DOWN: 1, PAGE_UP: -10, PAGE_DOWN: 10, HOME: -10**6, END: 10**6}
        if key in steps:
            self._comparison_offset = max(0, self._comparison_offset + steps[key])
            return True
        return False

    def _request_comparison(self, path: Path | None) -> None:
        self._comparison_mode = "report"
        if self._comparison_loading:
            return
        current = self.build_report()
        if not current.get("snapshots"):
            self._comparison_error = "Collect at least one sample before comparing a baseline."
            return
        existing = self._baseline_report
        self._comparison_error = None
        self._comparison_loading = True

        def work() -> None:
            try:
                baseline = load_report(path) if path is not None else existing
                if baseline is None:
                    raise ValueError("Load a baseline report first.")
                after = LoadedReport(Path("<current session>"), current, summarize(current))
                result = compare_reports(baseline, after, strict=True)
                self._comparison_results.put_nowait((baseline, result, None))
            except Exception as exc:  # noqa: BLE001 - isolate malformed files from the UI.
                self._comparison_results.put_nowait((None, None, str(exc)))

        worker = None
        try:
            worker = threading.Thread(target=work, name="DrainTracerBaseline", daemon=True)
            self._comparison_worker = worker
            worker.start()
        except Exception as exc:  # noqa: BLE001 - a failed worker must not disable the view.
            self._comparison_error = f"Comparison worker startup reported an error: {exc}"
            # A lost start acknowledgement can leave a live or completed worker.
            # Keep its ownership until the normal result drain permits a retry.
            if worker is None or (worker.ident is None and not worker.is_alive()
                                  and self._comparison_results.empty()):
                self._comparison_loading = False
                self._comparison_worker = None

    def _drain_comparison_result(self) -> None:
        if self._comparison_worker is not None and self._comparison_worker.is_alive():
            return
        try:
            baseline, comparison, error = self._comparison_results.get_nowait()
        except Empty:
            return
        self._comparison_loading = False
        self._comparison_error = error
        if error is None:
            self._baseline_report, self._comparison = baseline, comparison
            self._comparison_at = time.time()
            self._comparison_offset = 0

    def _comparison_controls(self) -> list[tuple[str, str, str]]:
        controls = [("P", "two PIDs", self.palette.accent), ("B", "JSON baseline", self.palette.teal)]
        if self._comparison_mode == "report" and self._baseline_report is not None:
            controls.append(("R", "refresh", self.palette.accent))
        controls.append((f"{self.glyphs.up}{self.glyphs.down}", "scroll", self.palette.muted))
        return controls

    def _comparison_help_keys(self) -> list[tuple[str, str]]:
        return [("P", "Enter two PIDs separated by a space to compare live processes"),
                ("B", "Enter a saved JSON path to compare with this monitored session"),
                ("R", "Compare the loaded baseline with a fresh capture of this session"),
                ("Up/Down", "Scroll the comparison (Home/End and Page Up/Down too)")]

    def _comparison_editor_footer(self) -> Text | None:
        if self._pid_draft is not None:
            text = f" PIDs {self._pid_draft}_  ENTER compare  ESC overview"
        elif self._baseline_draft is not None:
            room = max(1, self.console.width - 38)
            text = f" BASELINE {self._baseline_draft[-room:]}_  ENTER load  ESC overview"
        else:
            return None
        return Text(text, style=f"bold {self.palette.accent}", no_wrap=True, overflow="ellipsis")

    def _poll_live_pair(self) -> None:
        """Start at most one read while this page is visible; discard superseded selections."""
        if self._live_job is not None:
            requested, future = self._live_job
            if not future.done():
                return
            self._live_job = None
            if requested is self._live_pids:
                try:
                    self._live_pair = future.result()
                except Exception as exc:
                    self._live_error = f"Comparison unavailable: {safe_label(exc)}"
        pids, previous = self._live_pids, self._live_pair
        if (pids is None or self._pid_draft is not None or self._live_error
                or time.monotonic() < self._live_due or (previous and all(item.error for item in previous))):
            return
        self._live_due = time.monotonic() + 1.0
        future = Future()
        self._live_job = pids, future

        def read() -> None:
            try:
                left = sample_process(pids[0], previous[0] if previous else None)
                right = sample_process(pids[1], previous[1] if previous else None)
                future.set_result((left, right))
            except Exception as exc:
                future.set_exception(exc)

        worker = None
        try:
            worker = threading.Thread(target=read, name="DrainTracerLiveCompare", daemon=True)
            worker.start()
        except Exception as exc:
            if worker is None or worker.ident is None:
                future.set_exception(exc)

    def _live_comparison_body(self) -> list[RenderableType]:
        parts: list[RenderableType] = [Text("TWO LIVE PROCESSES", style=f"bold {self.palette.accent}")]
        if self._pid_draft is not None:
            parts.append(Text("Enter two PIDs separated by a space, then press Enter."))
        if self._live_error:
            parts.append(Text(self._live_error, style=self.palette.warn))
        if self._live_pair is None:
            if self._live_pids and self._pid_draft is None and not self._live_error:
                parts.append(Text("Reading processes...", style=self.palette.muted))
            return parts

        left, right = self._live_pair
        for label, item in zip(("A", "B"), (left, right)):
            line = Text.assemble((f"{label}  ", f"bold {self.palette.teal}"),
                                 f"PID {item.process.pid}  {safe_label(item.process.name)}")
            if item.error:
                line.append(f"  {safe_label(item.error)}", style=self.palette.warn)
            parts.append(line)
        table = Table(box=None, padding=(0, 1), header_style=f"bold {self.palette.accent}")
        table.add_column("Resource")
        for column in ("A", "B", "B - A"):
            table.add_column(column, justify="right")

        def metric(label, a, b, fmt):
            delta = "--" if a is None or b is None else ("+" if b >= a else "-") + fmt(abs(b - a))
            table.add_row(label, "--" if a is None else fmt(a), "--" if b is None else fmt(b), delta)

        metric("CPU (%)", left.cpu_percent, right.cpu_percent, lambda n: f"{n:.1f}")
        metric("Memory (RSS)", left.resident_bytes, right.resident_bytes, format_bytes)
        metric("Threads", left.threads, right.threads, str)
        metric("Handles" if os.name == "nt" else "Open FDs", left.handles, right.handles, str)
        parts.append(table)
        parts.append(Text("CPU is per core. -- means waiting or unavailable.", style=self.palette.muted))
        parts.append(Text("Target controls apply to the monitored tree.", style=self.palette.muted))
        return parts

    def _render_comparison_view(self) -> RenderableType:
        self._drain_comparison_result()
        if self._comparison_mode == "live":
            self._poll_live_pair()

        def body(width: int, height: int | None) -> RenderableType:
            if self._comparison_mode == "choose":
                return Group(
                    Text("CHOOSE A COMPARISON", style=f"bold {self.palette.accent}"),
                    Text.assemble(("[P] ", f"bold {self.palette.teal}"), "Two live processes"),
                    Text("    Compare resources for any two running PIDs.", style=self.palette.muted),
                    Text.assemble(("[B] ", f"bold {self.palette.teal}"), "Current session and saved JSON"),
                    Text("    Compare the monitored tree with a baseline report.", style=self.palette.muted),
                )
            if self._comparison_mode == "live":
                return self._scrolled(Group(*self._live_comparison_body()), width, height,
                                      "_comparison_offset", "end of comparison")
            parts: list[RenderableType] = [Text("BASELINE COMPARISON", style=f"bold {self.palette.accent}")]
            if self._comparison_loading:
                parts.append(Text("Loading/comparing baseline; sampling continues.", style=self.palette.muted))
            if self._comparison_error:
                parts.append(Text(f"Baseline unavailable: {self._comparison_error}", style=self.palette.danger))
                hint = ("Wait for the current comparison to finish before retrying."
                        if self._comparison_loading else
                        "Press B to choose another report; the previous comparison is retained.")
                parts.append(Text(hint))
            comparison = self._comparison
            if comparison is None:
                if not self._comparison_loading:
                    parts.append(Text("Press B and enter a saved JSON report path. Monitoring continues."))
            else:
                parts.append(Text(f"COMPARABILITY: {comparison.comparability.upper()} (same rules as diff --strict)",
                                  style=f"bold {self.palette.warn if comparison.blocked else self.palette.ok}"))
                for caveat in comparison.caveats:
                    parts.append(Text(caveat.message, style=self.palette.warn))
                captured = time.strftime("%H:%M:%S", time.localtime(self._comparison_at or time.time()))
                parts.append(Text(f"Captured {captured}; press R to refresh. A is baseline, B is this session.",
                                  style=self.palette.muted))
                parts.append(Text("Signal episodes A -> B: " + "; ".join(
                    f"{item.resource} {item.before.episodes} -> {item.after.episodes}"
                    for item in comparison.resources
                )))
                parts.append(render_comparison(comparison, self.glyphs, width))
            return self._scrolled(Group(*parts), width, height, "_comparison_offset", "end of comparison")

        return Sized(body)


__all__ = ["ComparisonViewMixin"]
