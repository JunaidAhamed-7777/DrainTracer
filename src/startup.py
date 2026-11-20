"""Live system overview and startup choices for launching or attaching."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from threading import Event, Thread
from typing import Literal

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .keys import KeyReader, stdin_is_interactive, stream_is_tty
from .system_overview import (
    DEFAULT_PROCESS_LIMIT,
    RankedProcess,
    SystemOverview,
    SystemOverviewSampler,
)
from .theme import PALETTE, select_glyphs
from .units import format_bytes, safe_label

StartupAction = Literal["open", "hook"]
_CPU_WARMUP = 0.25
_SAMPLE_INTERVAL = 1.0
_INPUT_INTERVAL = 0.05

_ACTIONS = (
    "Open a program",
    "Hook to a running program",
)


def _actions(selected: int | None, *, held: bool = False, wide: bool = False) -> Group:
    choices = Text(no_wrap=True, overflow="ellipsis")
    for index, label in enumerate(_ACTIONS):
        if index:
            choices.append("    " if wide else "\n")
        marker = ">" if index == selected else " "
        style = f"bold {PALETTE.ink} on {PALETTE.accent}" if index == selected else "bold"
        choices.append(f"{marker} [{index + 1}] {label}", style=style)
    if selected is None:
        return Group(choices, Text("Enter 1 or 2 to select | Q to quit", style=PALETTE.muted))
    hold_action = "resume" if held else "hold"
    hint = f"Up/Down move   Enter select   P {hold_action}   Q quit" if wide else f"Up/Down Enter | P {hold_action} | Q quit"
    return Group(
        choices,
        Text(hint, style=PALETTE.muted, no_wrap=True, overflow="ellipsis"),
    )


def _host_resources(console: Console, overview: SystemOverview | None, held: bool) -> Panel:
    cpu = "Sampling..." if overview is None else "Unavailable"
    memory = cpu
    ram_percent = None
    if overview is not None:
        if overview.cpu_percent is not None:
            cpu = f"{overview.cpu_percent:.1f}%"
        used, total = overview.used_memory_bytes, overview.total_memory_bytes
        if used is not None and total is not None and total > 0:
            ram_percent = 100 * used / total
            memory = f"{format_bytes(used)} / {format_bytes(total)} ({ram_percent:.1f}%)"
    values = Table.grid(padding=(0, 1), pad_edge=False)
    values.add_column(style="bold")
    has_meter = console.width >= 64
    if has_meter:
        values.add_column()
    values.add_column(no_wrap=True)
    for label, reading, percent in (
        ("CPU", cpu, overview.cpu_percent if overview else None), ("RAM", memory, ram_percent),
    ):
        cells = [Text(label)]
        if has_meter:
            glyphs = select_glyphs(console)
            width = min(32, console.width // 4)
            filled = round(width * min(100, max(0, percent or 0)) / 100)
            # Distinct shapes keep the scale visible on a monochrome terminal.
            cells.append(Text.assemble(
                (glyphs.spark[-1] * filled, PALETTE.cyan), ("." * (width - filled), PALETTE.muted),
            ))
        cells.append(Text(reading))
        values.add_row(*cells)
    state = "On hold" if held else "Live"
    if overview and overview.sampled_at is not None:
        state += " " + time.strftime("%H:%M:%S", time.localtime(overview.sampled_at))
    return Panel(
        values, title=f"[bold {PALETTE.accent}] SYSTEM OVERVIEW [/]", title_align="left",
        subtitle=state, subtitle_align="right",
        border_style=PALETTE.border, box=box.ROUNDED, padding=(0, 1),
    )


def _ranked_list(
    console: Console,
    title: str,
    rows: tuple[RankedProcess, ...],
    format_value: Callable[[int | float], str],
    empty: str,
) -> Group:
    heading = Rule(Text(title, style=f"bold {PALETTE.cyan}"), align="left", style=PALETTE.border)
    if not rows:
        return Group(heading, Text(empty, style=PALETTE.muted, no_wrap=True, overflow="ellipsis"))

    table = Table.grid(expand=True, padding=(0, 1), pad_edge=False)
    table.add_column(ratio=1, no_wrap=True)
    table.add_column(justify="right", no_wrap=True, style=PALETTE.muted)
    table.add_column(justify="right", no_wrap=True)
    encoding = console.encoding or "utf-8"
    for row in rows:
        name = safe_label(row.process.name)
        name = name.encode(encoding, errors="replace").decode(encoding)
        table.add_row(Text(name, overflow="ellipsis"), f"PID {row.process.pid}", format_value(row.value))
    return Group(heading, table)


def _startup_view(
    console: Console, overview: SystemOverview | None, selected: int, page: int, *, held: bool = False,
) -> tuple[Group, int]:
    """Fit all four rankings on screen, paging their rows together."""
    wide = console.width >= 68
    groups = (
        ("CPU (% of one core)", overview.top_cpu if overview else (), lambda value: f"{value:.1f}%"),
        ("Memory (RSS)", overview.top_memory if overview else (), format_bytes),
        (overview.handle_label if overview else "Handles / FDs",
         overview.top_handles if overview else (), lambda value: f"{value:,}"),
        ("Threads", overview.top_threads if overview else (), lambda value: f"{value:,}"),
    )
    longest = max(len(rows) for _, rows, _ in groups)
    # Host panel, actions, keyboard help and ranking title precede the lists.
    bands, fixed_rows = (2, 7) if wide else (4, 8)
    per_page = max(1, (console.height - fixed_rows) // bands - 1)
    pages = max(1, (longest + per_page - 1) // per_page)
    page = min(max(0, page), pages - 1)
    start = page * per_page
    title = Text("TOP PROCESSES", style=f"bold {PALETTE.accent}", no_wrap=True, overflow="ellipsis")
    if pages > 1:
        title.append(f"  {page + 1}/{pages} Left/Right", style=PALETTE.muted)

    lists = []
    for metric, rows, format_value in groups:
        empty = "Sampling..." if overview is None else "No more entries" if rows else "No readings"
        lists.append(_ranked_list(console, metric, rows[start:start + per_page], format_value, empty))
    if wide:
        grid = Table.grid(expand=True, padding=(0, 3), pad_edge=False)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(lists[0], lists[1])
        grid.add_row(lists[2], lists[3])
        rankings = grid
    else:
        rankings = Group(*lists)
    return Group(_host_resources(console, overview, held), _actions(selected, held=held, wide=wide), title, rankings), page


def _collect_overviews(limit: int, updates: deque[SystemOverview], stop: Event) -> None:
    sampler = SystemOverviewSampler(limit)
    delay = _CPU_WARMUP
    while not stop.is_set():
        overview = sampler.sample(should_stop=stop.is_set)
        if stop.is_set():
            break
        updates.append(overview)
        stop.wait(delay)
        delay = _SAMPLE_INTERVAL


def _choose_live(console: Console, limit: int) -> StartupAction | None:
    updates: deque[SystemOverview] = deque(maxlen=1)
    stop = Event()
    worker = Thread(target=_collect_overviews, args=(limit, updates, stop),
                    name="draintracer-system-overview", daemon=True)
    worker.start()
    try:
        overview = None
        held = False
        selected = page = 0
        last_frame = None
        keyboard = KeyReader(native_windows=True)
        with keyboard as keys, Live(console=console, auto_refresh=False, transient=True) as live:
            while True:
                if updates and not held:
                    overview = updates.pop()
                size = console.size
                frame = (size, overview, selected, page, held)
                if frame != last_frame:
                    view, page = _startup_view(console, overview, selected, page, held=held)
                    live.update(view, refresh=True)
                    last_frame = (size, overview, selected, page, held)
                key = keys.read()
                if key in ("escape", "q", "Q", "\x04", "\x1a") or keys.eof:
                    return None
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in ("1", "2"):
                    return "open" if key == "1" else "hook"
                if key in ("enter", " "):
                    return "open" if selected == 0 else "hook"
                if key in ("up", "down"):
                    selected = int(key == "down")
                elif key in ("left", "right"):
                    page += 1 if key == "right" else -1
                elif key in ("p", "P") and overview is not None:
                    held = not held
                time.sleep(_INPUT_INTERVAL)
    finally:
        stop.set()
        worker.join(timeout=_INPUT_INTERVAL)


def choose_startup_action(
    console: Console, *, limit: int = DEFAULT_PROCESS_LIMIT,
) -> StartupAction | None:
    """Show the system overview, then select a target action or quit."""
    if console.is_terminal and stream_is_tty(console.file) and stdin_is_interactive():
        return _choose_live(console, limit)

    console.print(_actions(None))
    while True:
        try:
            choice = console.input(Text("Choose 1 or 2 (Q to quit): ", style="bold"))
        except EOFError:
            return None
        choice = choice.strip().lower()
        if choice == "q":
            return None
        if choice in ("1", "2"):
            return "open" if choice == "1" else "hook"
        console.print(Text("Enter 1, 2, or Q.", style=PALETTE.warn))
