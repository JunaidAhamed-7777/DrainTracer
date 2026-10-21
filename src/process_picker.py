"""Searchable, paged terminal selection of an existing process."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext

import psutil
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from .picker_input import PickerKeyboard, supports_picker_keyboard
from .running_processes import (
    RunningProcess,
    is_process_current,
    list_running_processes,
)
from .theme import PALETTE

PAGE_SIZE = 15


def filter_processes(processes: Sequence[RunningProcess], query: str) -> list[RunningProcess]:
    """Match a name or PID substring without changing inventory order."""
    query = query.strip().casefold()
    return [row for row in processes if query in row.name.casefold() or query in str(row.pid)]


def _live_query(value: str, current: str) -> str:
    """Preview search edits while leaving incomplete commands out of the filter."""
    if value.startswith("/"):
        command, separator, text = value.partition(" ")
        return text.strip() if command.casefold() == "/search" and separator else current
    return value.strip()


def _label(value: str) -> str:
    """Keep process names and searches literal and free of terminal controls."""
    return "".join(character if character.isprintable() else " " for character in value)


def _compact(console: Console) -> bool:
    return console.width < 50 or console.height < 22


def _page_size(console: Console) -> int:
    return max(1, min(PAGE_SIZE, console.height - 12))


def _page_view(
    console: Console, processes: Sequence[RunningProcess], query: str, page: int,
    selected: int | None = None,
) -> tuple[Group, int]:
    """Build a bounded page, optionally marking one absolute row index."""
    page_size = _page_size(console)
    pages = max(1, (len(processes) + page_size - 1) // page_size)
    page = min(max(0, page), pages - 1)
    start = page * page_size
    parts = [Text("RUNNING PROGRAMS", style=f"bold {PALETTE.accent}")]
    if query:
        parts.append(Text(f"Search: {_label(query)}", style=PALETTE.muted,
                          no_wrap=True, overflow="ellipsis"))
    if not processes:
        message = "No programs match. Change the search or use /clear or /refresh."
        if not query:
            message = "No running programs are available. Use /refresh to try again."
        parts.append(Text(message, style=PALETTE.warn))
        return Group(*parts), page
    table = Table(box=None if _compact(console) else box.SIMPLE, expand=True, padding=(0, 1))
    if selected is not None:
        table.add_column("", width=1, no_wrap=True)
    table.add_column("PID", justify="right", no_wrap=True, style=PALETTE.accent)
    table.add_column("Program", ratio=1, no_wrap=True, overflow="ellipsis")
    for index, process in enumerate(processes[start : start + page_size], start):
        suffix = " (identity unavailable)" if process.create_time is None else ""
        cells = [str(process.pid), Text(_label(process.name) + suffix)]
        active = index == selected
        if selected is not None:
            cells.insert(0, ">" if active else "")
        table.add_row(*cells, style=f"bold {PALETTE.accent} on {PALETTE.surface}" if active else "")
    end = min(start + page_size, len(processes))
    summary = f"Showing {start + 1}-{end} of {len(processes)} | Page {page + 1}/{pages}"
    if _compact(console):
        summary = f"{len(processes)} matches | Page {page + 1}/{pages}"
    return Group(*parts, table, Text(summary, style=PALETTE.muted)), page


def _render_page(
    console: Console, processes: Sequence[RunningProcess], query: str, page: int,
    selected: int | None = None,
) -> int:
    view, page = _page_view(console, processes, query, page, selected)
    console.print(view)
    return page


def _read_processes(console: Console, *, show_status: bool = True) -> list[RunningProcess] | None:
    try:
        status = console.status("Reading running programs...", spinner="dots")
        with status if show_status else nullcontext():
            return list_running_processes()
    except (psutil.Error, OSError) as exc:
        console.print(Text(f"Could not read running programs: {_label(str(exc))}", style=PALETTE.warn))
        return None


def select_running_process(console: Console) -> int | None:
    """Search the inventory and explicitly select a verified PID, or cancel."""
    console.print(Text("Quitting detaches and leaves the program running.", style=PALETTE.muted))
    processes = _read_processes(console)
    keyboard = PickerKeyboard() if supports_picker_keyboard(console) else None
    display = Live(console=console, screen=True, auto_refresh=False, transient=True) if keyboard else nullcontext()
    with display as live:
        selected = _select_process(console, processes, keyboard, live)
    if selected is None:
        return None
    console.print(Text(f"Hooking to {_label(selected.name)} (PID {selected.pid})."))
    return selected.pid


def _select_process(
    console: Console, processes: list[RunningProcess] | None,
    keyboard: PickerKeyboard | None, live: Live | None,
) -> RunningProcess | None:
    query = ""
    page = 0
    cursor = 0
    buffer = ""
    notice = ""
    while True:
        matches = filter_processes(processes or [], query)
        cursor = min(cursor, max(0, len(matches) - 1))
        if keyboard:
            page = cursor // _page_size(console)
        if processes is None:
            view = Text("Process list unavailable. Use /refresh to retry.", style=PALETTE.warn)
        else:
            view, page = _page_view(console, matches, query, page, cursor if keyboard else None)
        if _compact(console):
            help_text = "Name or PID; /search TEXT\n/next /prev /refresh\n/clear | Q cancel"
            if keyboard:
                help_text = "Up/Down/Enter; PID/name\n/search /clear /refresh\n/next /prev | Esc/Q"
        else:
            help_text = "Type a name to search, or enter a listed PID to hook."
            if keyboard:
                help_text = "Up/Down: move | Enter: select > row | Type a PID or name; Enter to submit."
            help_text += "\n/search TEXT (name or PID) | /next | /prev | /refresh | /clear | Q to cancel"
            if keyboard:
                help_text += " | Esc: cancel"
        parts = [view]
        if notice:
            parts.append(Text(notice, style=PALETTE.warn,
                              no_wrap=keyboard is not None, overflow="ellipsis"))
        frame = Group(*parts, Text(help_text, style=PALETTE.muted))
        try:
            if keyboard:
                assert live is not None
                prompt = Text("Program or search: ", style="bold", no_wrap=True, overflow="ellipsis")
                prompt.append(_label(buffer))
                live.update(Group(frame, prompt), refresh=True)
                key = keyboard.read()
                if key.kind == "escape":
                    return None
                if key.kind in ("up", "down"):
                    cursor = min(max(0, cursor + (1 if key.kind == "down" else -1)),
                                 max(0, len(matches) - 1))
                    buffer = ""
                    continue
                if key.kind == "backspace":
                    buffer = buffer[:-1]
                    continue
                if key.kind == "text":
                    buffer += key.text
                    continue
                value, buffer = buffer.strip(), ""
            else:
                console.print(frame)
                value = console.input(Text("Program or search: ", style="bold")).strip()
        except EOFError:
            return None
        notice = ""
        command = value.casefold()
        if command in ("q", "/quit"):
            return None
        if command == "/refresh":
            processes = _read_processes(console, show_status=live is None)
            page, cursor = 0, 0
        elif command == "/next":
            page += 1
            cursor = min(page, max(0, (len(matches) - 1) // _page_size(console))) * _page_size(console)
        elif command == "/prev":
            page = max(0, page - 1)
            cursor = page * _page_size(console)
        elif command == "/clear":
            query, page, cursor = "", 0, 0
        elif command == "/search" or command.startswith("/search "):
            query, page, cursor = value.partition(" ")[2].strip(), 0, 0
        elif value.startswith("/"):
            notice = "Unknown command. Use /search TEXT to search a name."
        elif value.isdecimal() or (keyboard and not value):
            # Comparing strings also keeps excessively long numeric input bounded.
            selected = next((row for row in matches if str(row.pid) == value.lstrip("0")), None)
            if keyboard and not value and matches:
                selected = matches[cursor]
            if selected is None:
                notice = "That PID is not in these results. Search or /refresh."
                if keyboard and not value:
                    notice = "No program to select. Search or /refresh."
            elif selected.create_time is None:
                notice = (
                    "This program's identity is unavailable. Choose another program, "
                    "or use --pid explicitly to attempt direct attachment."
                )
            elif not is_process_current(selected):
                notice = "That program exited, changed, or cannot be verified. Refreshing the list."
                processes = _read_processes(console, show_status=live is None)
                page, cursor = 0, 0
            else:
                return selected
        elif value:
            query, page, cursor = value, 0, 0
