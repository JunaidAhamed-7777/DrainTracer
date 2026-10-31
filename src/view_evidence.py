"""View 3: signal evidence, or "why this warning?".

For one resource at a time, shows what the detector judged at the latest
sample: the metric, each window's points, span, baseline and current
values, delta, slope, the monotonic and drop ratios the checks used, the
heuristic score, and which check decided the outcome. All of it comes
from the ``WindowEvaluation`` records the detector emitted; the view
formats them and never re-runs a check. Severity is presented as the
heuristic tier it is, never as a probability that a leak exists.
"""

from __future__ import annotations

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .events import (
    METRIC_LABELS,
    RESOURCE_METRICS,
    EvidenceState,
    LeakAlert,
    LeakResource,
    ResourceEvidence,
    WindowEvaluation,
    WindowVerdict,
    severity_rank,
)
from .keys import BACKTAB, DOWN, END, HOME, LEFT, PAGE_DOWN, PAGE_UP, RIGHT, TAB, UP
from .units import format_bytes, format_duration
from .widgets import Sized

EVIDENCE_RESOURCES = (LeakResource.MEMORY, LeakResource.HANDLES, LeakResource.THREADS)
# The window table shows every column from this inner width.
FULL_TABLE_COLUMNS = 96
HEURISTIC_NOTE = (
    "Severity is a heuristic tier for how much and how fast the metric grew against "
    "the thresholds; it is not a probability that a leak exists."
)


