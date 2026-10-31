"""The ``diff`` command: compare two reports in the terminal or as JSON."""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .analysis import severity_rank
from .budgets import BUDGET_OPTIONS, parse_budget
from .events import METRIC_LABELS
from .repeat_comparison import (
    MAX_GROUP_BYTES,
    MAX_GROUP_RUNS,
    RULE,
    RepeatedComparison,
    compare_repeated_reports,
)
from .report_diff import (
    COMPARABLE,
    IMPROVED,
    INSUFFICIENT_COVERAGE,
    MIXED,
    NOT_COMPARABLE,
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
from .theme import ASCII_GLYPHS, PALETTE, Glyphs, plain_console, select_glyphs
from .units import format_duration
from .widgets import LegacySafe

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_ERROR = 2
# Only with --strict: the runs were not equivalent or not fully measured.
EXIT_NOT_COMPARABLE = 3
# Only with a budget flag: the later run's peak exceeded a budget.
EXIT_OVER_BUDGET = 4

_VERDICT_STYLE = {
    IMPROVED: ("IMPROVED", PALETTE.ok),
    REGRESSED: ("REGRESSED", PALETTE.danger),
    MIXED: ("MIXED", PALETTE.alert),
    UNCHANGED: ("UNCHANGED", PALETTE.accent),
    NOT_COMPARABLE: ("NOT COMPARABLE", PALETTE.danger),
    INSUFFICIENT_COVERAGE: ("INSUFFICIENT COVERAGE", PALETTE.warn),
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
        prog="draintracer diff",
        description=(
            "Compare two leak reports: did a change reduce drift or resolve a leak? "
            "Signals and post-warm-up drift are compared per resource."
        ),
    )
    parser.add_argument(
        "before", metavar="BEFORE", help="The earlier report (leak_report_<UUID>.json)."
    )
    parser.add_argument("after", metavar="AFTER", help="The later report to compare against it.")
    parser.add_argument("--no-color", action="store_true", help="Disable terminal colors, including forced color.")
    parser.add_argument("--plain", action="store_true", help="Print unstyled text without panel or table borders.")
    parser.add_argument("--quiet", action="store_true", help="Print only the verdict, caveats, and budget checks.")

    parser.add_argument("--before-run", action="append", default=[], metavar="REPORT",
                        help="Add an earlier run; grouped comparisons require 2-20 runs on each side.")
    parser.add_argument("--after-run", action="append", default=[], metavar="REPORT",
                        help="Add a later run; grouped comparisons always enforce strict comparability.")
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
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Refuse a verdict for runs that are not equivalent experiments: report "
            "'not comparable' (different targets, workloads, arguments, settings, "
            "operation counts, or lengths over 2x apart) or 'insufficient coverage' "
            "(an unjudged run or partial coverage), and exit 3."
        ),
    )
    for metric, option in BUDGET_OPTIONS.items():
        what = "a size such as 500MB" if metric == "private_bytes" else "a positive whole count"
        parser.add_argument(
            f"--{option.replace('_', '-')}",
            type=_budget(metric),
            metavar="SIZE" if metric == "private_bytes" else "N",
            dest=option,
            help=(
                f"Exit 4 when AFTER's peak {METRIC_LABELS[metric]} exceed {what}, "
                "with or without a leak signal."
            ),
        )
    return parser


