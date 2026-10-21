"""Searchable, paged terminal selection of an existing process."""

from __future__ import annotations

from collections.abc import Sequence

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .running_processes import RunningProcess
from .theme import PALETTE


def filter_processes(processes: Sequence[RunningProcess], query: str) -> list[RunningProcess]:
    """Match a name or PID substring without changing inventory order."""
    query = query.strip().casefold()
    return [row for row in processes if query in row.name.casefold() or query in str(row.pid)]


def _label(value: str) -> str:
    """Keep process names and searches literal and free of terminal controls."""
    return "".join(character if character.isprintable() else " " for character in value)


def _render_page(
    console: Console, processes: Sequence[RunningProcess], query: str, page: int
) -> int:
    """Render a bounded page and return its clamped index."""
    page_size = max(1, min(10, console.height - 12))
    pages = max(1, (len(processes) + page_size - 1) // page_size)
    page = min(max(0, page), pages - 1)
    start = page * page_size
    console.print(Text("RUNNING PROGRAMS", style=f"bold {PALETTE.accent}"))
    if query:
        console.print(Text(f"Search: {_label(query)}", style=PALETTE.muted))
    if not processes:
        message = "No programs match. Change the search or use /clear or /refresh."
        if not query:
            message = "No running programs are available. Use /refresh to try again."
        console.print(Text(message, style=PALETTE.warn))
        return page
    table = Table(box=box.SIMPLE, expand=True, padding=(0, 1))
    table.add_column("PID", justify="right", no_wrap=True, style=PALETTE.accent)
    table.add_column("Program", ratio=1, no_wrap=True, overflow="ellipsis")
    for process in processes[start : start + page_size]:
        suffix = " (identity unavailable)" if process.create_time is None else ""
        table.add_row(str(process.pid), Text(_label(process.name) + suffix))
    console.print(table)
    end = min(start + page_size, len(processes))
    console.print(
        Text(f"Showing {start + 1}-{end} of {len(processes)} | Page {page + 1}/{pages}",
             style=PALETTE.muted)
    )
    return page