class EvidenceViewMixin:
    """Render and drive the signal evidence view for ``DashboardApp``."""

    def _init_evidence_view(self) -> None:
        self._evidence_index = 0
        self._evidence_offset = 0
        self._evidence_pinned = False

    @property
    def evidence_resource(self) -> LeakResource:
        return EVIDENCE_RESOURCES[self._evidence_index]

    def _evidence_key(self, key: str) -> bool:
        if key in (LEFT, RIGHT, TAB, BACKTAB):
            step = -1 if key in (LEFT, BACKTAB) else 1
            self._evidence_index = (self._evidence_index + step) % len(EVIDENCE_RESOURCES)
            self._evidence_offset = 0
            self._evidence_pinned = True
            return True
        steps = {UP: -1, DOWN: 1, PAGE_UP: -10, PAGE_DOWN: 10, HOME: -10**6, END: 10**6}
        if key in steps:
            self._evidence_offset = max(0, self._evidence_offset + steps[key])
            return True
        return False

    def _show_evidence(self) -> None:
        """Open the view on the strongest active signal unless the user picked one."""
        snapshot = self.snapshot
        if self._evidence_pinned or snapshot is None or not snapshot.alerts:
            return
        strongest = max(snapshot.alerts, key=lambda alert: severity_rank(alert.severity))
        self._evidence_index = EVIDENCE_RESOURCES.index(strongest.resource)

    def _evidence_controls(self) -> list[tuple[str, str, str]]:
        palette, glyphs = self.palette, self.glyphs
        arrows = "<>" if glyphs.arrow == "->" else "←→"
        return [
            (arrows, "resource", palette.accent),
            (f"{glyphs.up}{glyphs.down}", "scroll", palette.muted),
        ]

    def _evidence_help_keys(self) -> list[tuple[str, str]]:
        return [
            ("Left/Right", "Switch resource: memory, handles, threads (Tab too)"),
            ("Up/Down", "Scroll the evidence"),
        ]

    def _render_evidence_view(self) -> Panel:
        palette = self.palette
        resource = self.evidence_resource
        content = self._evidence_content(resource)
        return Panel(
            Sized(lambda width, height: self._scrolled(
                content(width), width, height, "_evidence_offset", "end of evidence"
            )),
            title=Text(" SIGNAL EVIDENCE  why this warning? ", style=f"bold {palette.accent}"),
            title_align="left",
            subtitle=Text(" severity is a heuristic, not a probability ", style=palette.muted),
            subtitle_align="right",
            border_style=palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _evidence_content(self, resource: LeakResource):
        """A factory for the content at a width: the window table drops columns when narrow."""

        def build(width: int) -> RenderableType:
            snapshot = self.snapshot
            evidence = snapshot.evidence_for(resource) if snapshot else None
            alert = next((a for a in snapshot.alerts if a.resource == resource), None) \
                if snapshot else None
            parts: list[RenderableType] = [self._evidence_tabs(), Text("")]
            parts.append(self._evidence_facts(resource, evidence, alert))
            windows = evidence.windows if evidence is not None else ()
            if windows:
                parts += [Text(""), self._window_table(resource, windows, alert, width)]
                parts.append(self._evidence_reason(resource, evidence, alert))
            else:
                parts += [Text(""), Text(self._no_windows_text(evidence),
                                         style=self.palette.muted)]
            if alert is not None:
                parts += [Text(""), self._alert_detail(alert)]
            parts += [Text(""), self._criteria_text(resource), Text(HEURISTIC_NOTE,
                                                                    style=self.palette.muted)]
            return Group(*parts)

        return build

    def _evidence_tabs(self) -> Text:
        palette, sep = self.palette, self.glyphs.sep
        snapshot = self.snapshot
        tabs = Text(no_wrap=True, overflow="ellipsis")
        for index, resource in enumerate(EVIDENCE_RESOURCES):
            evidence = snapshot.evidence_for(resource) if snapshot else None
            label = f" {resource.value.upper()} {self._state_word(evidence)} "
            if index == self._evidence_index:
                tabs.append(label, style=f"bold {palette.ink} on {palette.accent}")
            else:
                tabs.append(label, style=palette.muted)
            if index < len(EVIDENCE_RESOURCES) - 1:
                tabs.append(f" {sep} ", style=palette.border)
        return tabs

    @staticmethod
    def _state_word(evidence: ResourceEvidence | None) -> str:
        if evidence is None:
            return "-"
        if evidence.state == EvidenceState.GROWTH:
            return evidence.severity.value.upper()
        return {
            EvidenceState.WARMING_UP: "WAIT",
            EvidenceState.COLLECTING: "COLLECT",
            EvidenceState.NO_GROWTH: "STEADY",
            EvidenceState.UNAVAILABLE: "N/A",
            EvidenceState.STALE: "PAUSED",
        }.get(evidence.state, "-")

    def _evidence_facts(
        self, resource: LeakResource, evidence: ResourceEvidence | None, alert: LeakAlert | None
    ) -> Table:
        palette, sep = self.palette, f" {self.glyphs.sep} "
        metric = RESOURCE_METRICS[resource]
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(style=f"bold {palette.muted}", no_wrap=True, min_width=10)
        grid.add_column(ratio=1, overflow="fold")
        grid.add_row("Metric", f"{METRIC_LABELS[metric]}, summed over the processes measured")
        if evidence is None:
            grid.add_row("State", Text("no evidence yet", style=palette.muted))
            return grid
        if evidence.state == EvidenceState.GROWTH:
            window = format_duration(evidence.evaluated_window_seconds or 0.0)
            state = Text(
                f"{evidence.severity.value.upper()} signal (heuristic) from the {window} window",
                style=f"bold {self._severity_color(evidence.severity)}",
            )
        else:
            label, color, detail = self._evidence_row(evidence)
            state = Text.assemble((label, f"bold {color}"), f"  {detail}")
        grid.add_row("State", state)
        samples = (
            f"{evidence.valid_samples:,} valid samples in this segment "
            f"(a window needs {evidence.needed_samples}){sep}"
            f"{format_duration(evidence.elapsed_seconds)} covered "
            f"(the shortest window needs {format_duration(evidence.needed_seconds)})"
        )
        grid.add_row("Samples", samples)
        coverage = evidence.coverage
        if coverage is not None:
            measured = Text(f"{coverage.measured}/{coverage.total} live processes readable")
            if coverage.partial:
                measured.append(
                    f"{sep}totals leave out PID "
                    + ", ".join(str(pid) for pid in coverage.unmeasured_pids[:4]),
                    style=f"bold {palette.warn}",
                )
            grid.add_row("Coverage", measured)
        if self.snapshot is not None:
            current_coverage = (self.snapshot.coverage or {}).get(metric, coverage)
            current = _quantity(resource, self.snapshot.totals.get(metric, 0.0))
            if current_coverage is not None:
                if current_coverage.measured == 0:
                    current = "n/a"
                elif current_coverage.partial:
                    current += " (partial lower bound)"
            grid.add_row("Current", current)
        if evidence.state == EvidenceState.STALE:
            grid.add_row("Note", Text("paused: these are the last evaluations before the pause",
                                      style=f"bold {palette.warn}"))
        return grid

    def _window_table(
        self,
        resource: LeakResource,
        windows: tuple[WindowEvaluation, ...],
        alert: LeakAlert | None,
        width: int,
    ) -> Table:
        palette = self.palette
        full = width >= FULL_TABLE_COLUMNS
        table = Table(
            box=box.SIMPLE_HEAD,
            expand=True,
            show_edge=False,
            pad_edge=False,
            padding=(0, 1),
            header_style=f"bold {palette.muted}",
            border_style=palette.border,
        )
        columns = ["WINDOW", "PTS", "SPAN"]
        if full:
            columns += ["BASELINE", "CURRENT"]
        columns += ["DELTA", "SLOPE", "MONO", "DROPS"]
        if full:
            columns.append("SCORE")
        for name in columns:
            table.add_column(name, justify="right", no_wrap=True)
        table.add_column("RESULT", ratio=1, overflow="fold")
        for window in windows:
            chosen = alert is not None and window.window_seconds == alert.window_seconds
            cells = [
                format_duration(window.window_seconds),
                str(window.points),
                format_duration(window.span_seconds),
            ]
            if full:
                cells += [_maybe(resource, window.baseline_value), _maybe(resource, window.current_value)]
            cells += [
                _maybe(resource, window.delta, signed=True),
                _maybe(resource, window.slope_per_second, signed=True, rate=True),
                _ratio(window.monotonic_ratio),
                _ratio(window.drop_ratio),
            ]
            if full:
                cells.append("-" if window.score is None else f"{window.score:.2f}")
            result = Text(self._verdict_text(window), style=self._verdict_style(window))
            if chosen:
                result.append(" (reported)", style=f"bold {palette.accent}")
            table.add_row(*cells, result)
        return table

    def _verdict_text(self, window: WindowEvaluation) -> str:
        criteria = self.detector.criteria
        verdict = window.verdict
        if verdict == WindowVerdict.TOO_FEW_SAMPLES:
            return f"filling: {window.points}/{criteria.min_points} points"
        if verdict == WindowVerdict.SHORT_SPAN:
            needed = window.window_seconds * criteria.min_span_ratio
            return f"filling: {format_duration(window.span_seconds)}/{format_duration(needed)}"
        if verdict == WindowVerdict.NO_UPWARD_TREND:
            return "no upward trend"
        if verdict == WindowVerdict.NOT_MONOTONIC:
            return f"too many falling steps (needs {criteria.min_monotonic_ratio:.0%} not falling)"
        if verdict == WindowVerdict.TOO_MANY_DROPS:
            return f"too many drops below peak (max {criteria.max_drop_ratio:.0%})"
        if verdict == WindowVerdict.BELOW_THRESHOLDS:
            low = criteria.severity_scores[-1][1]
            return f"grew, but score below {low:g} for LOW"
        return f"qualified: {window.severity.value.upper()}"

    def _verdict_style(self, window: WindowEvaluation) -> str:
        palette = self.palette
        if window.qualified:
            return f"bold {self._severity_color(window.severity)}"
        if not window.judged:
            return palette.muted
        return palette.ok

    def _evidence_reason(
        self, resource: LeakResource, evidence: ResourceEvidence, alert: LeakAlert | None
    ) -> Text:
        """One sentence on the decisive window, built from the emitted values only."""
        criteria = self.detector.criteria
        windows = evidence.windows
        if alert is not None:
            window = next((w for w in windows if w.window_seconds == alert.window_seconds), None)
        else:
            judged = [w for w in windows if w.judged]
            window = judged[-1] if judged else None
        if window is None:
            return Text("No window holds enough samples yet, so nothing was judged.",
                        style=self.palette.muted)
        name = format_duration(window.window_seconds)
        checks = (
            f"slope {_maybe(resource, window.slope_per_second, signed=True, rate=True)} "
            f"(needs > 0), {_ratio(window.monotonic_ratio)} of steps not falling "
            f"(needs {criteria.min_monotonic_ratio:.0%}), {_ratio(window.drop_ratio)} below the "
            f"running peak (max {criteria.max_drop_ratio:.0%})"
        )
        if window.qualified:
            text = (
                f"Why: the {name} window qualified with {checks}; heuristic score "
                f"{window.score:.2f} gives {window.severity.value.upper()}."
            )
            if len([w for w in windows if w.qualified]) > 1:
                text += " The strongest qualifying window is reported."
            return Text(text)
        text = f"Why not: the longest judged window ({name}) did not qualify: " \
               f"{self._verdict_text(window)}; {checks}"
        if window.score is not None:
            text += f"; score {window.score:.2f}"
        return Text(text + ".")

    def _no_windows_text(self, evidence: ResourceEvidence | None) -> str:
        if evidence is None:
            return "No evidence yet: waiting for samples."
        if evidence.state == EvidenceState.WARMING_UP:
            return "Warming up: windows are not evaluated until the warm-up ends."
        if evidence.state == EvidenceState.UNAVAILABLE:
            if evidence.unavailable_reason == "no live process":
                return "Unavailable: the monitored tree has no live process, so no window was evaluated."
            return "Unavailable: no live process could be read, so no window was evaluated."
        return "No window evaluations for this sample."

    def _alert_detail(self, alert: LeakAlert) -> Group:
        palette, sep = self.palette, f" {self.glyphs.sep} "
        lines: list[RenderableType] = [Text(alert.message, style=self._severity_color(alert.severity))]
        if alert.contributors:
            top = sep.join(
                f"{item.name} PID {item.pid} {_quantity(alert.resource, item.delta, signed=True)}"
                f" ({item.share:.0%}{', new' if item.new_process else ''}"
                f"{', exited' if item.exited else ''})"
                for item in alert.contributors[:3]
            )
            lines.append(Text(f"Contributors (share of positive growth): {top}"))
            lines.append(Text(
                f"Positive growth: new processes {_quantity(alert.resource, alert.growth_from_new_processes)}"
                f"{sep}existing processes {_quantity(alert.resource, alert.growth_within_existing)}",
                style=palette.muted,
            ))
            lines.append(Text("Exits and decreases can offset these positive contributions in the window change.",
                              style=palette.muted))
        return Group(*lines)

    def _criteria_text(self, resource: LeakResource) -> Text:
        criteria = self.detector.criteria
        limits = self.detector.thresholds[resource]
        tiers = ", ".join(
            f"{severity.value.upper()} {score:g}" for severity, score in reversed(criteria.severity_scores)
        )
        return Text(
            f"Rules: a window needs {criteria.min_points}+ points over "
            f"{criteria.min_span_ratio:.0%} of its length, an upward slope, "
            f"{criteria.min_monotonic_ratio:.0%}+ of steps not falling and at most "
            f"{criteria.max_drop_ratio:.0%} of samples below the running peak. Score = "
            f"growth / {_quantity(resource, limits.critical_delta)} (max 3) + rate / "
            f"{_quantity(resource, limits.critical_slope)}/s (max 3) + the monotonic ratio, "
            f"+0.25 for 30s+ and +0.5 for 60s+ windows. Tiers: {tiers}.",
            style=self.palette.muted,
        )


def _quantity(resource: LeakResource, value: float, signed: bool = False) -> str:
    if resource == LeakResource.MEMORY:
        text = format_bytes(abs(value))
        return (("-" if value < 0 else "+") + text) if signed else text
    return f"{value:+,.0f}" if signed else f"{value:,.0f}"


def _maybe(
    resource: LeakResource, value: float | None, signed: bool = False, rate: bool = False
) -> str:
    if value is None:
        return "-"
    if rate and resource != LeakResource.MEMORY:
        return f"{value:+,.2f}/s"
    text = _quantity(resource, value, signed=signed)
    return f"{text}/s" if rate else text


def _ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


__all__ = ["EVIDENCE_RESOURCES", "EvidenceViewMixin"]