def _budget(metric: str):
    def parse(value: str) -> float:
        try:
            return parse_budget(metric, value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return parse


def run_diff(argv: list[str], console: Console) -> int:
    """Parse ``diff`` arguments, compare the reports, and return the exit code."""
    args = build_diff_parser().parse_args(argv)
    if args.no_color:
        console.no_color = True
    if args.plain:
        console = plain_console(console)

    if args.before_run or args.after_run:
        return _run_repeated_diff(args, console)
    try:
        before = load_report(args.before)
        after = load_report(args.after)
    except DiffError as exc:
        console.print(Text.assemble(("Diff error: ", "bold red"), str(exc)))
        return EXIT_ERROR
    try:
        budgets = {
            metric: getattr(args, option)
            for metric, option in BUDGET_OPTIONS.items()
            if getattr(args, option) is not None
        }
        comparison = compare_reports(before, after, strict=args.strict, budgets=budgets)
    except Exception as exc:  # a report that parses but defeats the analysis
        console.print(Text.assemble(("Diff error: ", "bold red"), f"could not compare: {exc}"))
        return EXIT_ERROR
    if args.json:
        console.print(
            json.dumps(comparison.to_dict(), indent=2, allow_nan=False),
            markup=False,
            highlight=False,
            emoji=False,
            soft_wrap=True,
        )
    elif args.plain or args.quiet:
        rendered = _plain_comparison(comparison, quiet=args.quiet)
        console.print(LegacySafe(rendered, console.encoding) if LegacySafe.needed(console.encoding) else rendered)
    else:
        rendered = render_comparison(comparison, select_glyphs(console), console.width)
        console.print(LegacySafe(rendered, console.encoding) if LegacySafe.needed(console.encoding) else rendered)
    if comparison.blocked:
        return EXIT_NOT_COMPARABLE
    if comparison.over_budget:
        return EXIT_OVER_BUDGET
    if args.fail_on_regression and comparison.verdict in {REGRESSED, MIXED}:
        return EXIT_REGRESSION
    return EXIT_OK


def _plain_comparison(comparison: ReportComparison, *, quiet: bool = False) -> Text:
    """Keep evidence and qualification readable without styling or box glyphs."""
    def amount(resource, value):
        return "n/a" if value is None else format_amount(resource, value)

    def drift(resource, value):
        return "n/a" if value is None else format_drift(resource, value)

    lines = [f"VERDICT {comparison.verdict_label.upper()}", comparison.headline()]
    lines.extend(f"Caveat: {warning}" for warning in comparison.warnings)
    if not quiet:
        lines += [f"BEFORE {comparison.before.path}", f"AFTER {comparison.after.path}"]
        for item in comparison.resources:
            lines.append(
                f"{item.resource}: signal {item.before.worst.upper()} to {item.after.worst.upper()} "
                f"({item.signal}); growth {amount(item.resource, item.before.growth)} "
                f"to {amount(item.resource, item.after.growth)}; "
                f"drift {drift(item.resource, item.before.drift_per_second)} "
                f"to {drift(item.resource, item.after.drift_per_second)}; "
                f"peak {amount(item.resource, item.peak_before)} "
                f"to {amount(item.resource, item.peak_after)}"
            )
            if item.normalized:
                lines.append(f"{item.resource} growth per operation: {_per_operation_text(item, ASCII_GLYPHS)}")
        for kind, (before, after) in comparison.handle_type_growth.items():
            lines.append(f"Handle type {kind}: {amount('handles', before)} to {amount('handles', after)}")
        lines.extend(_process_attribution_lines(comparison))
    lines.extend(f"Budget: {check.describe()}" for check in comparison.budgets)
    return Text("\n".join(lines))

def _run_repeated_diff(args, console: Console) -> int:
    try:
        if not 2 <= 1 + len(args.before_run) <= MAX_GROUP_RUNS or not 2 <= 1 + len(args.after_run) <= MAX_GROUP_RUNS:
            raise DiffError("Supply 2-20 reports on each side using --before-run and --after-run.")
        paths = [args.before, *args.before_run, args.after, *args.after_run]
        file_info = [Path(path).stat() for path in paths]
        if any(not stat.S_ISREG(info.st_mode) for info in file_info):
            raise DiffError("Repeated inputs must be ordinary report files.")
        if sum(info.st_size for info in file_info) > MAX_GROUP_BYTES:
            raise DiffError("Repeated inputs exceed the combined 128 MiB JSON limit; use smaller reports or fewer runs.")
        before, after = [], []
        decoded_bytes = 0
        for group, names in ((before, [args.before, *args.before_run]), (after, [args.after, *args.after_run])):
            for path in names:
                report = load_report(path)
                decoded_bytes += len(json.dumps(report.data, separators=(",", ":"), allow_nan=False).encode("utf-8"))
                if decoded_bytes > MAX_GROUP_BYTES:
                    raise DiffError("Repeated decoded inputs exceed the combined 128 MiB JSON limit.")
                group.append(report)
        budgets = {metric: getattr(args, option) for metric, option in BUDGET_OPTIONS.items()
                   if getattr(args, option) is not None}
        comparison = compare_repeated_reports(before, after, budgets=budgets)
    except (DiffError, OSError, ValueError, TypeError) as exc:
        console.print(Text(f"Diff error: {exc}", style="bold red"))
        return EXIT_ERROR
    if args.json:
        console.print(json.dumps(comparison.to_dict(), indent=2, allow_nan=False), markup=False,
                      highlight=False, emoji=False, soft_wrap=True)
    else:
        console.print(render_repeated_comparison(comparison, plain=args.plain, quiet=args.quiet))
    if comparison.blocked:
        return EXIT_NOT_COMPARABLE
    if comparison.over_budget:
        return EXIT_OVER_BUDGET
    return EXIT_REGRESSION if args.fail_on_regression and comparison.verdict in {REGRESSED, MIXED} else EXIT_OK


def render_repeated_comparison(comparison: RepeatedComparison, *, plain: bool = False,
                               quiet: bool = False) -> RenderableType:
    parts: list[RenderableType] = [] if quiet else [
        Text("REPEATED RUN COMPARISON", style=f"bold {PALETTE.accent}"),
        Text(f"A: {len(comparison.before)} runs; B: {len(comparison.after)} runs"),
    ]
    parts.append(Text(f"COMPARABILITY: {comparison.comparability.upper()}", style="bold"))
    parts += [Text(f"{item['before']} / {item['after']}: {item['message']}", style=PALETTE.warn)
              for item in comparison.caveats]
    if not quiet:
        parts.append(Text(RULE, style=PALETTE.muted))
    for item in (() if quiet else comparison.resources):
        def spread(resource, values, drift: bool = False) -> str:
            show = format_drift if drift else format_amount
            return (f"median {show(resource, values.median)} "
                    f"[{show(resource, values.minimum)} .. {show(resource, values.maximum)}]")
        parts += [Text(f"{item.resource.upper()}: {item.verdict}", style="bold"),
                  Text(f"drift A {spread(item.resource, item.drift_before, True)}; B {spread(item.resource, item.drift_after, True)}"),
                  Text(f"peaks A {spread(item.resource, item.peaks_before)}; B {spread(item.resource, item.peaks_after)}")]
    parts += [Text(check.describe(), style=PALETTE.warn if check.exceeded else PALETTE.muted)
              for check in comparison.budgets]
    parts.append(Text(f"VERDICT: {comparison.verdict_label.upper()}", style="bold"))
    return Group(*parts) if plain or quiet else Panel(Group(*parts), border_style=PALETTE.border)


# Below this many columns the per-resource table no longer fits.
WIDE_COLUMNS = 125


def _process_attribution_lines(comparison: ReportComparison) -> list[str]:
    lines = ["Top retained process evidence: A and B are ranked independently; identities are not paired."]

    def process_label(resource, row):
        name = row["name"][:24]
        return f"PID {row['pid']} {name} +{format_amount(resource, row['delta'])}"

    for label, report in (("A", comparison.before), ("B", comparison.after)):
        available = False
        for resource, ranking in (report.summary.process_attribution or {}).items():
            lifetime, alert = ranking["lifetime"], ranking["alert"]
            if not lifetime["available"] and not alert["available"]:
                continue
            available = True
            row = lifetime["rows"][0] if lifetime["rows"] else None
            detail = process_label(resource, row) if row else "no positive retained change" if lifetime["available"] else "unavailable"
            if row and row["exited"]:
                detail += " (exited)"
            lines.append(f"{label} {resource} lifetime first-to-last: {detail}; startup included.")
            row = alert["rows"][0] if alert["rows"] else None
            detail = process_label(resource, row) if row else "contributor unavailable"
            if row:
                detail += (" (new worker)" if row["new_process"] is True else " (existing worker)"
                           if row["new_process"] is False else " (capacity flag unknown)")
            if alert["available"]:
                window = alert["window_seconds"]
                interval = f"{window:g}s" if window is not None else "unknown interval"
                lines.append(f"{label} {resource} recorded {alert['severity'].upper()} alert, {interval}: {detail}.")
            else:
                lines.append(f"{label} {resource} alert contribution: unavailable (no retained alert).")
            if lifetime["unknown_identity_rows"] or alert["unknown_identity_rows"]:
                lines.append(f"{label} {resource}: some creation times are unknown; identity precision is limited.")
            if lifetime["duplicate_identity_rows_omitted"] or alert["duplicate_identity_rows_omitted"]:
                lines.append(f"{label} {resource}: duplicate identity rows were omitted.")
            if lifetime["invalid_summaries"] or alert["invalid_contributors"]:
                lines.append(f"{label} {resource}: unusable retained rows were skipped.")
            if (lifetime["summaries_omitted"] or alert["snapshots_omitted"]
                    or alert["alerts_omitted"] or alert["contributors_omitted"]):
                lines.append(f"{label} {resource}: a bounded scan omitted retained detail; see --json counts.")
        if not available:
            lines.append(f"{label} process attribution: unavailable (not recorded or no usable retained detail).")
    lines.append("Lifetime and alert intervals differ. Retained rankings may be incomplete and do not identify allocation causes.")
    lines.append("--json includes up to five positive contributors per run, resource and scope, with scan limits and omissions.")
    return lines


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
            _source_text(report, sep),
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
    normalized = [item for item in comparison.resources if item.normalized]
    if normalized and width >= WIDE_COLUMNS:
        entries = [f"{item.resource} {_per_operation_text(item, glyphs)}" for item in normalized]
        parts.append(
            Text.assemble(("per operation  ", f"bold {PALETTE.muted}"), f" {sep} ".join(entries))
        )
    parts.append(Text(""))
    parts.append(Text("\n".join(_process_attribution_lines(comparison))))
    for check in comparison.budgets:
        mark, style = (glyphs.warn, PALETTE.danger) if check.exceeded else (glyphs.ok, PALETTE.ok)
        if not check.measured:
            mark, style = glyphs.warn, PALETTE.warn
        parts.append(Text.assemble(
            ("budget ", f"bold {PALETTE.muted}"), (f"{mark} ", f"bold {style}"),
            (f"B {check.describe()}", style),
        ))
    if comparison.caveats:
        for caveat in comparison.caveats:
            style = PALETTE.danger if comparison.strict and caveat.blocks else PALETTE.warn
            parts.append(
                Text.assemble((f"{glyphs.warn} ", f"bold {style}"), (caveat.message, style))
            )
    else:
        for warning in comparison.warnings:
            parts.append(
                Text.assemble((f"{glyphs.warn} ", f"bold {PALETTE.warn}"), (warning, PALETTE.warn))
            )

    label, color = _verdict_style(comparison)
    parts.append(Text(""))
    parts.append(
        Text.assemble(
            ("VERDICT ", f"bold {PALETTE.muted}"),
            (f" {label} ", f"bold {PALETTE.ink} on {color}"),
            f"  {comparison.headline()}",
        )
    )
    if comparison.qualified and comparison.comparability != COMPARABLE:
        parts.append(Text(
            "Not an equivalent comparison; --strict would report "
            f"'{comparison.comparability}'.",
            style=PALETTE.muted,
        ))
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
    table.add_column(f"PEAK A {glyphs.arrow} B", justify="right", no_wrap=True)
    for item in comparison.resources:
        table.add_row(
            Text(item.resource, style="bold"),
            _signal_text(item, glyphs),
            format_drift(item.resource, item.before.drift_per_second),
            format_drift(item.resource, item.after.drift_per_second),
            _change_text(item, glyphs),
            _growth_text(item, glyphs),
            _peak_text(item, glyphs),
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
        if item.normalized:
            grid.add_row(
                "", Text(f"per op {_per_operation_text(item, glyphs)}", style=PALETTE.muted)
            )
        grid.add_row("", Text(f"peak   {_peak_text(item, glyphs)}", style=PALETTE.muted))
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


def _peak_text(item, glyphs: Glyphs) -> str:
    """Whole-run peaks of the signal metric, ``n/a`` when a run never measured it."""

    def peak(value: float | None) -> str:
        return "n/a" if value is None else format_amount(item.resource, value)

    return f"{peak(item.peak_before)} {glyphs.arrow} {peak(item.peak_after)}"


def _per_operation_text(item, glyphs: Glyphs) -> str:
    def amount(value: float | None) -> str:
        if value is None:
            return "n/a"
        if item.resource == "memory":
            return format_amount(item.resource, value, signed=True)
        return f"{'-' if value < 0 else '+'}{abs(value):,.3g}"

    return (
        f"{amount(item.growth_per_operation_before)} {glyphs.arrow} "
        f"{amount(item.growth_per_operation_after)}"
    )


def _verdict_style(comparison: ReportComparison) -> tuple[str, str]:
    """The verdict badge; a qualified result never shows as a clean pass."""
    label, color = _VERDICT_STYLE[comparison.verdict]
    if comparison.qualified:
        label = f"{label} (QUALIFIED)"
        if comparison.verdict != REGRESSED:
            color = PALETTE.warn
    return label, color


def _source_text(report: LoadedReport, sep: str) -> Text:
    """Target, version and length, the experiment labels, then the file name."""
    text = Text(_describe(report, sep), style="bold")
    labels = _experiment_labels(report, sep)
    if labels:
        text.append("\n")
        text.append(labels, style=PALETTE.accent)
    text.append("\n")
    text.append(report.path.name, style=PALETTE.muted)
    return text


def _experiment_labels(report: LoadedReport, sep: str) -> str:
    experiment = report.experiment
    if experiment is None:
        return ""
    parts = []
    if experiment.workload_id:
        parts.append(f"workload {experiment.workload_id}")
    if experiment.build:
        parts.append(f"build {experiment.build}")
    if experiment.operations is not None:
        parts.append(f"{experiment.operations:,g} ops")
    return f" {sep} ".join(parts)


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
    "EXIT_NOT_COMPARABLE",
    "EXIT_OK",
    "EXIT_OVER_BUDGET",
    "EXIT_REGRESSION",
    "build_diff_parser",
    "render_comparison",
    "run_diff",
]
