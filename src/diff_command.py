"""The ``diff`` command: compare two reports in the terminal or as JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .analysis import severity_rank
from .report_diff import (
    IMPROVED,
    MIXED,
    REGRESSED,
    UNCHANGED,
    DiffError,
    LoadedReport,
    ReportComparison,
    compare_reports,
    format_amount,
    format_drift,
    load_report,
)
from .theme import PALETTE, Glyphs, select_glyphs
from .units import format_duration

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_ERROR = 2

_VERDICT_STYLE = {
    IMPROVED: ("IMPROVED", PALETTE.ok),
    REGRESSED: ("REGRESSED", PALETTE.danger),
    MIXED: ("MIXED", PALETTE.alert),
    UNCHANGED: ("UNCHANGED", PALETTE.accent),
}
_SIGNAL_STYLE = {
    "resolved": PALETTE.ok,
    "eased": PALETTE.ok,
    "new": PALETTE.danger,
    "worsened": PALETTE.danger,
    "persists": PALETTE.warn,
    "clear": PALETTE.muted,
}
_SEVERITY_STYLE = {
    "none": PALETTE.muted,
    "low": PALETTE.warn,
    "medium": PALETTE.alert,
    "high": PALETTE.danger,
    "critical": PALETTE.critical,
}


def build_diff_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{os.path.basename(sys.argv[0]) or 'draintracer'} diff",
        description=(
            "Compare two leak reports: did a change reduce drift or resolve a leak? "
            "Signals and post-warm-up drift are compared per resource."
        ),
    )
    parser.add_argument(
        "before", metavar="BEFORE", help="The earlier report (leak_report_<UUID>.json)."
    )
    parser.add_argument("after", metavar="AFTER", help="The later report to compare against it.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the comparison as JSON instead of a table.",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit 1 when the verdict is regressed or mixed, for CI checks.",
    )
    return parser


def run_diff(argv: list[str], console: Console) -> int:
    """Parse ``diff`` arguments, compare the reports, and return the exit code."""
    args = build_diff_parser().parse_args(argv)
    try:
        before = load_report(args.before)
        after = load_report(args.after)
    except DiffError as exc:
        console.print(Text.assemble(("Diff error: ", "bold red"), str(exc)))
        return EXIT_ERROR
    try:
        comparison = compare_reports(before, after)
    except Exception as exc:  # a report that parses but defeats the analysis
        console.print(Text.assemble(("Diff error: ", "bold red"), f"could not compare: {exc}"))
        return EXIT_ERROR
    if args.json:
        console.print(
            json.dumps(comparison.to_dict(), indent=2, allow_nan=False),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    else:
        console.print(render_comparison(comparison, select_glyphs(console), console.width))
    if args.fail_on_regression and comparison.verdict in {REGRESSED, MIXED}:
        return EXIT_REGRESSION
    return EXIT_OK


# Below this many columns the per-resource table no longer fits.
WIDE_COLUMNS = 110


def render_comparison(
    comparison: ReportComparison, glyphs: Glyphs, width: int = WIDE_COLUMNS
) -> RenderableType:
    """The comparison as a Rich panel: sources, per-resource results, verdict.

    Wide consoles get one table row per resource; narrower ones (CI logs are
    often 80 columns) get a stacked block per resource instead.
    """
    sep = glyphs.sep
    sources = Table.grid(padding=(0, 1))
    sources.add_column(style=f"bold {PALETTE.accent}", no_wrap=True)
    sources.add_column(overflow="fold")
    for label, report in (("A", comparison.before), ("B", comparison.after)):
        sources.add_row(
            label,
            Text.assemble(
                (_describe(report, sep), "bold"), "\n", (report.path.name, PALETTE.muted)
            ),
        )

    parts: list[RenderableType] = [sources, Text("")]
    if width >= WIDE_COLUMNS:
        parts.append(_resource_table(comparison, glyphs))
    else:
        parts.extend(_resource_blocks(comparison, glyphs))
    if comparison.handle_type_growth:
        entries = [
            f"{category} {_signed_count(before)} {glyphs.arrow} {_signed_count(after)}"
            for category, (before, after) in comparison.handle_type_growth.items()
        ]
        parts.append(Text(""))
        parts.append(
            Text.assemble(("handle types  ", f"bold {PALETTE.muted}"), f" {sep} ".join(entries))
        )
    for warning in comparison.warnings:
        parts.append(
            Text.assemble((f"{glyphs.warn} ", f"bold {PALETTE.warn}"), (warning, PALETTE.warn))
        )

    label, color = _VERDICT_STYLE[comparison.verdict]
    parts.append(Text(""))
    parts.append(
        Text.assemble(
            ("VERDICT ", f"bold {PALETTE.muted}"),
            (f" {label} ", f"bold {PALETTE.ink} on {color}"),
            f"  {comparison.headline()}",
        )
    )
    return Panel(
        Group(*parts),
        title=Text(f" {glyphs.brand} DRAINTRACER REPORT DIFF ", style=f"bold {PALETTE.accent}"),
        title_align="left",
        border_style=PALETTE.border,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _resource_table(comparison: ReportComparison, glyphs: Glyphs) -> Table:
    table = Table(
        box=box.SIMPLE_HEAD,
        expand=True,
        pad_edge=False,
        header_style=f"bold {PALETTE.muted}",
        border_style=PALETTE.border,
    )
    table.add_column("RESOURCE", no_wrap=True)
    table.add_column(f"SIGNAL A {glyphs.arrow} B", no_wrap=True)
    table.add_column("DRIFT A", justify="right", no_wrap=True)
    table.add_column("DRIFT B", justify="right", no_wrap=True)
    table.add_column("CHANGE", no_wrap=True)
    table.add_column(f"GROWTH A {glyphs.arrow} B", justify="right", no_wrap=True)
    for item in comparison.resources:
        table.add_row(
            Text(item.resource, style="bold"),
            _signal_text(item, glyphs),
            format_drift(item.resource, item.before.drift_per_second),
            format_drift(item.resource, item.after.drift_per_second),
            _change_text(item, glyphs),
            _growth_text(item, glyphs),
        )
    return table


def _resource_blocks(comparison: ReportComparison, glyphs: Glyphs) -> list[RenderableType]:
    blocks: list[RenderableType] = []
    for item in comparison.resources:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(min_width=8, no_wrap=True)
        grid.add_column(overflow="fold")
        grid.add_row(Text(item.resource.upper(), style="bold"), _signal_text(item, glyphs))
        drift = Text(
            f"drift  {format_drift(item.resource, item.before.drift_per_second)} {glyphs.arrow} "
            f"{format_drift(item.resource, item.after.drift_per_second)}  "
        )
        drift.append_text(_change_text(item, glyphs))
        grid.add_row("", drift)
        grid.add_row("", Text(f"growth {_growth_text(item, glyphs)}", style=PALETTE.muted))
        blocks.append(grid)
    return blocks


def _signal_text(item, glyphs: Glyphs) -> Text:
    def severity(name: str) -> tuple[str, str]:
        return (name.upper() if name != "none" else "clear", _SEVERITY_STYLE.get(name, PALETTE.muted))

    text = Text.assemble(severity(item.before.worst), f" {glyphs.arrow} ", severity(item.after.worst))
    if item.signal not in {"clear", "persists"} or severity_rank(item.after.worst):
        text.append(f"  {item.signal}", style=f"bold {_SIGNAL_STYLE[item.signal]}")
    return text


def _growth_text(item, glyphs: Glyphs) -> str:
    return (
        f"{format_amount(item.resource, item.before.growth, signed=True)} {glyphs.arrow} "
        f"{format_amount(item.resource, item.after.growth, signed=True)}"
    )


def _describe(report: LoadedReport, sep: str) -> str:
    parts = [report.name]
    version = report.data.get("version")
    if version:
        parts.append(str(version))
    parts.append(format_duration(report.summary.duration))
    created = report.data.get("created_at")
    if isinstance(created, str) and created:
        parts.append(created[:19].replace("T", " "))
    return f" {sep} ".join(parts)


def _change_text(item, glyphs: Glyphs) -> Text:
    magnitude = item.change_magnitude() or "changed"
    if item.trend == IMPROVED:
        return Text(f"{glyphs.down} {magnitude}", style=f"bold {PALETTE.ok}")
    if item.trend == REGRESSED:
        return Text(f"{glyphs.up} {magnitude}", style=f"bold {PALETTE.danger}")
    if item.trend == UNCHANGED:
        return Text("unchanged", style=PALETTE.muted)
    return Text("unknown", style=PALETTE.muted)


def _signed_count(value: int | None) -> str:
    if value is None:
        return "n/a"
    return f"{'-' if value < 0 else '+'}{abs(value):,}"



__all__ = [
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_REGRESSION",
    "build_diff_parser",
    "render_comparison",
    "run_diff",
]
