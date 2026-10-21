"""Searchable, paged terminal selection of an existing process."""

from __future__ import annotations

from collections.abc import Sequence

import psutil
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .running_processes import (
    RunningProcess,
    is_process_current,
    list_running_processes,
)
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


def _read_processes(console: Console) -> list[RunningProcess] | None:
    try:
        with console.status("Reading running programs...", spinner="dots"):
            return list_running_processes()
    except (psutil.Error, OSError) as exc:
        console.print(Text(f"Could not read running programs: {_label(str(exc))}", style=PALETTE.warn))
        return None


def select_running_process(console: Console) -> int | None:
    """Search the inventory and explicitly select a verified PID, or cancel."""
    console.print(Text("Quitting detaches and leaves the program running.", style=PALETTE.muted))
    processes = _read_processes(console)
    query = ""
    page = 0
    while True:
        matches = filter_processes(processes or [], query)
        if processes is None:
            console.print(Text("Process list unavailable. Use /refresh to retry.", style=PALETTE.warn))
        else:
            page = _render_page(console, matches, query, page)
        console.print(Text("Type a name to search, or enter a listed PID to hook.", style=PALETTE.muted))
        console.print(
            Text("/search TEXT (name or PID) | /next | /prev | /refresh | /clear | Q to cancel",
                 style=PALETTE.muted)
        )
        try:
            value = console.input(Text("Program or search: ", style="bold")).strip()
        except EOFError:
            return None
        command = value.casefold()
        if command in ("q", "/quit"):
            return None
        if command == "/refresh":
            processes = _read_processes(console)
            page = 0
        elif command == "/next":
            page += 1
        elif command == "/prev":
            page -= 1
        elif command == "/clear":
            query, page = "", 0
        elif command == "/search" or command.startswith("/search "):
            query, page = value.partition(" ")[2].strip(), 0
        elif value.startswith("/"):
            console.print(Text("Unknown command. Use /search TEXT to search a name.", style=PALETTE.warn))
        elif value.isdecimal():
            # Comparing strings also keeps excessively long numeric input bounded.
            selected = next((row for row in matches if str(row.pid) == value.lstrip("0")), None)
            if selected is None:
                console.print(Text("That PID is not in these results. Search or /refresh.", style=PALETTE.warn))
            elif selected.create_time is None:
                console.print(
                    Text("This program's identity is unavailable. Choose another program, "
                         "or use --pid explicitly to attempt direct attachment.", style=PALETTE.warn)
                )
            elif not is_process_current(selected):
                console.print(
                    Text("That program exited, changed, or cannot be verified. Refreshing the list.",
                         style=PALETTE.warn)
                )
                processes = _read_processes(console)
                page = 0
            else:
                console.print(Text(f"Hooking to {_label(selected.name)} (PID {selected.pid})."))
                return selected.pid
        elif value:
            query, page = value, 0
