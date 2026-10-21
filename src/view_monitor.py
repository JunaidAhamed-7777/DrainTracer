"""View 5: monitor diagnostics, or "can I trust this recording?".

Everything shown here was measured by the detector (``MonitorDiagnostics``
on each snapshot) or counted by the session (``SessionFacts``); the view
only words it. Host CPU and memory pressure are not sampled yet, and the
view says so instead of leaving the question unanswered.
"""

from __future__ import annotations

import time

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .events import COVERAGE_METRICS, METRIC_LABELS, EvidenceState
from .units import format_duration
from .widgets import Sized


class MonitorViewMixin:
    """Render the monitor diagnostics view for ``DashboardApp``.

    Relies on the dashboard's palette, glyphs, snapshot, detector and
    session accessors; it keeps no state of its own.
    """

    # Wider terminals show the sections in two columns.
    MONITOR_TWO_COLUMNS = 100

    def _monitor_controls(self) -> list[tuple[str, str, str]]:
        return []

    def _render_monitor_view(self) -> Panel:
        palette = self.palette
        return Panel(
            Sized(self._monitor_body),
            title=Text(" MONITOR DIAGNOSTICS ", style=f"bold {palette.cyan}"),
            title_align="left",
            subtitle=Text(" can this recording be trusted? ", style=palette.muted),
            subtitle_align="right",
            border_style=palette.border,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _monitor_body(self, width: int, height: int | None) -> RenderableType:
        sections = [
            ("SAMPLING", self._monitor_sampling_rows()),
            ("COVERAGE", self._monitor_coverage_rows()),
            ("FRESHNESS", self._monitor_freshness_rows()),
            ("MONITOR PROCESS", self._monitor_self_rows()),
            ("HOST", [("Context", Text(
                "not collected: host CPU and available memory are not sampled yet",
                style=self.palette.muted,
            ))]),
        ]
        blocks = [self._monitor_section(title, rows) for title, rows in sections]
        if width < self.MONITOR_TWO_COLUMNS:
            return Group(*blocks)
        columns = Table.grid(expand=True, padding=(0, 2))
        columns.add_column(ratio=1)
        columns.add_column(ratio=1)
        columns.add_row(Group(*blocks[:2]), Group(*blocks[2:]))
        return columns

    def _monitor_section(self, title: str, rows: list[tuple[str, RenderableType]]) -> Group:
        palette = self.palette
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(style=f"bold {palette.muted}", no_wrap=True, min_width=11)
        grid.add_column(ratio=1, overflow="fold")
        for label, value in rows:
            grid.add_row(label, value if isinstance(value, Text) else Text(str(value)))
        return Group(Text(title, style=f"bold {palette.accent}"), grid)

    def _monitor_sampling_rows(self) -> list[tuple[str, RenderableType]]:
        sep = f" {self.glyphs.sep} "
        diagnostics = self.diagnostics
        monitor = self.facts.to_dict()["monitor"]
        samples = self.facts.to_dict()["samples"]
        requested = (
            diagnostics.requested_interval_ms
            if diagnostics is not None
            else self.detector.sample_interval_ms
        )
        cadence = f"requested {requested} ms"
        if diagnostics is not None and diagnostics.actual_interval_ms is not None:
            lag = diagnostics.cadence_lag_ms or 0.0
            cadence += f"{sep}actual {diagnostics.actual_interval_ms:.0f} ms ({lag:+.0f} ms)"
        else:
            cadence += f"{sep}actual not measured yet"
        if monitor["interval_ms"]["mean"] is not None:
            cadence += f"{sep}session mean {monitor['interval_ms']['mean']:.0f} ms"
        collection = []
        if diagnostics is not None and diagnostics.collection_ms is not None:
            collection.append(f"this sample {diagnostics.collection_ms:.0f} ms")
        if monitor["collection_ms"]["mean"] is not None:
            collection.append(
                f"mean {monitor['collection_ms']['mean']:.0f} ms, "
                f"max {monitor['collection_ms']['max']:.0f} ms"
            )
        counts = (
            f"{samples['taken']:,} taken{sep}{samples['collected']:,} collected{sep}"
            f"{samples['paused']:,} paused{sep}{samples['failed']:,} failed"
        )
        dropped = monitor["dropped_snapshots"]
        dropped_text = Text(
            f"{dropped:,} snapshots dropped before the dashboard read them"
            if dropped
            else "none",
            style=f"bold {self.palette.warn}" if dropped else "",
        )
        return [
            ("Cadence", cadence),
            ("Collection", sep.join(collection) or "not measured yet"),
            ("Samples", counts),
            ("Dropped", dropped_text),
        ]

    def _monitor_coverage_rows(self) -> list[tuple[str, RenderableType]]:
        palette, sep = self.palette, f" {self.glyphs.sep} "
        snapshot = self.snapshot
        coverage = snapshot.coverage if snapshot is not None else None
        if coverage:
            readable = sep.join(
                f"{METRIC_LABELS.get(metric, metric)} {coverage[metric].measured}/"
                f"{coverage[metric].total}"
                for metric in COVERAGE_METRICS
                if metric in coverage
            )
        else:
            readable = "not measured yet"
        unreadable = self._monitor_unreadable()
        session = self.coverage_text()
        return [
            ("Readable", readable),
            ("Unreadable", Text(unreadable or "none",
                                style=f"bold {palette.warn}" if unreadable else "")),
            ("Session", Text(session or "every live process was readable on every sample",
                             style=f"bold {palette.warn}" if session else palette.muted)),
        ]

    def _monitor_unreadable(self, limit: int = 4) -> str:
        """``PID 200 access denied, PID 300 handles`` for processes left out now."""
        snapshot = self.snapshot
        if snapshot is None:
            return ""
        pids = snapshot.unmeasured_pids
        by_pid = {process.pid: process for process in snapshot.processes}
        parts = []
        for pid in pids[:limit]:
            process = by_pid.get(pid)
            if process is None or process.access_denied:
                parts.append(f"PID {pid} access denied")
            else:
                missing = [m for m in COVERAGE_METRICS if not process.measured(m)]
                names = ", ".join(METRIC_LABELS.get(m, m) for m in missing)
                parts.append(f"PID {pid} {process.name} ({names} unreadable)")
        if len(pids) > limit:
            parts.append(f"+{len(pids) - limit} more")
        return "; ".join(parts)

    def _monitor_freshness_rows(self) -> list[tuple[str, RenderableType]]:
        palette, sep = self.palette, f" {self.glyphs.sep} "
        snapshot = self.snapshot
        rows: list[tuple[str, RenderableType]] = []
        if snapshot is None:
            rows.append(("Last sample", Text("none yet", style=palette.muted)))
        else:
            age = max(0.0, time.time() - snapshot.timestamp)
            interval = self.detector.sample_interval_ms / 1000
            stale = not snapshot.monitoring_paused and age > 3 * interval
            text = f"{_age(age)} ago"
            if snapshot.monitoring_paused:
                text += f"{sep}paused"
            elif stale:
                text += f"{sep}stale (older than 3 intervals)"
            rows.append(("Last sample", Text(text, style=f"bold {palette.warn}" if stale else "")))
            rows.append(("Evidence", self._monitor_evidence_freshness(age)))
        diagnostics = self.diagnostics
        if not self.detector.classifier.available:
            types = Text("handle types unavailable on this system", style=palette.muted)
        elif diagnostics is None or diagnostics.handle_types_age_seconds is None:
            types = Text("not classified yet", style=palette.muted)
        else:
            types = Text(f"classified {diagnostics.handle_types_age_seconds:.1f}s ago")
            if diagnostics.handle_types_cost_ms is not None:
                types.append(f"{sep}pass {diagnostics.handle_types_cost_ms:.0f} ms")
        rows.append(("Handle types", types))
        chart = f"dashboard keeps the last {self.HISTORY_LIMIT} samples"
        thinning = self.chart_thinning_text()
        if thinning:
            chart += f"{sep}{thinning}"
        rows.append(("Charts", chart))
        return rows

    def _monitor_evidence_freshness(self, age: float) -> Text:
        """When leak evidence was last evaluated, from the detector's states."""
        palette = self.palette
        states = {item.state for item in self.snapshot.evidence} if self.snapshot else set()
        if not states:
            return Text("no evidence yet", style=palette.muted)
        if EvidenceState.STALE in states:
            return Text("on hold while paused", style=f"bold {palette.warn}")
        if states == {EvidenceState.WARMING_UP}:
            return Text("waiting for the warm-up to finish", style=palette.muted)
        if states == {EvidenceState.UNAVAILABLE}:
            return Text("nothing readable to evaluate", style=f"bold {palette.danger}")
        return Text(f"evaluated with the last sample ({_age(age)} ago)")

    def _monitor_self_rows(self) -> list[tuple[str, RenderableType]]:
        sep = f" {self.glyphs.sep} "
        diagnostics = self.diagnostics
        monitor = self.facts.to_dict()["monitor"]
        cpu = []
        if diagnostics is not None and diagnostics.monitor_cpu_percent is not None:
            cpu.append(f"{diagnostics.monitor_cpu_percent:.1f}% of one core now")
        if monitor["monitor_cpu_percent"]["mean"] is not None:
            cpu.append(f"mean {monitor['monitor_cpu_percent']['mean']:.1f}%")
        memory = []
        if diagnostics is not None and diagnostics.monitor_private_bytes is not None:
            memory.append(f"private {self._format_bytes(diagnostics.monitor_private_bytes)}")
        if diagnostics is not None and diagnostics.monitor_rss_bytes is not None:
            memory.append(f"RSS {self._format_bytes(diagnostics.monitor_rss_bytes)}")
        if monitor["monitor_private_peak_bytes"] is not None:
            memory.append(f"peak private {self._format_bytes(monitor['monitor_private_peak_bytes'])}")
        return [
            ("CPU", sep.join(cpu) or "not measured yet"),
            ("Memory", sep.join(memory) or "not measured yet"),
        ]


def _age(seconds: float) -> str:
    """``0.4s`` for fresh samples, ``2m5s`` once they are old."""
    return format_duration(seconds) if seconds >= 60 else f"{seconds:.1f}s"


__all__ = ["MonitorViewMixin"]
