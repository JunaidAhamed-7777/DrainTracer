"""Saved-baseline comparison without changing monitoring or target ownership."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from queue import Empty, Queue

from rich.console import Group, RenderableType
from rich.text import Text

from .analysis import summarize
from .diff_command import render_comparison
from .keys import BACKSPACE, DOWN, END, ENTER, ESCAPE, HOME, PAGE_DOWN, PAGE_UP, UP
from .report_diff import LoadedReport, ReportComparison, compare_reports, load_report
from .widgets import Sized


class ComparisonViewMixin:
    """One bounded file worker; published results are applied only on the UI thread."""

    def _init_comparison_view(self) -> None:
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
        if self._baseline_draft is None:
            return False
        if key == ESCAPE:
            self._baseline_draft = None
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
        if key.lower() == "b":
            if not self._comparison_loading:
                self._baseline_draft = ""
            return True
        if key.lower() == "r":
            if self._baseline_report is not None and not self._comparison_loading:
                self._request_comparison(None)
            return True
        steps = {UP: -1, DOWN: 1, PAGE_UP: -10, PAGE_DOWN: 10, HOME: -10**6, END: 10**6}
        if key in steps:
            self._comparison_offset = max(0, self._comparison_offset + steps[key])
            return True
        return False

    def _request_comparison(self, path: Path | None) -> None:
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
        return [("B", "baseline", self.palette.accent), ("R", "refresh", self.palette.teal),
                (f"{self.glyphs.up}{self.glyphs.down}", "scroll", self.palette.muted)]

    def _comparison_help_keys(self) -> list[tuple[str, str]]:
        return [("B", "Load a JSON baseline path; Enter confirms, Esc cancels"),
                ("R", "Compare the loaded baseline with a fresh capture of this session"),
                ("Up/Down", "Scroll the comparison (Home/End and Page Up/Down too)")]

    def _render_comparison_view(self) -> RenderableType:
        self._drain_comparison_result()

        def body(width: int, height: int | None) -> RenderableType:
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
